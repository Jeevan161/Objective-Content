"""
app/mcq_pipeline/utils/pricing.py
---------------------------------
Token → USD cost ESTIMATION for pipeline LLM calls.

The proxy (and every LLM API) returns token COUNTS, never a dollar figure — so cost is
computed here from a per-model price table. The figure is therefore an ESTIMATE against
published list prices; a proxy billing at internal/negotiated rates will differ.

Rates are USD per 1,000,000 tokens: (input, cached_input, output).
  * input        — prompt tokens billed at the full rate (i.e. prompt_tokens minus cached)
  * cached_input — prompt tokens served from the provider's prompt cache (cheaper)
  * output       — completion tokens

Override or extend the table at deploy time with the MODEL_PRICES_JSON env var, e.g.
  MODEL_PRICES_JSON={"gpt-4o": [2.5, 1.25, 10.0], "my-proxy-model": [1.0, 0.5, 3.0]}
Keys are matched case-insensitively by LONGEST-PREFIX, so a snapshot like
"gpt-4o-2024-11-20" resolves to the "gpt-4o" entry.
"""
from __future__ import annotations

import json
import os

# (input_per_1m, cached_input_per_1m, output_per_1m) in USD. Seeded from published
# list prices (June 2026). GPT-4o is no longer on OpenAI's live pricing page, so these
# are maintained defaults — correct them here or via MODEL_PRICES_JSON as prices change.
_DEFAULT_PRICES: dict[str, tuple[float, float, float]] = {
    # OpenAI — the internal proxy serves gpt-4o-2024-11-20
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-4o": (2.50, 1.25, 10.00),
    # Anthropic Claude (generation role) — list prices; cached = cache-read rate
    "claude-sonnet-4": (3.00, 0.30, 15.00),
    "claude-3-5-sonnet": (3.00, 0.30, 15.00),
    "claude-3-5-haiku": (0.80, 0.08, 4.00),
}


def _load_prices() -> dict[str, tuple[float, float, float]]:
    prices = dict(_DEFAULT_PRICES)
    raw = os.environ.get("MODEL_PRICES_JSON")
    if raw:
        try:
            for k, v in (json.loads(raw) or {}).items():
                if isinstance(v, (list, tuple)) and len(v) == 3:
                    prices[str(k).lower()] = (float(v[0]), float(v[1]), float(v[2]))
        except Exception:  # noqa: BLE001 — a bad override must never break a run
            pass
    return prices


_PRICES = _load_prices()


def _rates_for(model: str | None) -> tuple[float, float, float] | None:
    """Longest-prefix, case-insensitive lookup. OpenRouter slugs (e.g.
    "anthropic/claude-sonnet-4.6") match on their trailing path segment too."""
    if not model:
        return None
    name = model.lower()
    candidates = [name]
    if "/" in name:
        candidates.append(name.rsplit("/", 1)[-1])
    best: tuple[int, tuple[float, float, float]] | None = None
    for cand in candidates:
        for key, rates in _PRICES.items():
            if cand.startswith(key) and (best is None or len(key) > best[0]):
                best = (len(key), rates)
    return best[1] if best else None


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int,
                  cached_tokens: int = 0) -> dict:
    """Estimate the USD cost of one call (or an aggregate) for `model`.

    `input_tokens` is the TOTAL prompt tokens (cached + uncached), matching the OpenAI
    `usage` convention; `cached_tokens` of those are billed at the cheaper cached rate.
    Returns {"usd": float, "priced": bool, "model": str} — priced=False when the model
    has no rate (usd=0, so it's visible as "unpriced" rather than silently wrong)."""
    rates = _rates_for(model)
    if rates is None:
        return {"usd": 0.0, "priced": False, "model": model or ""}
    in_rate, cached_rate, out_rate = rates
    cached = max(0, min(int(cached_tokens or 0), int(input_tokens or 0)))
    uncached = max(0, int(input_tokens or 0) - cached)
    usd = (uncached * in_rate + cached * cached_rate + int(output_tokens or 0) * out_rate) / 1_000_000
    return {"usd": round(usd, 6), "priced": True, "model": model or ""}


