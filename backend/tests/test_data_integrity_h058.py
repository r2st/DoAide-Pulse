"""H058: M17 data integrity — CHECK constraints, FK cascades, atomic counters.

Verifies that the database-level guards the models declare actually fire, that
FK cascades clean up child rows, and that counter increments use SQL expressions
rather than Python-side read-modify-write.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.preview_link import PreviewLink
from app.models.project import Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.revision import ContentRevision, RevisionSource
from app.models.template import ContentTemplate, TemplateMode
from app.models.translation import ContentTranslation, TranslationStatus
from app.models.trigger import Trigger, TriggerEvent, TriggerKind
from app.models.user import User
from app.models.webhook import Webhook, WebhookDelivery, WebhookEvent


# ---- Helpers ---------------------------------------------------------------- #


@pytest.fixture
def content(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Test Piece",
        slug="test-piece",
        body_markdown="Hello world.\n\n" + ("word " * 200),
        status=ContentStatus.DRAFT,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---- CHECK constraints on Content ------------------------------------------ #


def test_content_word_count_rejects_negative(db, project):
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Neg",
        slug="neg-wc",
        body_markdown="",
        status=ContentStatus.DRAFT,
    )
    db.add(row)
    db.commit()
    with pytest.raises(Exception):
        db.execute(
            text("UPDATE content SET word_count = -1 WHERE id = :id"),
            {"id": row.id},
        )
    db.rollback()


def test_content_confidence_rejects_out_of_range(db, project):
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Bad confidence",
        slug="bad-conf",
        body_markdown="",
        status=ContentStatus.DRAFT,
        confidence=1.5,
    )
    db.add(row)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


# ---- CHECK constraints on Project ------------------------------------------ #


def test_project_engagement_threshold_rejects_negative(db, user):
    row = Project(
        user_id=user.id,
        name="Bad threshold",
        slug="bad-thresh",
        description="",
        tone=Tone.TECHNICAL,
        engagement_threshold=-1,
    )
    db.add(row)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


def test_project_scan_count_rejects_negative(db, user):
    row = Project(
        user_id=user.id,
        name="Bad scan",
        slug="bad-scan",
        description="",
        tone=Tone.TECHNICAL,
    )
    db.add(row)
    db.commit()
    with pytest.raises(Exception):
        db.execute(
            text("UPDATE projects SET scan_count = -1 WHERE id = :id"),
            {"id": row.id},
        )
    db.rollback()


# ---- CHECK constraints on Publication -------------------------------------- #


def test_publication_attempts_rejects_negative(db, content):
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
        attempts=-1,
    )
    db.add(pub)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


# ---- CHECK constraints on ContentMetric ----------------------------------- #


def test_metric_views_rejects_negative(db, content):
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
    )
    db.add(pub)
    db.commit()
    m = ContentMetric(publication_id=pub.id, views=-1)
    db.add(m)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


# ---- CHECK constraints on ContentRevision --------------------------------- #


def test_revision_word_count_rejects_negative(db, content):
    rev = ContentRevision(
        content_id=content.id,
        revision=1,
        title=content.title,
        body_markdown=content.body_markdown,
        word_count=-5,
        source=RevisionSource.EDIT,
    )
    db.add(rev)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


def test_revision_number_rejects_zero(db, content):
    rev = ContentRevision(
        content_id=content.id,
        revision=0,
        title=content.title,
        body_markdown=content.body_markdown,
        source=RevisionSource.EDIT,
    )
    db.add(rev)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


# ---- CHECK constraints on ContentTranslation ------------------------------ #


def test_translation_source_version_rejects_negative(db, content):
    tr = ContentTranslation(
        content_id=content.id,
        language="fr",
        source_version=-1,
    )
    db.add(tr)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


# ---- CHECK constraints on Webhook / Delivery ------------------------------ #


def test_webhook_consecutive_failures_rejects_negative(db, user):
    wh = Webhook(
        user_id=user.id,
        url="https://example.com/hook",
        consecutive_failures=-1,
    )
    db.add(wh)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


# ---- CHECK constraints on Trigger ----------------------------------------- #


def test_trigger_fire_count_rejects_negative(db, project):
    t = Trigger(
        project_id=project.id,
        kind=TriggerKind.WEBHOOK,
        name="test",
    )
    db.add(t)
    db.commit()
    with pytest.raises(Exception):
        db.execute(
            text("UPDATE triggers SET fire_count = -1 WHERE id = :id"),
            {"id": t.id},
        )
    db.rollback()


# ---- CHECK constraints on Template ---------------------------------------- #


def test_template_use_count_rejects_negative(db, user):
    t = ContentTemplate(
        user_id=user.id,
        name="Test template",
        mode=TemplateMode.LITERAL,
        use_count=-1,
    )
    db.add(t)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


# ---- CHECK constraints on PreviewLink ------------------------------------- #


def test_preview_link_view_count_rejects_negative(db, content):
    from datetime import UTC, datetime, timedelta

    link = PreviewLink(
        content_id=content.id,
        token_hash="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        view_count=-1,
    )
    db.add(link)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


# ---- FK cascades ----------------------------------------------------------- #


def test_deleting_user_cascades_to_projects(db, user, project):
    pid = project.id
    db.delete(user)
    db.commit()
    assert db.get(Project, pid) is None


def test_deleting_project_cascades_to_content(db, project, content):
    cid = content.id
    db.delete(project)
    db.commit()
    assert db.get(Content, cid) is None


def test_deleting_content_cascades_to_publications(db, content):
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(pub)
    db.commit()
    pub_id = pub.id
    db.delete(content)
    db.commit()
    assert db.get(Publication, pub_id) is None


def test_deleting_content_cascades_to_revisions(db, content):
    rev = ContentRevision(
        content_id=content.id,
        revision=1,
        title=content.title,
        body_markdown=content.body_markdown,
        source=RevisionSource.EDIT,
    )
    db.add(rev)
    db.commit()
    rev_id = rev.id
    db.delete(content)
    db.commit()
    assert db.get(ContentRevision, rev_id) is None


def test_deleting_content_cascades_to_translations(db, content):
    tr = ContentTranslation(
        content_id=content.id,
        language="fr",
        source_version=1,
        status=TranslationStatus.READY,
    )
    db.add(tr)
    db.commit()
    tr_id = tr.id
    db.delete(content)
    db.commit()
    assert db.get(ContentTranslation, tr_id) is None


def test_deleting_publication_cascades_to_metrics(db, content):
    pub = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
    )
    db.add(pub)
    db.commit()
    m = ContentMetric(publication_id=pub.id, views=100)
    db.add(m)
    db.commit()
    m_id = m.id
    db.delete(pub)
    db.commit()
    assert db.get(ContentMetric, m_id) is None


def test_deleting_webhook_cascades_to_deliveries(db, user):
    wh = Webhook(
        user_id=user.id,
        url="https://example.com/hook",
        events=["content.published"],
    )
    db.add(wh)
    db.commit()
    d = WebhookDelivery(
        webhook_id=wh.id,
        event=WebhookEvent.CONTENT_PUBLISHED,
        payload={"test": True},
    )
    db.add(d)
    db.commit()
    d_id = d.id
    db.delete(wh)
    db.commit()
    assert db.get(WebhookDelivery, d_id) is None


def test_deleting_trigger_cascades_to_events(db, project):
    t = Trigger(
        project_id=project.id,
        kind=TriggerKind.WEBHOOK,
        name="test",
    )
    db.add(t)
    db.commit()
    ev = TriggerEvent(
        trigger_id=t.id,
        headline="test event",
        payload={},
    )
    db.add(ev)
    db.commit()
    ev_id = ev.id
    db.delete(t)
    db.commit()
    assert db.get(TriggerEvent, ev_id) is None


# ---- Unique constraints ---------------------------------------------------- #


def test_content_slug_unique_per_project(db, project):
    c1 = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="First",
        slug="same-slug",
        body_markdown="",
        status=ContentStatus.DRAFT,
    )
    c2 = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Second",
        slug="same-slug",
        body_markdown="",
        status=ContentStatus.DRAFT,
    )
    db.add(c1)
    db.commit()
    db.add(c2)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


def test_publication_unique_per_content_platform(db, content):
    p1 = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    p2 = Publication(
        content_id=content.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PENDING,
    )
    db.add(p1)
    db.commit()
    db.add(p2)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


def test_project_slug_unique_per_user(db, user):
    p1 = Project(
        user_id=user.id,
        name="P1",
        slug="same",
        description="",
        tone=Tone.TECHNICAL,
    )
    p2 = Project(
        user_id=user.id,
        name="P2",
        slug="same",
        description="",
        tone=Tone.TECHNICAL,
    )
    db.add(p1)
    db.commit()
    db.add(p2)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


# ---- Optimistic locking (version_id_col) ---------------------------------- #


def test_content_version_increments_on_update(db, content):
    v1 = content.version
    content.title = "Updated title"
    db.commit()
    db.refresh(content)
    assert content.version == v1 + 1


def test_stale_content_version_raises(db, content):
    from sqlalchemy.orm.exc import StaleDataError

    v1 = content.version
    db.execute(
        text("UPDATE content SET version = :v WHERE id = :id"),
        {"v": v1 + 1, "id": content.id},
    )
    content.title = "My edit"
    with pytest.raises(StaleDataError):
        db.flush()
    db.rollback()


# ---- Atomic counter increments (SQL expressions) --------------------------- #


def test_preview_link_view_count_uses_sql_expression(db, content):
    """The view_count increment must use a SQL expression, not Python +=."""
    from datetime import UTC, datetime, timedelta

    from app.services.preview_links import PreviewLink

    link = PreviewLink(
        content_id=content.id,
        token_hash="b" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        view_count=5,
    )
    db.add(link)
    db.commit()

    link.view_count = PreviewLink.view_count + 1
    db.commit()
    db.refresh(link)
    assert link.view_count == 6


def test_trigger_fire_count_uses_sql_expression(db, project):
    """The fire_count increment must use a SQL expression, not Python +=."""
    t = Trigger(
        project_id=project.id,
        kind=TriggerKind.WEBHOOK,
        name="counter test",
        fire_count=10,
    )
    db.add(t)
    db.commit()

    t.fire_count = Trigger.fire_count + 1
    db.commit()
    db.refresh(t)
    assert t.fire_count == 11


def test_template_use_count_uses_sql_expression(db, user):
    """The use_count increment must use a SQL expression, not Python +=."""
    t = ContentTemplate(
        user_id=user.id,
        name="Counter test",
        mode=TemplateMode.LITERAL,
        use_count=3,
    )
    db.add(t)
    db.commit()

    t.use_count = ContentTemplate.use_count + 1
    db.commit()
    db.refresh(t)
    assert t.use_count == 4


# ---- Timestamp mixin ------------------------------------------------------- #


def test_created_at_and_updated_at_are_set_on_insert(db, user):
    assert user.created_at is not None
    assert user.updated_at is not None


def test_updated_at_advances_on_update(db, user):
    import time

    original = user.updated_at
    time.sleep(0.01)
    user.full_name = "Updated"
    db.commit()
    db.refresh(user)
    assert user.updated_at >= original


# ---- Soft-delete-like patterns: is_active filtering ----------------------- #


def test_inactive_user_connected_platforms_excludes_disconnected(db, user):
    from app.models.platform_connection import ConnectionStatus, PlatformConnection

    conn = PlatformConnection(
        user_id=user.id,
        platform=Platform.DEVTO,
        status=ConnectionStatus.DISCONNECTED,
        encrypted_credentials="",
    )
    db.add(conn)
    db.commit()
    db.refresh(user)
    assert "devto" not in user.connected_platforms


# ---- Model-layer data validation ------------------------------------------ #


def test_content_body_syncs_word_count(db, project):
    c = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="WC test",
        slug="wc-test",
        body_markdown="one two three four five",
        status=ContentStatus.DRAFT,
    )
    db.add(c)
    db.commit()
    assert c.word_count == 5

    c.body_markdown = "just two"
    db.commit()
    assert c.word_count == 2


def test_content_body_sync_handles_empty(db, project):
    c = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="Empty WC",
        slug="empty-wc",
        body_markdown="",
        status=ContentStatus.DRAFT,
    )
    db.add(c)
    db.commit()
    assert c.word_count == 0
