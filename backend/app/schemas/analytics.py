"""Response models for the analytics dashboard.

The nine analytics endpoints returned ``dict``. Every one of them had a
summary, a description and its error codes in the schema, and then said the
success body was an object — of what, unstated. A generated client got
``Any``; a reader of ``/docs`` got an empty box where the interesting half
should be; and nothing anywhere connected ``read_rate`` to the reason it can be
``None``.

Two things are load-bearing about how these are written.

**A response model is a filter.** FastAPI serialises through it, so a field
these models forget is a field the API silently stops returning. That makes
completeness a correctness property rather than a documentation nicety, and it
is asserted directly: ``test_analytics_models_do_not_drop_fields`` builds a
populated account and diffs every key the services produce against every key
the endpoint answers with.

**A rate is not a number.** ``None`` and ``0.0`` mean different things
everywhere below — "no platform you publish to reports this" against "nobody
did it" — and the distinction is the whole reason :func:`~app.services
.analytics_service._rate` counts what was *reported* separately from what was
counted. Typing these as ``float | None`` is what carries that into the schema:
a client that renders a missing rate as 0% is drawing a conclusion nothing
supports.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings
from app.services.velocity import window_count_key


class _Rates(BaseModel):
    """The three derived rates every aggregation reports.

    ``None`` is not zero. A rate is ``None`` when nothing reported the
    numerator at all — no platform in the bucket distinguishes a read from a
    view, say — and ``0.0`` when something reported it and the answer was
    nought. Charting the first as the second invents a finding.
    """

    click_through_rate: float | None = Field(
        default=None, description="Clicks per view, or null if no platform reported clicks."
    )
    read_rate: float | None = Field(
        default=None, description="Reads per view, or null if no platform reported reads."
    )
    engagement_rate: float | None = Field(
        default=None,
        description=(
            "Any interaction per view. Engagement is derived from columns that "
            "coalesce to zero, so this is null only when there are no views."
        ),
    )


class TotalsOut(_Rates):
    """Account-wide counters, plus the rates derived from them."""

    content_count: int = 0
    published_count: int = 0
    publication_count: int = 0
    views: int = 0
    #: Only platforms that distinguish "opened" from "read to the end" report
    #: this — Dev.to and Medium do, the social platforms have nothing to say.
    reads: int = 0
    clicks: int = 0
    engagement: int = 0
    #: How many publications reported reads / clicks at all. Zero is what makes
    #: the matching rate null rather than zero.
    reads_reported: int = 0
    clicks_reported: int = 0


class ContentTypeStatsOut(_Rates):
    """One row of the per-content-type breakdown."""

    content_type: str
    label: str
    publications: int
    views: int
    reads: int
    clicks: int
    engagement: int
    #: Averaged over the publications that reported a view count, not over the
    #: ones that exist. Null when none did.
    avg_views: float | None = None


class PlatformStatsOut(_Rates):
    """One row of the per-platform breakdown."""

    platform: str
    published: int
    #: Reported alongside reach on purpose: "LinkedIn gets no views" and
    #: "LinkedIn rejected every post" look identical in a views-only table and
    #: need very different responses.
    failed: int
    views: int
    reads: int
    clicks: int
    engagement: int


class ProjectStatsOut(BaseModel):
    """One row of the per-project breakdown. No rates — this one counts."""

    project_id: int
    name: str
    slug: str
    content: int
    published: int
    views: int
    engagement: int


class TopContentOut(_Rates):
    """One of the best-performing individual pieces."""

    content_id: int
    title: str
    content_type: str
    project_id: int
    published_at: datetime | None = None
    read_minutes: int
    views: int
    reads: int
    clicks: int
    engagement: int
    reads_reported: int
    clicks_reported: int


class TimelinePointOut(BaseModel):
    """Publish events on one day."""

    #: ISO date. Every day in the window is present, including the empty ones.
    date: str
    publications: int


class PublishedPointOut(BaseModel):
    """Pieces published in one period.

    Distinct from :class:`TimelinePointOut`, which counts *publications*: a
    piece cross-posted to five platforms is five there and one here. That is
    the difference between "how much did Pulse send" and "how much did I
    publish", and they are different charts.
    """

    period: str = Field(
        description=(
            "ISO date. The day itself for a daily series; the Monday the week "
            "starts on for a weekly one. Every period in the window is "
            "present, including the empty ones."
        )
    )
    published: int


class GenerationCostPointOut(BaseModel):
    """Token spend on one day.

    Install-wide, not per-account: the usage table has no owner column. See
    :mod:`app.services.ops_metrics` for why, and note that on a multi-account
    install every caller sees the same series.
    """

    date: str
    calls: int
    total_tokens: int = Field(
        description=(
            "Tokens, which is what cost means here — Pulse runs on free tiers "
            "where quota is the scarce thing and there is no price table to "
            "multiply by."
        )
    )
    tokens_per_call: float | None = Field(
        default=None,
        description=(
            "Mean tokens per call, or null on a day with no calls. Separates a "
            "day that cost more because more was written from one where each "
            "generation got longer."
        ),
    )


class EngagementTrendPointOut(_Rates):
    """Reader activity *gained* on one day.

    Distinct from :class:`TimelinePointOut`, which counts publish events: these
    are day-over-day differences in the cumulative counters the platforms
    report, including on posts published long before the window opened.
    """

    date: str
    views: int
    reads: int
    clicks: int
    engagement: int
    #: That day's reads weighted by how long each piece takes to read.
    reader_minutes: int


class LengthBandOut(_Rates):
    """How posts of one length band performed."""

    band: str
    #: The band's upper bound in minutes. Null for the open-ended last one.
    max_read_minutes: int | None = None
    publications: int
    avg_read_minutes: float | None = None
    views: int
    reads: int
    clicks: int
    engagement: int
    reader_minutes: int
    avg_views: float | None = None


class ReadTimeOut(BaseModel):
    """Post length against performance."""

    published_pieces: int
    avg_read_minutes: float | None = None
    total_words: int
    #: Reading time actually spent, as far as the platforms will say: *reads*
    #: times reading time, never views. Counting a bounce's full reading time
    #: would invent attention nobody paid.
    reader_minutes: int
    #: Zero here usually means "nowhere you publish counts reads" rather than
    #: "nobody read it", which is why it is reported next to the number.
    publications_reporting_reads: int
    read_rate: float | None = None
    by_length: list[LengthBandOut] = []


class OverviewOut(BaseModel):
    """Everything the analytics page needs, in one round trip."""

    totals: TotalsOut
    by_content_type: list[ContentTypeStatsOut] = []
    by_platform: list[PlatformStatsOut] = []
    by_project: list[ProjectStatsOut] = []
    top_content: list[TopContentOut] = []
    timeline: list[TimelinePointOut] = []
    engagement_trend: list[EngagementTrendPointOut] = []
    read_time: ReadTimeOut


def _window_count_keys() -> tuple[str, str]:
    """The early and benchmark key names, as configured right now.

    Read at call time rather than frozen at import: the validator has to judge
    the payload the service is actually producing, and a process whose settings
    were changed under it should fail loudly rather than quietly accept keys it
    no longer documents.
    """
    return (
        window_count_key(int(settings.velocity_early_window_hours)),
        window_count_key(int(settings.velocity_benchmark_window_hours)),
    )


def _document_window_counts(schema: dict[str, Any]) -> None:
    """Add the two configuration-named counts to the generated schema.

    They cannot be declared as fields — their names come from
    ``VELOCITY_EARLY_WINDOW_HOURS`` and ``VELOCITY_BENCHMARK_WINDOW_HOURS``, so
    the class does not know them until an install is configured — but they are
    known by the time the schema is generated, which is the moment that matters
    for OpenAPI. Written in rather than left to ``additionalProperties`` so a
    generated client gets two typed, named, documented fields instead of a bag
    of ``Any``, and so ``/docs`` stops showing the velocity panel's entire
    content as an unexplained extra.
    """
    properties = schema.setdefault("properties", {})
    early, benchmark = _window_count_keys()
    for key, window in ((early, "early"), (benchmark, "benchmark")):
        # Left out of ``required`` by simply not being added to it: a curve too
        # young for a window carries the key with ``null`` in it, and one built
        # before the windows were widened may not carry it at all.
        properties[key] = {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "title": f"Views in the first {key.removeprefix('views_first_')}",
            "description": (
                f"Cumulative views over this publication's {window} window. "
                "Null when no metric snapshot lands inside the window — the "
                "post is not that old yet, or nothing polled it in time — "
                "which is unknown, not nought. The window is "
                f"`{window}_window_hours` on this same object, and the field "
                "name is built from it."
            ),
        }
    # The two above are the only extras there are. Saying so keeps a generated
    # client from typing the rest of the object as an open map.
    schema["additionalProperties"] = False


class VelocityCurveOut(BaseModel):
    """How one publication's audience arrived.

    Carries two fields this class cannot name: the early and benchmark view
    counts are keyed ``views_first_{hours}h``, and the hours come from
    ``VELOCITY_EARLY_WINDOW_HOURS`` and ``VELOCITY_BENCHMARK_WINDOW_HOURS`` —
    configuration, not schema. So ``extra="allow"`` keeps them rather than
    dropping them on the floor, which is what a strict model would do to a
    payload the frontend already reads.

    They are not renamed to something declarable because the key names are the
    API's existing contract. What has changed is that ``extra="allow"`` no
    longer means *anything*:

    * :func:`_document_window_counts` writes both names into the generated
      schema, so they are typed and described in OpenAPI rather than being an
      undocumented pair a client has to know about from somewhere else.
    * :meth:`_only_the_window_counts_may_ride_along` refuses any other extra.
      An open model on a response is a hole in both directions — a key the
      service starts producing reaches clients undocumented, and a key it
      *stops* producing goes unnoticed. Two specific names are allowed through;
      a third is a bug in whatever built the payload, and 500 is the honest
      answer to it.
    * ``early_window_hours`` and ``benchmark_window_hours`` are declared here as
      well as on the summary, so the key names are derivable from a single
      curve. Without them ``GET /analytics/velocity/{id}`` was a payload whose
      two most interesting fields could not be located without first fetching a
      different endpoint.
    """

    model_config = ConfigDict(
        extra="allow", json_schema_extra=_document_window_counts
    )

    publication_id: int
    content_id: int
    platform: str
    title: str
    published_at: datetime | None = None
    age_hours: float
    #: Metric snapshots behind this curve. One point is not a curve.
    snapshots: int
    views: int
    engagement: int
    views_per_day: float | None = None
    stalled: bool
    #: The two windows the ``views_first_{n}h`` keys are named for. Repeated on
    #: every curve rather than only on the summary so that one curve, on its
    #: own, says which keys it carries.
    early_window_hours: int
    benchmark_window_hours: int

    @model_validator(mode="after")
    def _only_the_window_counts_may_ride_along(self) -> VelocityCurveOut:
        allowed = set(_window_count_keys())
        unexpected = sorted(set(self.__pydantic_extra__ or {}) - allowed)
        if unexpected:
            raise ValueError(
                f"unexpected extra field(s) {unexpected} on a velocity curve; "
                f"the only extras this model allows are {sorted(allowed)}, "
                "which are named from the velocity window settings"
            )
        return self


class CurvePointOut(BaseModel):
    """One reading on a growth curve."""

    #: Hours since publication.
    hours: float
    views: int
    engagement: int


class VelocityCurveDetailOut(VelocityCurveOut):
    """A curve with the readings it was derived from, for the detail chart."""

    points: list[CurvePointOut] = []


class BenchmarkOut(BaseModel):
    """What a normal first day looks like on one platform, for this account."""

    platform: str
    early_window_hours: int
    benchmark_window_hours: int
    median_early_views: float | None = None
    median_benchmark_views: float | None = None
    #: Publications that supplied an observation for each window.
    early_sample: int
    benchmark_sample: int
    #: Below this the medians are still reported — they are interesting — but
    #: nothing is judged against them.
    reliable: bool


class VelocitySummaryOut(BaseModel):
    """The velocity panel: benchmarks, the fastest starts, the stalled posts."""

    early_window_hours: int
    benchmark_window_hours: int
    publications: int
    benchmarks: list[BenchmarkOut] = []
    fastest: list[VelocityCurveOut] = []
    stalled: list[VelocityCurveOut] = []


class AlertOut(BaseModel):
    """One thing worth looking at, and the numbers that justify saying so."""

    kind: str
    severity: str
    content_id: int
    publication_id: int
    platform: str
    title: str
    message: str
    #: Observed against expected, as a fraction. Null for alerts that are not a
    #: comparison against a benchmark.
    ratio: float | None = None
    observed: int | None = None
    expected: float | None = None


class AlertsOut(BaseModel):
    """Alerts plus the counts the dashboard badge needs."""

    alerts: list[AlertOut] = []
    warnings: int = 0
    notices: int = 0


class DigestMovementOut(BaseModel):
    """One week's metrics, against the week before."""

    views: int = 0
    clicks: int = 0
    engagement: int = 0
    previous_views: int = 0
    #: Views this window against the last, as a fraction. Null for a first
    #: week — rendering that as +100% would be an invention.
    change: float | None = None


