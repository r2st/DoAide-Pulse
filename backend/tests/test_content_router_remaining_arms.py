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

from datetime import datetime, timedelta

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


@pytest.fixture
def nominated(monkeypatch):
    """Capture the ``canonical`` the router hands to ``optimal_slots``.

    The two tests below are about one decision — which platform, if any, the
    router nominates as the original — and that decision *is* this argument.
    Reading it off the response instead means reading it out of the order the
    slots came back in, and that order is whatever the cadence table produced
    for the day the suite happens to run: devto's Tuesday/Thursday 13:00 against
    Mastodon's daily 14:00 put Mastodon first from Friday to Monday, and from
    13:00 UTC on a Thursday. The assertion was true for about half the week.

    The real function still runs, so the endpoint's response is a real one.
    """
    from app.services import scheduling

    seen: list[Platform | None] = []
    original = scheduling.optimal_slots

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("canonical"))
        return original(*args, **kwargs)

    monkeypatch.setattr("app.routers.content.scheduling.optimal_slots", _spy)
    return seen


def test_a_project_that_did_not_ask_for_a_canonical_gets_none(
    client, auth, db, project, piece, connected, nominated
):
    """``auto_canonical`` off means no platform is nominated as the original.

    Even though the project names one. The flag is the switch; the named
    platform is only which one it would be — inventing an original for a project
    that asked for none puts a ``rel=canonical`` on somebody's blog pointing at
    a copy.
    """
    project.auto_canonical = False
    project.canonical_platform = Platform.DEVTO
    db.commit()

    resp = client.get(
        f"{API}/{piece.id}/schedule/suggestions?platforms=devto&platforms=mastodon",
        headers=auth,
    )

    assert resp.status_code == 200
    assert nominated == [None]
    assert {slot["platform"] for slot in resp.json()} == {"devto", "mastodon"}


def test_the_nominated_platform_is_honoured_when_the_project_asked(
    client, auth, db, project, piece, connected, nominated
):
    """And with the flag on, the named platform is the one that goes through.

    This previously asserted only that two slots came back, which is equally
    true of the case above — the two tests could not tell each other apart.
    """
    project.auto_canonical = True
    project.canonical_platform = Platform.DEVTO
    db.commit()

    resp = client.get(
        f"{API}/{piece.id}/schedule/suggestions?platforms=devto&platforms=mastodon",
        headers=auth,
    )

    assert resp.status_code == 200
    assert nominated == [Platform.DEVTO]

    # And the nomination has its documented effect: the copy is pushed behind
    # the original by the syndication delay, so it cannot go out before there is
    # a URL for it to be canonical to.
    from app.config import settings

    slots = {s["platform"]: datetime.fromisoformat(s["when"]) for s in resp.json()}
    assert slots["mastodon"] - slots["devto"] >= timedelta(
        seconds=settings.syndication_delay_seconds
    )


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


def test_naming_the_already_published_platform_still_leaves_it_alone(
    client, auth, db, piece, connected
):
    """The same protection, for the caller that resends the whole platform list.

    The test above omits dev.to, so the published row never enters the batch.
    A UI that posts back every platform it is showing *does* name it, and
    ``publishing_service.queue`` hands the live row straight back rather than
    re-arming it. The optimizer then walks that batch assigning its slots, and
    the published row has to be stepped over there too — otherwise the row
    comes back ``scheduled`` for a time in the future and the next beat posts
    a second copy to a platform that already has one.
    """
    already = Publication(
        content_id=piece.id,
        platform=Platform.DEVTO,
        status=PublicationStatus.PUBLISHED,
        published_at=utcnow(),
        external_url="https://dev.to/x/a-piece",
    )
    db.add(already)
    db.commit()
    db.refresh(already)
    published_at = already.published_at

    resp = client.post(
        f"{API}/{piece.id}/schedule",
        headers=auth,
        json={"platforms": ["devto", "mastodon"], "optimize": True},
    )

    assert resp.status_code == 200, resp.text
    by_platform = {row["platform"]: row for row in resp.json()}
    assert by_platform["devto"]["status"] == "published"
    assert by_platform["devto"]["scheduled_for"] is None
    assert by_platform["mastodon"]["status"] == "scheduled"

    db.refresh(already)
    assert already.status == PublicationStatus.PUBLISHED
    assert already.scheduled_for is None
    assert already.published_at == published_at


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
