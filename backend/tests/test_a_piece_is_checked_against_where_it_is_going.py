"""Will this piece publish there, and will it arrive intact?

Two questions that could previously only be answered by trying, and whose
answers arrive too late when you do. A refusal is a wasted attempt and a
``failed`` row; a truncation is a live post that is not the post that was on
screen, and on Bluesky that has no undo.

The three sources of a finding are tested apart, because they fail
independently and a caller acts on them differently: the connection (the most
common cause of a failed row on this install), the adapter (scaffolded, cannot
publish at all) and the format (300 graphemes, four tags, a filename).

Two properties of the design are pinned here as much as the behaviour itself:

* **It reports, it does not refuse.** A truncation warning leaves the platform
  ``publishable``. The short-form destinations exist precisely to carry a short
  version of a long piece, and a check that called that a failure would refuse
  the thing it was built to describe.
* **No credentials, no network.** The checks read the piece, the adapters'
  constants and the connections' *status*. A stored secret is never decrypted,
  which is what makes this safe to run on a draft and behind a panel.
"""
from __future__ import annotations

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import platform_check, publishers, publishing_service
from app.services.publishers import formatting
from app.services.publishers.base import PREFLIGHT_ERROR, PREFLIGHT_WARNING


@pytest.fixture
def piece(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        status=ContentStatus.APPROVED,
        title="Pulse ships marketing automation",
        slug="herald-ships-marketing-automation",
        body_markdown="## Why\n\nPulse watches your repo and writes the post.",
        excerpt="Pulse writes the posts about the projects you ship.",
        meta_description="Pulse automates developer marketing end to end.",
        keywords=["marketing automation"],
        tags=["python", "fastapi"],
        source={"kind": "test"},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _request(piece, platform, **kwargs):
    return publishing_service.build_request(piece, platform=platform, **kwargs)


# --------------------------------------------------------------------------- #
# The adapters, which are where a format failure is actually known             #
# --------------------------------------------------------------------------- #


def test_an_article_destination_has_nothing_to_say_about_a_normal_piece(piece):
    """The default is silence. A blog post going somewhere that hosts blog posts
    has no format to fail, and a panel that warned about it would train the
    reader to ignore the panel."""
    findings = publishers.get_adapter(Platform.DEVTO).preflight(
        _request(piece, Platform.DEVTO)
    )

    assert findings == []


def test_bluesky_says_how_far_over_the_limit_a_long_excerpt_is(db, piece):
    piece.excerpt = "word " * 200
    db.commit()

    (finding,) = publishers.get_adapter(Platform.BLUESKY).preflight(
        _request(piece, Platform.BLUESKY)
    )

    assert finding.level == PREFLIGHT_WARNING
    assert finding.limit == formatting.BLUESKY_LIMIT == 300
    assert finding.actual > 300
    # The number, not only the sentence: "this will be shortened" cannot be
    # acted on and "this is 700 characters over" can.
    assert str(finding.actual - 300) in finding.message


def test_bluesky_is_quiet_about_a_piece_that_fits(piece):
    """The other side, so the check cannot pass by warning about everything."""
    assert publishers.get_adapter(Platform.BLUESKY).preflight(
        _request(piece, Platform.BLUESKY)
    ) == []


def test_the_bluesky_warning_agrees_with_what_bluesky_would_actually_post(db, piece):
    """The measurement has to describe the composer, not a second opinion of it.

    ``social_budget`` assembles the post the way ``compose_social`` does and
    skips only the trimming, so "over by N" means the composer had to cut. If
    the two drift, the panel reports truncation that does not happen — or worse,
    stays quiet about truncation that does.
    """
    piece.excerpt = "word " * 200
    db.commit()
    adapter = publishers.get_adapter(Platform.BLUESKY)
    request = _request(piece, Platform.BLUESKY)

    (finding,) = adapter.preflight(request)
    posted = adapter.build_text(request)

    assert finding.actual > formatting.BLUESKY_LIMIT
    assert len(posted) <= formatting.BLUESKY_LIMIT, "the composer must still fit"


def test_bluesky_refuses_a_draft_before_an_attempt_is_spent_on_it(piece):
    """The same refusal ``publish`` raises, made while it is still free."""
    (finding,) = publishers.get_adapter(Platform.BLUESKY).preflight(
        _request(piece, Platform.BLUESKY, as_draft=True)
    )

    assert finding.level == PREFLIGHT_ERROR
    assert "draft" in finding.message.lower()


def test_mastodon_and_bluesky_disagree_about_the_same_piece(db, piece):
    """500 characters and a flat link cost against 300 and a real one.

    A piece that overflows Bluesky routinely fits Mastodon, and reporting one
    number for "the social platforms" would be wrong for whichever one it was
    not measured against.
    """
    piece.excerpt = "word " * 70
    db.commit()

    bluesky = publishers.get_adapter(Platform.BLUESKY).preflight(
        _request(piece, Platform.BLUESKY)
    )
    mastodon = publishers.get_adapter(Platform.MASTODON).preflight(
        _request(piece, Platform.MASTODON)
    )

    assert bluesky and bluesky[0].limit == 300
    assert mastodon == []


def test_devto_reports_the_tag_it_will_silently_drop(db, piece):
    """Forem accepts a fifth tag and ignores it. Nothing anywhere said so."""
    piece.tags = ["python", "fastapi", "testing", "ci", "docker"]
    db.commit()

    (finding,) = publishers.get_adapter(Platform.DEVTO).preflight(
        _request(piece, Platform.DEVTO)
    )

    assert finding.level == PREFLIGHT_WARNING
    assert finding.limit == 4
    assert "docker" in finding.message


def test_devto_refuses_a_piece_with_no_title(db, piece):
    piece.title = "   "
    db.commit()

    findings = publishers.get_adapter(Platform.DEVTO).preflight(
        _request(piece, Platform.DEVTO)
    )

    assert [f.level for f in findings] == [PREFLIGHT_ERROR]


def test_git_refuses_a_piece_with_no_slug(db, piece):
    """A Git post is a file named after the slug."""
    piece.slug = ""
    db.commit()

    findings = publishers.get_adapter(Platform.GIT).preflight(
        _request(piece, Platform.GIT)
    )

    assert any(f.level == PREFLIGHT_ERROR and "slug" in f.message for f in findings)


def test_git_warns_about_empty_front_matter_description(db, piece):
    """It commits fine and looks broken in a search result."""
    piece.meta_description = ""
    piece.excerpt = ""
    db.commit()

    findings = publishers.get_adapter(Platform.GIT).preflight(
        _request(piece, Platform.GIT)
    )

    assert any(
        f.level == PREFLIGHT_WARNING and "description" in f.message for f in findings
    )


# --------------------------------------------------------------------------- #
# The service, which adds what the adapters cannot know                        #
# --------------------------------------------------------------------------- #


def test_an_unconnected_platform_is_an_error_in_the_platforms_own_words(
    db, piece, user
):
    """The sentence is ``not_connected_error``'s, not a fourth spelling of it.

    ``publish_recovery`` matches that exact string to find the rows a new
    connection un-blocks. A second wording here is a second thing to keep in
    step, and this is the failure that put all ten failed pieces on production.
    """
    (verdict,) = platform_check.check(db, piece, [Platform.DEVTO], user_id=user.id)

    assert not verdict.publishable
    assert verdict.errors[0].message == publishing_service.not_connected_error(
        Platform.DEVTO
    )


def test_a_connected_platform_is_publishable(db, piece, user, connect):
    connect("devto")

    (verdict,) = platform_check.check(db, piece, [Platform.DEVTO], user_id=user.id)

    assert verdict.publishable
    assert verdict.findings == []


def test_a_connection_that_is_not_live_does_not_count(db, piece, user, connect):
    """``invalid`` is a connection the platform rejected. It is not a connection."""
    connect("devto")
    row = db.query(PlatformConnection).filter_by(platform=Platform.DEVTO).one()
    row.status = ConnectionStatus.INVALID
    db.commit()

    (verdict,) = platform_check.check(db, piece, [Platform.DEVTO], user_id=user.id)

    assert not verdict.publishable


def test_a_scaffolded_platform_says_so_rather_than_failing_at_the_end_of_a_queue(
    db, piece, user
):
    (verdict,) = platform_check.check(db, piece, [Platform.TWITTER], user_id=user.id)

    assert not verdict.publishable
    assert "cannot publish" in verdict.errors[0].message


def test_the_format_is_checked_even_when_the_platform_is_not_connected(db, piece, user):
    """Somebody about to connect Bluesky should find out the excerpt is 400
    characters too long in the same breath, not on the next screen."""
    piece.excerpt = "word " * 200
    db.commit()

    (verdict,) = platform_check.check(db, piece, [Platform.BLUESKY], user_id=user.id)

    assert len(verdict.errors) == 1, "the missing connection"
    assert len(verdict.warnings) == 1, "and the length, reported anyway"


def test_a_truncation_warning_leaves_the_platform_publishable(
    db, piece, user, connect
):
    """The property the whole design turns on. A short-form destination exists to
    carry a short version of a long piece; calling that a failure would refuse
    the thing it is for."""
    connect("bluesky")
    piece.excerpt = "word " * 200
    db.commit()

    (verdict,) = platform_check.check(db, piece, [Platform.BLUESKY], user_id=user.id)

    assert verdict.warnings
    assert verdict.publishable


def test_an_adapter_that_raises_inside_preflight_does_not_lose_the_panel(
    db, piece, user, monkeypatch, connect
):
    """Advice with a bug in it is still only advice.

    Losing every other platform's verdict because one adapter miscounted a
    limit would make this less useful than having nothing at all.
    """
    connect("devto")

    def boom(request):
        raise RuntimeError("miscounted")

    monkeypatch.setattr(publishers.get_adapter(Platform.DEVTO), "preflight", boom)

    (verdict,) = platform_check.check(db, piece, [Platform.DEVTO], user_id=user.id)

    assert verdict.publishable, "the connection was fine; only the advice broke"


def test_the_check_never_decrypts_a_credential(db, piece, user, connect, monkeypatch):
    """What makes this safe on a draft and cheap behind a panel."""
    connect("devto")

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not decrypt credentials")

    monkeypatch.setattr(
        "app.services.publishing_service.decrypt_credentials", forbidden
    )

    platform_check.check(db, piece, [Platform.DEVTO], user_id=user.id)


# --------------------------------------------------------------------------- #
# Which destinations get checked when the caller does not say                  #
# --------------------------------------------------------------------------- #


def test_the_default_destinations_are_where_the_piece_is_actually_going(
    db, piece, project
):
    db.add(Publication(content_id=piece.id, platform=Platform.DEVTO))
    project.autopilot_platforms = ["bluesky"]
    db.commit()
    db.refresh(piece)

    assert platform_check.destinations_for(piece) == [Platform.DEVTO, Platform.BLUESKY]


def test_a_destination_that_is_both_queued_and_configured_is_listed_once(
    db, piece, project
):
    db.add(Publication(content_id=piece.id, platform=Platform.DEVTO))
    project.autopilot_platforms = ["devto"]
    db.commit()
    db.refresh(piece)

    assert platform_check.destinations_for(piece) == [Platform.DEVTO]


def test_an_unknown_stored_platform_does_not_break_the_panel(db, piece, project):
    """A name stored before it was renamed. The projects page is where that gets
    fixed; a preview panel that 500s on it helps nobody."""
    project.autopilot_platforms = ["devto", "geocities"]
    db.commit()
    db.refresh(piece)

    assert platform_check.destinations_for(piece) == [Platform.DEVTO]


# --------------------------------------------------------------------------- #
# The endpoint                                                                 #
# --------------------------------------------------------------------------- #


def test_the_endpoint_reports_every_destination(client, auth, piece, project, db):
    project.autopilot_platforms = ["devto", "bluesky"]
    db.commit()

    body = client.get(
        f"/api/v1/content/{piece.id}/platform-check", headers=auth
    ).json()

    assert body["content_id"] == piece.id
    assert [p["platform"] for p in body["platforms"]] == ["devto", "bluesky"]
    assert body["publishable"] is False, "neither is connected"


def test_the_endpoint_takes_an_explicit_platform_list(client, auth, piece, connect):
    connect("devto")

    body = client.get(
        f"/api/v1/content/{piece.id}/platform-check?platforms=devto", headers=auth
    ).json()

    assert body["publishable"] is True
    assert len(body["platforms"]) == 1


def test_the_endpoint_carries_the_numbers_behind_a_limit(
    client, auth, piece, db, connect
):
    connect("bluesky")
    piece.excerpt = "word " * 200
    db.commit()

    (found,) = client.get(
        f"/api/v1/content/{piece.id}/platform-check?platforms=bluesky", headers=auth
    ).json()["platforms"]

    (finding,) = found["findings"]
    assert finding["limit"] == 300
    assert finding["actual"] > 300


def test_the_endpoint_refuses_an_unknown_platform(client, auth, piece):
    refused = client.get(
        f"/api/v1/content/{piece.id}/platform-check?platforms=geocities", headers=auth
    )

    assert refused.status_code == 422


def test_a_piece_with_nowhere_to_go_is_not_publishable(client, auth, piece, project, db):
    """A green tick on the one case that most needs a red one."""
    project.autopilot_platforms = []
    db.commit()

    body = client.get(
        f"/api/v1/content/{piece.id}/platform-check", headers=auth
    ).json()

    assert body["platforms"] == []
    assert body["publishable"] is False


def test_the_draft_flag_changes_the_answer(client, auth, piece, connect):
    """Bluesky has no draft state, so "stage everywhere" is an error there."""
    connect("bluesky")

    live = client.get(
        f"/api/v1/content/{piece.id}/platform-check?platforms=bluesky", headers=auth
    ).json()
    staged = client.get(
        f"/api/v1/content/{piece.id}/platform-check?platforms=bluesky&as_draft=true",
        headers=auth,
    ).json()

    assert live["publishable"] is True
    assert staged["publishable"] is False


def test_somebody_elses_piece_is_not_checkable(client, auth, db, piece):
    """The ownership rule every other read here follows."""
    from app.models.user import User

    other = User(email="other@example.com", hashed_password="x", full_name="Other")
    db.add(other)
    db.commit()

    missing = client.get("/api/v1/content/999999/platform-check", headers=auth)

    assert missing.status_code == 404


def test_the_endpoint_does_not_need_the_piece_to_be_publishable_to_answer(
    client, auth, piece, db
):
    """A draft is the moment this is most useful — it is still editable."""
    piece.status = ContentStatus.DRAFT
    db.commit()

    answered = client.get(
        f"/api/v1/content/{piece.id}/platform-check?platforms=devto", headers=auth
    )

    assert answered.status_code == 200


def test_a_published_piece_still_reports_its_platforms(client, auth, piece, db):
    """Reading the panel after the fact is how somebody finds out *why* the post
    that went out is shorter than the one they wrote."""
    db.add(
        Publication(
            content_id=piece.id,
            platform=Platform.BLUESKY,
            status=PublicationStatus.PUBLISHED,
        )
    )
    piece.status = ContentStatus.PUBLISHED
    db.commit()

    body = client.get(
        f"/api/v1/content/{piece.id}/platform-check", headers=auth
    ).json()

    assert [p["platform"] for p in body["platforms"]] == ["bluesky"]