class DigestPublishedOut(BaseModel):
    """A piece that went live during the window."""

    content_id: int
    title: str
    read_minutes: int
    platforms: list[str] = []
    url: str | None = None
    views: int


class DigestTopOut(BaseModel):
    """A best performer over the window, by views gained."""

    content_id: int
    title: str
    views: int


class DigestFailedOut(BaseModel):
    """A publication that gave up during the window."""

    content_id: int
    title: str
    platform: str
    #: Clipped by the digest builder; the full text is on the publication.
    error: str = ""


class DigestUpcomingOut(BaseModel):
    """Something on the calendar."""

    content_id: int
    title: str
    platform: str
    scheduled_for: datetime | None = None


class DigestOut(BaseModel):
    """The weekly digest as data — the same object the email is rendered from."""

    since: datetime
    until: datetime
    subject: str
    #: True when there is nothing worth an email. Alerts alone do not make a
    #: week non-empty: they are about posts published weeks ago.
    is_empty: bool
    movement: DigestMovementOut
    published: list[DigestPublishedOut] = []
    top: list[DigestTopOut] = []
    needs_review: int = 0
    failed: list[DigestFailedOut] = []
    upcoming: list[DigestUpcomingOut] = []
    attention: list[AlertOut] = []


class DigestSendOut(BaseModel):
    """What happened when the digest was asked to go out."""

    #: False is a normal answer, not a failure — an empty week is not mailed,
    #: and neither is anything when SMTP is unconfigured.
    sent: bool
    #: Which of those it was, so the UI need not guess. Empty on success.
    reason: str = ""
    #: The subject line that was sent, present only when there was one to send.
    subject: str | None = None


