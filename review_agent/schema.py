"""Structured output contract for a review."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["bug", "risk", "missing_test", "style"]
Severity = Literal["high", "medium", "low"]


class Finding(BaseModel):
    category: Category
    severity: Severity
    file: str = Field(description="Path of the changed file, exactly as in the diff")
    line: int | None = Field(default=None, description="Line number in the NEW file, or null if file-level")
    title: str = Field(description="One-line summary")
    explanation: str = Field(description="Why this is a problem, grounded in the diff/context")
    suggestion: str = Field(default="", description="Concrete fix")
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    source: Literal["llm", "static"] = "llm"


class Review(BaseModel):
    summary: str = Field(description="2-4 sentence overview of the change and its main risks")
    verdict: Literal["approve", "comment", "request_changes"]
    findings: list[Finding] = Field(default_factory=list)


class ReviewResult(BaseModel):
    """What the CLI/UI returns: the review plus run metadata."""
    review: Review
    model: str
    tool_calls: list[str] = Field(default_factory=list)
    dropped_findings: list[str] = Field(default_factory=list)
    files_reviewed: list[str] = Field(default_factory=list)
    redactions: int = 0
