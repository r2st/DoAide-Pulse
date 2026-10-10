"""M17 Data Integrity: CHECK constraints, audit columns, and race-condition guards."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.exc import IntegrityError

from app.models.content import Content, ContentStatus, ContentType
from app.models.llm_usage import LLMUsage
from app.models.metrics import ContentMetric
from app.models.mixins import utcnow
from app.models.preview_link import PreviewLink
from app.models.project import Project, Tone
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.subscriber import Subscriber
from app.models.template import ContentTemplate, TemplateMode
from app.models.trigger import Trigger
from app.models.webhook import Webhook, WebhookDelivery


# ---------------------------------------------------------------------------
# CHECK constraints: non-negative counters
# ---------------------------------------------------------------------------

class TestCheckConstraints:
    """Database-level CHECK constraints reject invalid data."""

    def test_content_negative_word_count_rejected(self, db, user):
        project = Project(
            user_id=user.id, name="p", slug="p", tone=Tone.TECHNICAL,
        )
        db.add(project)
        db.flush()
        c = Content(
            project_id=project.id, title="t", slug="s",
            content_type=ContentType.TUTORIAL, status=ContentStatus.DRAFT,
        )
        db.add(c)
        db.flush()
        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE content SET word_count = :v WHERE id = :id"),
                {"v": -1, "id": c.id},
            )
        db.rollback()

    def test_content_confidence_out_of_range_rejected(self, db, user):
        project = Project(
            user_id=user.id, name="p", slug="p", tone=Tone.TECHNICAL,
        )
        db.add(project)
        db.flush()
        c = Content(
            project_id=project.id, title="t", slug="s",
            content_type=ContentType.TUTORIAL, status=ContentStatus.DRAFT,
        )
        db.add(c)
        db.flush()
        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE content SET confidence = :v WHERE id = :id"),
                {"v": 1.5, "id": c.id},
            )
        db.rollback()

    def test_publication_negative_attempts_rejected(self, db, user):
        project = Project(
            user_id=user.id, name="p", slug="p", tone=Tone.TECHNICAL,
        )
        db.add(project)
        db.flush()
        c = Content(
            project_id=project.id, title="t", slug="s",
            content_type=ContentType.TUTORIAL, status=ContentStatus.DRAFT,
        )
        db.add(c)
        db.flush()
        pub = Publication(content_id=c.id, platform=Platform.DEVTO)
        db.add(pub)
        db.flush()
        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE publications SET attempts = :v WHERE id = :id"),
                {"v": -1, "id": pub.id},
            )
        db.rollback()

    def test_project_negative_scan_count_rejected(self, db, user):
        project = Project(
            user_id=user.id, name="p", slug="p", tone=Tone.TECHNICAL,
        )
        db.add(project)
        db.flush()
        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE projects SET scan_count = :v WHERE id = :id"),
                {"v": -1, "id": project.id},
            )
        db.rollback()

    def test_project_negative_engagement_threshold_rejected(self, db, user):
        project = Project(
            user_id=user.id, name="p", slug="p", tone=Tone.TECHNICAL,
        )
        db.add(project)
        db.flush()
        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE projects SET engagement_threshold = :v WHERE id = :id"),
                {"v": -1, "id": project.id},
            )
        db.rollback()

    def test_llm_usage_negative_duration_rejected(self, db):
        row = LLMUsage(provider="test", model="m", duration_ms=0)
        db.add(row)
        db.flush()
        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE llm_usage SET duration_ms = :v WHERE id = :id"),
                {"v": -1, "id": row.id},
            )
        db.rollback()

    def test_preview_link_negative_view_count_rejected(self, db, user):
        project = Project(
            user_id=user.id, name="p", slug="p", tone=Tone.TECHNICAL,
        )
        db.add(project)
        db.flush()
        c = Content(
            project_id=project.id, title="t", slug="s",
            content_type=ContentType.TUTORIAL, status=ContentStatus.DRAFT,
        )
        db.add(c)
        db.flush()
        link = PreviewLink(
            content_id=c.id, token_hash="a" * 64, expires_at=utcnow(),
        )
        db.add(link)
        db.flush()
        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE preview_links SET view_count = :v WHERE id = :id"),
                {"v": -1, "id": link.id},
            )
        db.rollback()

    def test_template_negative_use_count_rejected(self, db, user):
        t = ContentTemplate(
            user_id=user.id, name="tmpl", mode=TemplateMode.LITERAL,
            content_type=ContentType.ANNOUNCEMENT,
        )
        db.add(t)
        db.flush()
        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE content_templates SET use_count = :v WHERE id = :id"),
                {"v": -1, "id": t.id},
            )
        db.rollback()


# ---------------------------------------------------------------------------
# Audit trail: TimestampMixin on every model
# ---------------------------------------------------------------------------

class TestAuditTrail:
    """Every persistent model carries created_at and updated_at."""

    MODELS_WITH_TIMESTAMPS = [
        Content, Publication, Project, Webhook, WebhookDelivery,
        Trigger, ContentTemplate, PreviewLink, Subscriber,
        ContentMetric,
    ]

    @pytest.mark.parametrize(
        "model", MODELS_WITH_TIMESTAMPS, ids=lambda m: m.__tablename__
    )
    def test_model_has_audit_columns(self, model):
        mapper = sa_inspect(model)
        col_names = {c.key for c in mapper.column_attrs}
        assert "created_at" in col_names, f"{model.__tablename__} missing created_at"
        assert "updated_at" in col_names, f"{model.__tablename__} missing updated_at"


# ---------------------------------------------------------------------------
# Engagement-alert latch is atomic
# ---------------------------------------------------------------------------

class TestEngagementAlertLatch:
    """The engagement notification fires at most once per piece."""

    def test_latch_prevents_double_notification(self, db, user):
        """Simulates two sweeps both finding the same piece above threshold."""
        from app.services import engagement_alerts

        project = Project(
            user_id=user.id, name="p", slug="p", tone=Tone.TECHNICAL,
            engagement_threshold=5,
        )
        db.add(project)
        db.flush()
        c = Content(
            project_id=project.id, title="t", slug="s",
            content_type=ContentType.TUTORIAL,
            status=ContentStatus.PUBLISHED,
            published_at=utcnow(),
        )
        db.add(c)
        db.flush()
        pub = Publication(
            content_id=c.id, platform=Platform.DEVTO,
            status=PublicationStatus.PUBLISHED,
            published_at=utcnow(),
        )
        db.add(pub)
        db.flush()
        metric = ContentMetric(
            publication_id=pub.id, views=100, reactions=10, comments=5,
        )
        db.add(metric)
        db.commit()

        first = engagement_alerts.evaluate(db, [c.id])
        assert len(first) == 1

        second = engagement_alerts.evaluate(db, [c.id])
        assert len(second) == 0, "second sweep must not re-fire"


# ---------------------------------------------------------------------------
# Subscriber model now has optional project_id
# ---------------------------------------------------------------------------

class TestSubscriberProjectLink:
    """Subscriber rows can optionally link to a project."""

    def test_subscriber_without_project(self, db):
        sub = Subscriber(email="a@b.com")
        db.add(sub)
        db.commit()
        db.refresh(sub)
        assert sub.project_id is None

    def test_subscriber_with_project(self, db, user):
        project = Project(
            user_id=user.id, name="p", slug="p", tone=Tone.TECHNICAL,
        )
        db.add(project)
        db.flush()
        sub = Subscriber(email="c@d.com", project_id=project.id)
        db.add(sub)
        db.commit()
        db.refresh(sub)
        assert sub.project_id == project.id
