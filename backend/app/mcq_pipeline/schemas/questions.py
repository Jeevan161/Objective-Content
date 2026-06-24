"""
qgen_schemas.py (vendored verbatim)
-----------------------------------
LEAN output models — what each per-type question agent returns (question-specific
content only; the platform scaffold is added by a later formatting step).
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class MCQOption(BaseModel):
    content: str
    is_correct: bool
    content_type: str = Field(
        default="TEXT",
        description="how the portal should render this option: 'TEXT' for a plain value / "
                    "literal code / output, or 'MARKDOWN' when the option uses inline Markdown "
                    "(e.g. `code`, **bold**). Default to TEXT unless the option text actually "
                    "contains Markdown.")


# MULTIPLE_CHOICE and MORE_THAN_ONE_MULTIPLE_CHOICE share this shape.
class MCQLean(BaseModel):
    question: str = Field(description="the question stem (plain text/markdown)")
    options: List[MCQOption] = Field(description="answer options; mark the correct one(s)")
    explanation: str = Field(description="why the correct option(s) is right and the others wrong")


class TrueFalseLean(BaseModel):
    statement: str = Field(description="a single claim the learner judges true/false")
    is_true: bool
    code: str = Field(default="", description="optional snippet the statement is about; "
                                              "leave empty for a purely conceptual statement")
    code_language: str = Field(default="", description="language of `code` when present, "
                                                       "e.g. PYTHON/JAVA/JS/SQL; empty when there is no code")
    explanation: str = Field(description="why the statement is true / false")


class TextualLean(BaseModel):
    question: str
    answer: str = Field(description="the exact expected answer string (term/value/command)")
    explanation: str = Field(description="why this is the expected answer")


class CodeMCQLean(BaseModel):
    question: str = Field(description="the question stem about the code (e.g. output / logic / error)")
    code: str = Field(description="a short runnable snippet using ONLY taught syntax")
    code_language: str = Field(description="language of the snippet, e.g. PYTHON/JAVA/JS/SQL")
    correct_output: str = Field(description="the exact real output of the code")
    wrong_answers: List[str] = Field(description="plausible but wrong outputs (distractors)")
    explanation: str = Field(description="why the correct output is right and the distractors wrong")


class CodeTextualLean(BaseModel):
    question: str = Field(description="the question stem about the code")
    code: str
    code_language: str = Field(description="language of the snippet, e.g. PYTHON/JAVA/JS/SQL")
    expected_output: str = Field(description="the exact real output the learner must type")
    explanation: str = Field(description="why this is the expected output")


class CodeMoreThanOneLean(BaseModel):
    question: str = Field(description="the question stem about the code")
    code: str
    code_language: str = Field(description="language of the snippet, e.g. PYTHON/JAVA/JS/SQL")
    correct_outputs: List[str] = Field(description="all true statements/outputs about the code")
    wrong_answers: List[str] = Field(description="false statements (distractors)")
    explanation: str = Field(description="why the correct statements hold and the others do not")


class FibCodingLean(BaseModel):
    question: str = Field(description="what the learner must accomplish by filling the blank")
    code_lines: List[str] = Field(
        description="the full program, line by line. The line to blank out must contain "
                    "the sentinel {{BLANK}} exactly where the blank goes.")
    code_language: str = Field(description="language of the snippet, e.g. PYTHON/JAVA/JS/SQL")
    blank_answer: str = Field(description="the code that correctly fills {{BLANK}}")
    test_input: str = Field(default="", description="stdin for the test case, if any")
    test_output: str = Field(description="expected stdout when the blank is filled correctly")
    explanation: str = Field(description="why this fills the blank correctly")


class RearrangeLean(BaseModel):
    question: str
    ordered_items: List[str] = Field(description="the items in the CORRECT order (first to last)")
    explanation: str = Field(description="why this ordering is correct")