class DashboardFailedOut(BaseModel):
    """A publication that gave up, for the attention column."""

    id: int
    content_id: int
    platform: str
    error: str | None = None


class DashboardUpcomingOut(BaseModel):
    """Something on the calendar, for the attention column."""

    id: int
    content_id: int
    title: str
    platform: str
    scheduled_for: datetime | None = None


class DashboardRecentOut(BaseModel):
    """A recently created piece."""

    id: int
    title: str
    status: str
    content_type: str
    project_id: int
    project_name: str
    created_at: datetime


class DashboardOut(BaseModel):
    """The home page: counters, what needs attention, what just happened.

    Separate from :class:`OverviewOut` because the dashboard is loaded far more
    often and does not need the full per-type and per-platform breakdown.
    """

    totals: TotalsOut
    needs_review: int = 0
    failed_publications: list[DashboardFailedOut] = []
    upcoming: list[DashboardUpcomingOut] = []
    recent_content: list[DashboardRecentOut] = []
    by_project: list[ProjectStatsOut] = []
    timeline: list[TimelinePointOut] = []
    #: Capped tighter than ``GET /analytics/alerts``: this is the "what needs
    #: attention" column, not the full list.
    alerts: list[AlertOut] = []


__all__ = [
    "AlertOut",
    "AlertsOut",
    "BenchmarkOut",
    "ContentTypeStatsOut",
    "CurvePointOut",
    "DashboardFailedOut",
    "DashboardOut",
    "DashboardRecentOut",
    "DashboardUpcomingOut",
    "DigestFailedOut",
    "DigestMovementOut",
    "DigestOut",
    "DigestPublishedOut",
    "DigestSendOut",
    "DigestTopOut",
    "DigestUpcomingOut",
    "EngagementTrendPointOut",
    "LengthBandOut",
    "OverviewOut",
    "PlatformStatsOut",
    "ProjectStatsOut",
    "ReadTimeOut",
    "TimelinePointOut",
    "TopContentOut",
    "TotalsOut",
    "VelocityCurveDetailOut",
    "VelocityCurveOut",
    "VelocitySummaryOut",
]
