"""A failed translation stores a safe error, not the LLM provider's raw reply.

The ``AllProvidersFailed`` message aggregates every provider's error string,
which can include response bodies that echo API keys or expose internal
infrastructure (model names, provider URLs, rate-limit details).  The error
is stored on ``ContentTranslation.error`` and returned to the user through
``TranslationOut.error`` — so it must be sanitised before it is written.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.translation import ContentTranslation, TranslationStatus
from app.services import ai, translation


ENGLISH_BODY = (
    "# Publishing with Pulse\n"
    "\n"
    "Pulse turns your repository activity into finished posts. Connect a "
    "GitHub account, choose the repository you want watched, and every release "
    "becomes a complete draft waiting for your review.\n"
)


@pytest.fixture
def content(db, project):
    c = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        title="Testing translations",
        slug="testing-translations",
        body_markdown=ENGLISH_BODY,
        status=ContentStatus.DRAFT,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_provider_error_is_sanitised_before_storage(db, content, monkeypatch):
    """str(AIError) can carry API keys echoed by the provider — only the type
    name should reach the row.
    """
    api_key = "sk-or-v1-s3cretToken12345678901234567890"
    raw_msg = (
        f"All LLM providers failed: OpenRouter returned 401: "
        f"invalid API key: {api_key}"
    )

    def _boom(*a, **kw):
        raise ai.AIError(raw_msg)

    monkeypatch.setattr(ai, "json_completion", _boom)

    with pytest.raises(translation.TranslationUnavailable):
        translation.translate(db, content, "fr")

    row = (
        db.query(ContentTranslation)
        .filter_by(content_id=content.id, language="fr")
        .one()
    )
    assert row.status == TranslationStatus.FAILED
    assert api_key not in row.error
    assert "Internal error" in row.error
