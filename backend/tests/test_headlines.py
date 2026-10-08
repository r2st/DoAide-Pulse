"""Headline variants, applying a swap, and attributing engagement to windows."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import headlines, llm_router


def _content(db, project, **overrides) -> Content:
    defaults = dict(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Pulse ships bulk operations",
        slug="pulse-ships-bulk-operations",
        excerpt="Approve, reject or publish many drafts in one call.",
        focus_keyword="bulk content operations",
    )
    defaults.update(overrides)
    row = Content(**defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def stub_llm(monkeypatch):
    state = {"payload": None}

    def fake_complete(messages, *, model=None, **_kwargs):
        text = state["payload"]
        return llm_router.Completion(
            text=text if isinstance(text, str) else json.dumps(text),
            provider="stub",
            model=model or "stub-model",
        )

    monkeypatch.setattr(llm_router, "complete", fake_complete)
    return state


# --------------------------------------------------------------------------- #
# Generating variants                                                         #
# --------------------------------------------------------------------------- #


def test_falls_back_to_templates_without_a_provider(project, db):
    content = _content(db, project)
    result = headlines.generate_variants(content, project)

    assert result.is_fallback is True
    assert result.provider is None
    assert result.variants
    assert content.title not in result.variants


def test_uses_ai_variants_when_the_chain_succeeds(project, db, stub_llm):
    stub_llm["payload"] = {
        "variants": [
            "Bulk Content Ops: Approve, Reject or Publish in One Click",
            "How Pulse Handles Bulk Approvals",
            "Why We Added Bulk Content Operations",
        ]
    }
    content = _content(db, project)

    result = headlines.generate_variants(content, project, count=3)

    assert result.is_fallback is False
    assert result.provider == "stub"
    assert len(result.variants) == 3


def test_dedupes_and_drops_a_reasoning_leak(project, db, stub_llm):
    stub_llm["payload"] = {
        "variants": [
            "Pulse ships bulk operations",  # identical to the current title
            "We need to write a headline about this feature first.",  # reasoning leak
            "Bulk Approvals, Explained",
            "Bulk Approvals, Explained",  # duplicate within the response
        ]
    }
    content = _content(db, project)

    result = headlines.generate_variants(content, project, count=4)

    assert result.variants == ["Bulk Approvals, Explained"]


def test_empty_ai_response_falls_back_to_templates(project, db, stub_llm):
    stub_llm["payload"] = {"variants": []}
    content = _content(db, project)

    result = headlines.generate_variants(content, project)

    assert result.is_fallback is True
    assert result.variants


def test_a_maximum_length_title_never_comes_back_as_its_own_variant(project, db):
    """At 300 characters the suffix templates truncate back into the title.

    300 is the ceiling on both ``Content.title`` and the schema, so this is a
    title the API accepts, not a synthetic one. ``{title}: What You Need to
    Know`` clipped to 300 characters is exactly the title again, and offering
    the author their own headline back as an "alternative" is the one thing the
    fallback is not allowed to do. The prefix templates still have something to
    say, so the list stays useful rather than going empty.
    """
    title = ("Pulse ships bulk operations and this headline keeps going " * 6)[:300]
    assert len(title) == 300
    content = _content(db, project, title=title, slug="a-very-long-title")

    result = headlines.generate_variants(content, project)

    assert result.is_fallback is True
    assert title not in result.variants
    assert not any(v == title for v in result.variants)
    assert result.variants, "the prefix templates must still yield something"
    assert all(v.startswith(("Why ", "The Case for ")) for v in result.variants)


# --------------------------------------------------------------------------- #
# Applying a headline                                                         #
# --------------------------------------------------------------------------- #


def test_apply_headline_opens_and_closes_contiguous_windows(project, db):
    content = _content(db, project)
    original_title = content.title
    original_created = content.created_at

    headlines.apply_headline(content, "New Headline One")
    db.commit()
    db.refresh(content)

    assert content.title == "New Headline One"
    assert len(content.headline_history) == 1
    first = content.headline_history[0]
    assert first["title"] == original_title
    assert first["started_at"] == original_created.isoformat()
    assert first["ended_at"]

    headlines.apply_headline(content, "New Headline Two")
    db.commit()
    db.refresh(content)

    assert content.title == "New Headline Two"
    assert len(content.headline_history) == 2
    second = content.headline_history[1]
    assert second["title"] == "New Headline One"
    # Contiguous: the second window starts exactly where the first ended.
    assert second["started_at"] == first["ended_at"]


def test_apply_headline_leaves_the_slug_untouched(project, db):
    content = _content(db, project, status=ContentStatus.PUBLISHED)
    original_slug = content.slug

    headlines.apply_headline(content, "A Completely Different Headline")
    db.commit()
    db.refresh(content)

    assert content.slug == original_slug


# --------------------------------------------------------------------------- #
# Performance attribution                                                     #
# --------------------------------------------------------------------------- #


def test_performance_is_a_single_open_window_before_any_swap(project, db):
    content = _content(db, project)

    windows = headlines.performance(content, db)

    assert len(windows) == 1
    assert windows[0].current is True
    assert windows[0].title == content.title
    assert windows[0].ended_at is None
    assert windows[0].snapshots == 0


def test_performance_attributes_metrics_to_the_headline_that_was_live(project, db):
    content = _content(db, project, status=ContentStatus.PUBLISHED)
    publication = Publication(
        content_id=content.id, platform=Platform.DEVTO, status=PublicationStatus.PUBLISHED
    )
    db.add(publication)
    db.commit()
    db.refresh(publication)

    before = ContentMetric(
        # Exactly the window's start, not offset forward — the swap that
        # closes this window happens moments from now, in real wall-clock
        # time, so anything added here risks landing *after* it.
        publication_id=publication.id,
        captured_at=content.created_at,
        views=100,
        reactions=5,
        comments=1,
    )
    db.add(before)
    db.commit()

    headlines.apply_headline(content, "New Headline")
    db.commit()
    db.refresh(content)

    # Platform counters are cumulative, so this reads as 50 more views and two
    # more interactions than the previous poll — not as a post that lost half
    # its audience.
    after = ContentMetric(
        publication_id=publication.id,
        captured_at=datetime.now(UTC) + timedelta(hours=1),
        views=150,
        reactions=7,
        comments=1,
        shares=1,
    )
    db.add(after)
    db.commit()

    windows = headlines.performance(content, db)

    assert len(windows) == 2
    original, current = windows
    assert original.current is False
    assert original.views == 100
    assert original.engagement == 6  # reactions(5) + comments(1)
    assert original.snapshots == 1

    assert current.current is True
    assert current.title == "New Headline"
    # What the new headline *added*, not the running total it inherited.
    assert current.views == 50
    assert current.engagement == 3  # reactions +2, shares +1
    assert current.snapshots == 1