def is_priced(model: str | None) -> bool:
    return _rates_for(model) is not None


def merge_summaries(summaries: list[dict]) -> dict:
    """Combine several cost summaries (e.g. a base run + its later variant phase) into one,
    re-deriving per-model/per-step buckets and totals. USD is recomputed per model from the
    merged token counts, so it stays consistent with the price table."""
    by_model: dict[str, dict] = {}
    by_step: dict[str, dict] = {}
    unpriced: set[str] = set()

    def _acc(target: dict, key: str, row: dict) -> None:
        b = target.setdefault(key, {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0})
        b["input_tokens"] += int(row.get("input_tokens") or 0)
        b["output_tokens"] += int(row.get("output_tokens") or 0)
        b["cached_tokens"] += int(row.get("cached_tokens") or 0)
        b["calls"] += int(row.get("calls") or 0)

    for s in summaries:
        if not isinstance(s, dict):
            continue
        for row in s.get("by_model") or []:
            _acc(by_model, row.get("model") or "(unknown)", row)
        for row in s.get("by_step") or []:
            _acc(by_step, row.get("step") or "(unattributed)", row)
        unpriced.update(s.get("unpriced_models") or [])

    tot_in = tot_out = tot_cached = calls = 0
    tot_usd = 0.0
    out_models = []
    for model, b in sorted(by_model.items()):
        c = estimate_cost(model, b["input_tokens"], b["output_tokens"], b["cached_tokens"])
        out_models.append({"model": model, **b, "estimated_cost_usd": c["usd"]})
        tot_in += b["input_tokens"]; tot_out += b["output_tokens"]
        tot_cached += b["cached_tokens"]; calls += b["calls"]; tot_usd += c["usd"]
    out_steps = [{"step": k, **v} for k, v in sorted(by_step.items())]
    return {
        "input_tokens": tot_in, "output_tokens": tot_out, "cached_tokens": tot_cached,
        "total_tokens": tot_in + tot_out, "calls": calls,
        "estimated_cost_usd": round(tot_usd, 6),
        "by_model": out_models, "by_step": out_steps, "unpriced_models": sorted(unpriced),
    }


def summarize(entries: list[dict]) -> dict:
    """Aggregate a list of per-call usage entries (each {model, input_tokens, output_tokens,
    cached_tokens, step}) into a run/node cost summary: totals, estimated USD, and breakdowns
    by model and by step. USD is summed PER MODEL (each model priced at its own rate)."""
    by_model: dict[str, dict] = {}
    by_step: dict[str, dict] = {}
    unpriced: set[str] = set()

    def _bucket(d: dict, key: str) -> dict:
        return d.setdefault(key, {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                                  "calls": 0, "estimated_cost_usd": 0.0})

    tot_in = tot_out = tot_cached = calls = 0
    tot_usd = 0.0
    for e in entries or []:
        model = e.get("model") or ""
        inp = int(e.get("input_tokens") or 0)
        out = int(e.get("output_tokens") or 0)
        cached = int(e.get("cached_tokens") or 0)
        cost = estimate_cost(model, inp, out, cached)
        if not cost["priced"] and model:
            unpriced.add(model)
        tot_in += inp; tot_out += out; tot_cached += cached; calls += 1; tot_usd += cost["usd"]
        for d, key in ((by_model, model or "(unknown)"), (by_step, e.get("step") or "(unattributed)")):
            b = _bucket(d, key)
            b["input_tokens"] += inp; b["output_tokens"] += out
            b["cached_tokens"] += cached; b["calls"] += 1
            b["estimated_cost_usd"] = round(b["estimated_cost_usd"] + cost["usd"], 6)

    return {
        "input_tokens": tot_in, "output_tokens": tot_out, "cached_tokens": tot_cached,
        "total_tokens": tot_in + tot_out, "calls": calls,
        "estimated_cost_usd": round(tot_usd, 6),
        "by_model": [{"model": k, **v} for k, v in sorted(by_model.items())],
        "by_step": [{"step": k, **v} for k, v in sorted(by_step.items())],
        "unpriced_models": sorted(unpriced),
    }
