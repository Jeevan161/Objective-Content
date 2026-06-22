"""Portal environments, URLs, and shared constants.

Two portals are supported — PROD and BETA — selectable per request. Values can
be overridden via environment variables so the same code works without edits.
"""
import os

# ── Environments ─────────────────────────────────────────────────────────────
# Each environment has its own base URL and login credentials.
ENVIRONMENTS = {
    "PROD": {
        "base_url": os.environ.get("PORTAL_PROD_BASE_URL", ""),
        "username": os.environ.get("PORTAL_PROD_USERNAME", "content_loader"),
        "password": os.environ.get("PORTAL_PROD_PASSWORD", ""),
    },
    "BETA": {
        "base_url": os.environ.get("PORTAL_BETA_BASE_URL", ""),
        "username": os.environ.get("PORTAL_BETA_USERNAME", "content_loader"),
        "password": os.environ.get("PORTAL_BETA_PASSWORD", ""),
    },
}

DEFAULT_ENVIRONMENT = "PROD"

# The learning site is shared across environments (used only to build links).
LEARNING_COURSE_URL = os.environ.get("PORTAL_LEARNING_COURSE_URL", "")

# ── HTTP ─────────────────────────────────────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.6533.89 Safari/537.36"
)
CSRF_TOKEN_INPUT_NAME = "csrfmiddlewaretoken"

# Name fields per unit type are the same regardless of environment.
UNIT_NAME_FIELD_MAP = {
    "QUESTION_SET": "title",
    "LEARNING_SET": "name",
    "PRACTICE": "title",
    "QUIZ": "title",
}


class PortalConfig:
    """All URLs + credentials for one environment, built from its base URL."""

    def __init__(self, environment=DEFAULT_ENVIRONMENT):
        env_key = (environment or DEFAULT_ENVIRONMENT).upper()
        env = ENVIRONMENTS.get(env_key)
        if not env:
            valid = ", ".join(ENVIRONMENTS)
            raise ValueError(f"Unknown environment '{environment}'. Valid: {valid}")

        self.environment = env_key
        self.base_url = env["base_url"]
        self.username = env["username"]
        self.password = env["password"]

        base = self.base_url
        self.login_url = f"{base}/admin/login/"
        self.resource_links_url = f"{base}/admin/nkb_resources/resourceparentresourcethroughmodel/"
        self.course_detail_url_template = f"{base}/admin/nkb_resources/course/{{}}"
        self.topic_detail_url_template = f"{base}/admin/nkb_resources/topic/{{}}/change/"
        self.unit_detail_url_template = f"{base}/admin/nkb_resources/unit/{{}}"
        # Admin change page for a single learning resource — its `content`
        # textarea holds the cheat-sheet markdown (no Bearer token needed).
        self.learning_resource_detail_url_template = (
            f"{base}/admin/nkb_learning_resource/learningresource/{{}}/"
        )
        self.course_version_list_url = f"{base}/admin/nkb_resources/courseversion/"
        self.course_version_detail_url_template = f"{base}/admin/nkb_resources/courseversion/{{}}/change/"
        self.unit_name_url_map = {
            "QUESTION_SET": f"{base}/admin/nkb_question/questionset/{{}}/change/",
            "LEARNING_SET": f"{base}/admin/nkb_learning_resource/learningresourceset/{{}}/",
            "PRACTICE": f"{base}/admin/nkb_exam/exam/{{}}/",
            "QUIZ": f"{base}/admin/nkb_exam/exam/{{}}/",
        }
        self.unit_name_field_map = UNIT_NAME_FIELD_MAP
