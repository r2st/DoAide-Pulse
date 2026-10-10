"""The signal a trigger produces must survive the round trip to a prompt and a row.

:class:`~app.services.signals.TriggerSignal` is the normalized shape every
trigger kind collapses to. ``digest()`` turns it into prompt text and
``event_payload()`` turns it into the JSON stored on the event row. Both must
preserve bounds (the summary limit, the item cap) and both must be stable: the
same signal must produce the same payload regardless of when or how often it is
called.
"""
from __future__ import annotations

from app.models.content import ContentType
from app.models.trigger import TriggerKind
from datetime import UTC, datetime

from app.services.github_client import Commit, Release, RepoActivity
from app.services.signals import (
    DEFAULT_MAX_ITEMS,
    SUMMARY_LIMIT,
    TriggerSignal,
    digest_key,
    from_repo_activity,
)


def _repo_activity(*, commits: int = 0, release: bool = False) -> RepoActivity:
    return RepoActivity(
        full_name="r2st/DoAide-Pulse",
        new_commits=[
            Commit(
                sha=f"sha{i}",
                message=f"feat: thing {i}\n\nbody",
                author="r2st",
                committed_at=datetime(2026, 7, 1, tzinfo=UTC),
                url="https://github.com/r2st/DoAide-Pulse/commit/x",
            )
            for i in range(commits)
        ],
        new_release=(
            Release(
                tag="v1.2.0",
                name="Calendar drag-and-drop",
                body="- Drag to reschedule\n- SEO panel",
                published_at=datetime(2026, 7, 20, tzinfo=UTC),
                url="https://github.com/r2st/DoAide-Pulse/releases/v1.2.0",
                prerelease=False,
            )
            if release
            else None
        ),
        head_sha="abc123",
        latest_tag="v1.2.0" if release else None,
    )


def _signal(**overrides) -> TriggerSignal:
    defaults = {
        "kind": TriggerKind.GITHUB,
        "source": "GitHub test/repo",
        "headline": "3 new commits",
    }
    defaults.update(overrides)
    return TriggerSignal(**defaults)


class TestDigest:
    def test_headline_only(self):
        s = _signal(headline="Release v2.0")
        assert s.digest() == "Release v2.0"

    def test_summary_is_truncated_to_limit(self):
        long_summary = "x" * (SUMMARY_LIMIT + 500)
        s = _signal(summary=long_summary)
        digest = s.digest()
        assert len(long_summary[:SUMMARY_LIMIT]) <= len(digest)
        assert "x" * (SUMMARY_LIMIT + 1) not in digest

    def test_items_are_capped_at_max_items(self):
        items = tuple(f"commit {i}" for i in range(DEFAULT_MAX_ITEMS + 10))
        s = _signal(items=items, item_noun="commit")
        digest = s.digest()
        assert "most recent" in digest
        assert f"- commit {DEFAULT_MAX_ITEMS - 1}" in digest
        assert f"- commit {DEFAULT_MAX_ITEMS}" not in digest

    def test_custom_max_items(self):
        items = tuple(f"item {i}" for i in range(10))
        s = _signal(items=items)
        digest = s.digest(max_items=3)
        assert "most recent" in digest
        lines = [l for l in digest.splitlines() if l.startswith("- ")]
        assert len(lines) == 3

    def test_url_appears_at_the_end(self):
        s = _signal(url="https://github.com/test/repo/releases/v1")
        digest = s.digest()
        assert digest.endswith("Source: https://github.com/test/repo/releases/v1")

    def test_empty_signal_produces_empty_digest(self):
        s = _signal(headline="", summary="", items=())
        assert s.digest() == ""
        assert not s.has_news

    def test_whitespace_only_headline_is_not_news(self):
        s = _signal(headline="   ", summary="  ", items=())
        assert not s.has_news
        assert s.digest() == ""


class TestEventPayload:
    def test_payload_carries_all_fields(self):
        s = _signal(
            summary="Release notes here",
            items=("fix A", "fix B"),
            item_noun="fix",
            url="https://example.com",
            dedupe_key="abc123",
            suggested_type=ContentType.ANNOUNCEMENT,
            raw={"tag": "v1.0"},
        )
        p = s.event_payload()
        assert p["kind"] == TriggerKind.GITHUB.value
        assert p["source"] == "GitHub test/repo"
        assert p["headline"] == "3 new commits"
        assert p["summary"] == "Release notes here"
        assert p["items"] == ["fix A", "fix B"]
        assert p["item_noun"] == "fix"
        assert p["url"] == "https://example.com"
        assert p["dedupe_key"] == "abc123"
        assert p["suggested_type"] == ContentType.ANNOUNCEMENT.value
        assert p["raw"] == {"tag": "v1.0"}

    def test_payload_truncates_summary(self):
        long = "y" * (SUMMARY_LIMIT + 100)
        p = _signal(summary=long).event_payload()
        assert len(p["summary"]) == SUMMARY_LIMIT

    def test_payload_truncates_items(self):
        items = tuple(f"i{n}" for n in range(DEFAULT_MAX_ITEMS + 5))
        p = _signal(items=items).event_payload()
        assert len(p["items"]) == DEFAULT_MAX_ITEMS


class TestDigestKey:
    def test_deterministic(self):
        assert digest_key("a", "b") == digest_key("a", "b")

    def test_different_inputs_differ(self):
        assert digest_key("a", "b") != digest_key("a", "c")

    def test_order_matters(self):
        assert digest_key("a", "b") != digest_key("b", "a")

    def test_empty_part_is_handled(self):
        k = digest_key("release", "")
        assert isinstance(k, str)
        assert len(k) == 40

    def test_length_is_fixed(self):
        assert len(digest_key("x" * 1000, "y" * 1000)) == 40


class TestFromRepoActivity:
    def test_release_is_an_announcement(self):
        activity = _repo_activity(release=True)
        signal = from_repo_activity(activity)
        assert signal.suggested_type is ContentType.ANNOUNCEMENT
        assert "v1.2.0" in signal.headline
        assert signal.dedupe_key is not None

    def test_commits_are_a_feature_spotlight(self):
        activity = _repo_activity(commits=5)
        signal = from_repo_activity(activity)
        assert signal.suggested_type is ContentType.FEATURE_SPOTLIGHT
        assert "5 new commit" in signal.headline
        assert signal.item_noun == "commit"
        assert len(signal.items) == 5

    def test_empty_activity_has_no_news(self):
        activity = _repo_activity(commits=0, release=False)
        signal = from_repo_activity(activity)
        assert not signal.has_news
        assert signal.dedupe_key is None

    def test_custom_source_label(self):
        activity = _repo_activity(commits=1)
        signal = from_repo_activity(activity, source="Custom Label")
        assert signal.source == "Custom Label"

    def test_default_source_uses_full_name(self):
        activity = _repo_activity(commits=1)
        signal = from_repo_activity(activity)
        assert "r2st/DoAide-Pulse" in signal.source
