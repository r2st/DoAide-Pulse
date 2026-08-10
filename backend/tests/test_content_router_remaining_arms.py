"""Three content-router decisions that only show up on the second-most-common input.

* Editing keywords **and** the focus keyword in one PATCH. The sync exists so a
  caller that sends only keywords gets a sensible focus; it must not then
  overwrite a focus the caller sent deliberately.
* A project with ``auto_canonical`` switched off. The canonical platform is what
  tells every other platform where the original lives, and inventing one for a
  project that asked for none puts a ``rel=canonical`` on somebody's blog
  pointing at a copy.
* Scheduling a piece that is already out on one platform. The already-published
  publication must be left where it is — moving it back to ``scheduled`` would
  re-publish something that is on the internet.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.template import ContentTemplate

API = "/api/v1/content"


@pytest.fixture
def piece(db, project):
    row = Content(
        project_id=project.id,
        content_type=ContentType.TUTORIAL,
        status=ContentStatus.APPROVED,
        title="A piece",
        slug="a-piece",
        body_markdown="Body with enough words to be a body.",
        keywords=["original"],
        focus_keyword="original",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --------------------------------------------------------------------------- #
# Keyword sync                                                                #
# --------------------------------------------------------------------------- #


def test_new_keywords_alone_pull_the_focus_keyword_with_them(client, auth, piece):
    resp = client.patch(
        f"{API}/{piece.id}", headers=auth, json={"keywords": ["fastapi", "sqlalchemy"]}
    )

    assert resp.status_code == 200
    assert resp.json()["focus_keyword"] == "fastapi"


def test_a_focus_keyword_sent_in_the_same_patch_is_not_overwritten(client, auth, piece):
    """The caller said which one matters. The convenience must not argue."""
    resp = client.patch(
        f"{API}/{piece.id}",
        headers=auth,
        json={"keywords": ["fastapi", "sqlalchemy"], "focus_keyword": "sqlalchemy"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["focus_keyword"] == "sqlalchemy"
    assert body["keywords"] == ["fastapi", "sqlalchemy"]


def test_clearing_the_keywords_leaves_the_focus_keyword_alone(client, auth, piece):
    """An empty list is not a new first keyword to sync to."""
    resp = client.patch(f"{API}/{piece.id}", headers=auth, json={"keywords": []})

    assert resp.status_code == 200
    assert resp.json()["keywords"] == []
    assert resp.json()["focus_keyword"] == "original"


# --------------------------------------------------------------------------- #
# The canonical platform                                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture
def connected(db, user):
    for platform in (Platform.DEVTO, Platform.MASTODON):
        db.add(
            PlatformConnection(
                user_id=user.id,
                platform=platform,
                encrypted_credentials="not-read-in-this-test",
                status=ConnectionStatus.CONNECTED,
            )
        )
    db.commit()


def test_a_project_that_did_not_ask_for_a_canonical_gets_none(
    client, auth, db, project, piece, connected
):
    """``auto_canonical`` off means no platform is nominated as the original."""
    project.auto_canonical = False
    project.canonical_platform = Platform.DEVTO
    db.commit()

    resp = client.get(
        f"{API}/{piece.id}/schedule/suggestions?platforms=devto&platforms=mastodon",
        headers=auth,
    )

    assert resp.status_code == 200
    assert [slot["platform"] for slot in resp.json()] == ["devto", "mastodon"]


def test_the_nominated_platform_is_honoured_when_the_project_asked(
    client, auth, db, project, piece, connected
):
    project.auto_canonical = True
    project.canonical_platform = Platform.DEVTO
    db.commit()

    resp = client.get(
        f"{API}/{piece.id}/schedule/suggestions?platforms=devto&platforms=mastodon",
        headers=auth,
    )

    assert resp.status_code == 200
    assert len(resp.json()) == 2


# --------------------------------------------------------------------------- #
# Scheduling around something already out                                     #
# --------------------------------------------------------------------------- #


def test_scheduling_leaves_a_platform_that_already_published_alone(
    client, auth, db, piece, connected
):
    """The piece went out on dev.to yesterday and is being scheduled on Mastodon.

    Sweeping the published row back to ``scheduled`` would hand it to a worker
    again, and dev.to would get a second copy of a post that is already live.
    """
    already = Publication(
        content_id=piece.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=utcnow(),
        external_url="https://dev.to/x/a-piece",
    )
    pending = Publication(
        content_id=piece.id,
        platform=Platform.MASTODON,
        status=PublicationStatus.PENDING,
    )
    db.add_all([already, pending])
    db.commit()
    db.refresh(already)
    db.refresh(pending)

    resp = client.post(
        f"{API}/{piece.id}/schedule",
        headers=auth,
        json={"platforms": ["mastodon"], "optimize": True},
    )

    assert resp.status_code == 200, resp.text
    db.refresh(already)
    db.refresh(pending)
    assert already.status == PublicationStatus.PUBLISHED
    assert already.scheduled_for is None
    assert pending.status == PublicationStatus.SCHEDULED
    assert pending.scheduled_for is not None


# --------------------------------------------------------------------------- #
# Templates: a prompt template with no headline of its own                    #
# --------------------------------------------------------------------------- #


def test_a_prompt_template_with_no_title_keeps_the_models_headline(
    client, auth, db, project
):
    """``title_template`` is optional. Left blank, the model's title stands.

    The overwrite only exists for authors who templated a headline on purpose —
    an empty one is not an instruction to blank the title.
    """
    template = ContentTemplate(
        user_id=project.user_id,
        name="Briefing only",
        mode="prompt",
        content_type=ContentType.ANNOUNCEMENT,
        title_template="",
        body_template="Write about {{summary}}.",
        variables=[{"name": "summary", "required": True}],
        default_project_id=project.id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)

    resp = client.post(
        f"/api/v1/templates/{template.id}/use",
        headers=auth,
        json={"values": {"summary": "the template engine"}},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"]
    assert body["slug"]
    assert body["source"]["mode"] == "prompt"
