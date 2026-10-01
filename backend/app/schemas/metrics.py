"""Response models for ``/api/v1/metrics``.

Written to the same two rules as :mod:`app.schemas.analytics`, and for the same
reasons:

**A response model is a filter.** FastAPI serialises through these, so a field
forgotten here is a field the endpoint silently stops returning.
``test_metrics_model_does_not_drop_fields`` diffs the keys
:mod:`app.services.ops_metrics` produces against the keys the endpoint answers
with, so adding a section to the service without adding it here fails a test
rather than quietly shrinking the payload.

**A rate is not a number.** ``success_rate`` and ``avg_generation_ms`` are
``None`` when there is nothing to compute them from — no settled publication,
no generation in the window — and a client that renders either as zero is
reporting a total failure where the truth is an absence of data.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ContentCountsOut(BaseModel):
    """How many pieces the account has, per status."""

    total: int = Field(description="Every piece the account has, in any status.")
    by_status: dict[str, int] = Field(
        description=(
            "One entry per status in Pulse's vocabulary — draft, review, "
            "approved, published, archived, failed — including the ones at "
            "zero, so the key set does not change as the data does."
        )
    )


class ProjectScanOut(BaseModel):
    """One project's scan history, as the stored counters record it."""

    project_id: int
    name: str
    scan_count: int = Field(description="Completed scans of this repo, ever.")
    last_scanned_at: datetime | None = Field(
        default=None, description="When the last scan finished; null if never scanned."
    )
    last_scan_duration_ms: int | None = Field(
        default=None, description="How long the last completed scan took, end to end."
    )
    scans_per_day: float | None = Field(
        default=None,
        description=(
            "Scans per day over the project's lifetime, or null for a project "
            "less than a day old — a rate over a few hours is an artefact of "
            "the denominator, not a measurement."
        ),
    )


class PlatformPublishOut(BaseModel):
    """Publication outcomes for one platform."""

    platform: str
    published: int
    failed: int
    in_flight: int = Field(
        description="Rows not yet settled — pending, scheduled, or publishing."
    )
    success_rate: float | None = Field(
        default=None,
        description=(
            "Published over settled (published + failed), 0–1. Null when "
            "nothing has settled yet, which is not the same as a rate of zero."
        ),
    )
    timed: int = Field(
        default=0,
        description=(
            "Attempts that actually reached the platform and were timed — the "
            "denominator of avg_duration_ms, reported so a mean over two "
            "attempts is not mistaken for a measurement of the platform."
        ),
    )
    avg_duration_ms: int | None = Field(
        default=None,
        description=(
            "Mean wall-clock of this platform's API call, failed attempts "
            "included. Null when nothing has been timed — a row still pending "
            "or scheduled has no duration, and neither has one written before "
            "Pulse recorded them."
        ),
    )


class PublishRatesOut(BaseModel):
    """Publication outcomes and platform latency for the account."""

    published: int
    failed: int
    in_flight: int
    success_rate: float | None = Field(default=None)
    timed: int = 0
    avg_duration_ms: int | None = Field(
        default=None,
        description=(
            "Mean platform latency across every timed attempt, weighted by "
            "each platform's share of them rather than a mean of the means."
        ),
    )
    by_platform: list[PlatformPublishOut]


class ProviderUsageOut(BaseModel):
    """One provider's slice of the LLM window."""

    provider: str
    calls: int
    calls_failed: int
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    avg_duration_ms: int = Field(
        description="Mean wall-clock over every attempt, failed ones included."
    )


class PurposeUsageOut(BaseModel):
    """One feature's slice of the LLM window."""

    purpose: str = Field(
        description=(
            'What the completion was for — "content", "headlines", "ideas", '
            '"repurpose", "inline_edit". Empty for rows written before the '
            "caller started tagging."
        )
    )
    calls: int
    calls_failed: int
    total_tokens: int
    avg_duration_ms: int


class LLMUsageOut(BaseModel):
    """Token spend and latency over the window.

    Install-wide, not per-account: the usage table has no owner column, because
    the router that writes it does not know whose request it is serving. See
    :mod:`app.services.ops_metrics`.
    """

    window_hours: int
    calls: int
    calls_failed: int
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    avg_duration_ms: int
    by_provider: list[ProviderUsageOut]
    by_purpose: list[PurposeUsageOut]
    avg_generation_ms: int | None = Field(
        default=None,
        description=(
            "Mean wall-clock of the completions that generated a piece, or "
            "null if none were generated in the window. Pulse stores no "
            "generation duration on the content itself, so this is the only "
            "source for it."
        ),
    )
    generations: int = Field(
        description="How many generation completions the average is over."
    )


class BreakersOut(BaseModel):
    """Which upstreams are being stood down, in the process that answered.

    Circuit breakers are in-memory and per-process. The process answering this
    request is the API; content is generated by a Celery worker with its own.
    An empty block here means "none open in the web process", not "none open".
    """

    scope: str = Field(
        description="Which process these snapshots came from. Always api-process."
    )
    llm: dict[str, dict[str, float]] = Field(
        description="Provider name to failure count and seconds until retry."
    )
    publish: dict[str, dict[str, float]] = Field(
        description="platform:account key to failure count and seconds until retry."
    )


class MetricsOut(BaseModel):
    """The operational picture: how the install is running, not how posts did."""

    window_hours: int = Field(
        description="The window the LLM section covers. The rest is all-time."
    )
    content: ContentCountsOut
    projects: list[ProjectScanOut]
    publishing: PublishRatesOut
    llm: LLMUsageOut
    breakers: BreakersOut


__all__ = [
    "BreakersOut",
    "ContentCountsOut",
    "LLMUsageOut",
    "MetricsOut",
    "PlatformPublishOut",
    "ProjectScanOut",
    "ProviderUsageOut",
    "PublishRatesOut",
    "PurposeUsageOut",
]
