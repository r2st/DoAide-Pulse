"""A headline swap changed Herald's copy of the title and nothing else.

The gap this file pins shut: ``headlines.apply_headline`` retitles the piece in
the database, opens a fresh attribution window, and starts crediting that new
headline with everything the post earns from then on — while the post itself,
on Dev.to or Bluesky or in somebody's inbox, still says what it always said.
Every number the headline feature produced was about a headline no reader had
ever seen.

Two halves to the fix, and both are tested here:

* **Tell the destinations that can be told.** Dev.to, Hashnode, WordPress and a
  Git repo all have an API for it. ``headline_sync.sync_title`` uses it, and
  records on the publication what each destination is now showing.
* **Stop counting the ones that cannot.** A Bluesky post has no title and a
  sent newsletter is in inboxes. Their engagement says nothing about a headline
  that was never on them, so ``headlines.performance`` leaves them out — and
  ``/headlines/reach`` says so out loud, because a piece doing well everywhere
  and reporting "not enough data" otherwise looks like a bug.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.models.content import Content, ContentStatus, ContentType
from app.models.metrics import ContentMetric
from app.models.publication import Platform, Publication, PublicationStatus
from app.services import headline_sync, headlines, publishers
from app.services.publishers.base import (
    Adapter,
    NotImplementedAdapter,
    PublishError,
    PublishRequest,
)
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.git import GitAdapter
from app.services.publishers.hashnode import HashnodeAdapter
from app.services.publishers.wordpress import WordPressAdapter
from app.tasks.headline_tasks import auto_select_headlines, sync_headline


def _no_close(session):
    """Hand a task the test's session without letting it close the shared one."""

    class NoCloseProxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def __setattr__(self, name, value):
            setattr(session, name, value)

        def close(self):
            pass

    return lambda: NoCloseProxy()


def _now() -> datetime:
    return datetime.now(UTC)


class FakeResponse:
    """Enough of an ``httpx.Response`` for the adapters' ``_json`` helper."""

    status_code = 200

    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _answer(adapter: Adapter, monkeypatch, payload: Any) -> list[dict]:
    """Make every request this adapter sends return *payload*. Records the calls."""
    calls: list[dict] = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(payload)

    monkeypatch.setattr(adapter, "_request", fake_request)
    return calls


REQUEST = PublishRequest(
    title="The headline that won",
    body_markdown="## Why\n\nBecause it did.",
    excerpt="Because it did.",
    meta_description="",
    slug="the-headline",
)


@pytest.fixture
def piece(db, project) -> Content:
    row = Content(
        project_id=project.id,
        content_type=ContentType.ANNOUNCEMENT,
        title="Original headline",
        slug="original-headline",
        status=ContentStatus.PUBLISHED,
        created_at=_now() - timedelta(days=20),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _publication(db, piece, platform, *, live_title="Original headline", external_id="ext-1"):
    row = Publication(
        content_id=piece.id,
        platform=platform,
        status=PublicationStatus.PUBLISHED,
        external_id=external_id,
        live_title=live_title,
        published_at=piece.created_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _snapshot(db, publication, *, at, views):
    db.add(
        ContentMetric(
            publication_id=publication.id,
            captured_at=at,
            views=views,
            reactions=0,
            comments=0,
            shares=0,
        )
    )
    db.commit()


# --------------------------------------------------------------------------- #
# The adapter contract                                                        #
# --------------------------------------------------------------------------- #


def test_every_adapter_that_claims_the_capability_implements_it():
    """``supports_title_update`` and ``update_title`` cannot drift apart.

    The flag is what decides whether a publication's engagement is counted as
    evidence about the current headline. An adapter that claimed it without
    implementing it would have its posts counted for a headline they never
    carried — the exact bug this whole file exists to close, reintroduced one
    class attribute at a time.
    """
    for adapter in publishers.all_adapters():
        overrides = type(adapter).update_title is not Adapter.update_title
        assert overrides == adapter.supports_title_update, (
            f"{adapter.display_name}: supports_title_update="
            f"{adapter.supports_title_update} but overrides={overrides}"
        )


def test_an_adapter_that_cannot_retitle_says_so_rather_than_silently_passing():
    """The default has to raise, not return.

    A base method that returned ``None`` would let a caller record the retitle
    as done for Bluesky, which is the failure the flag exists to prevent.
    """
    for adapter in publishers.all_adapters():
        if adapter.supports_title_update:
            continue
        with pytest.raises(NotImplementedAdapter):
            adapter.update_title(REQUEST, {"api_key": "k"}, "ext-1")


# --------------------------------------------------------------------------- #
# What each adapter sends                                                     #
# --------------------------------------------------------------------------- #


def test_devto_retitle_sends_the_title_and_nothing_else(monkeypatch):
    """A headline test changes one variable.

    Resending the body would smuggle whatever the piece has become in Herald
    since publication onto a live post under cover of a title swap.
    """
    adapter = DevToAdapter()
    calls = _answer(adapter, monkeypatch, {"id": 9})

    adapter.update_title(REQUEST, {"api_key": "k"}, "9")

    assert len(calls) == 1
    assert calls[0]["method"] == "PUT"
    assert calls[0]["url"].endswith("/articles/9")
    assert calls[0]["json_body"] == {"article": {"title": "The headline that won"}}


def test_wordpress_retitle_does_not_move_the_post(monkeypatch):
    """The slug is not resent: the post is live at a URL and links point at it."""
    adapter = WordPressAdapter()
    calls = _answer(adapter, monkeypatch, {"id": 12})

    adapter.update_title(
        REQUEST,
        {
            "site_url": "https://blog.example.com",
            "username": "ada",
            "application_password": "pw",
        },
        "12",
    )

    assert calls[0]["json_body"] == {"title": "The headline that won"}
    assert "slug" not in calls[0]["json_body"]
    assert calls[0]["url"].endswith("/posts/12")


def test_hashnode_retitle_requires_the_platform_to_confirm(monkeypatch):
    """GraphQL answers 200 for a mutation that did nothing."""
    adapter = HashnodeAdapter()
    _answer(adapter, monkeypatch, {"data": {"updatePost": {"post": None}}})

    with pytest.raises(PublishError):
        adapter.update_title(REQUEST, {"api_key": "k", "publication_id": "p"}, "post-9")


def test_hashnode_retitle_sends_the_post_id_and_the_title(monkeypatch):
    adapter = HashnodeAdapter()
    calls = _answer(
        adapter, monkeypatch, {"data": {"updatePost": {"post": {"id": "post-9"}}}}
    )

    adapter.update_title(REQUEST, {"api_key": "k", "publication_id": "p"}, "post-9")

    variables = calls[0]["json_body"]["variables"]
    assert variables == {"input": {"id": "post-9", "title": "The headline that won"}}


def test_an_empty_headline_never_reaches_a_platform(monkeypatch):
    """Every one of these would 4xx, and one of them would blank a live title."""
    blank = PublishRequest(title="   ", body_markdown="b", excerpt="", meta_description="")
    for adapter, credentials in (
        (DevToAdapter(), {"api_key": "k"}),
        (
            WordPressAdapter(),
            {"site_url": "https://b.example.com", "username": "u", "application_password": "p"},
        ),
        (HashnodeAdapter(), {"api_key": "k", "publication_id": "p"}),
        (GitAdapter(), {"repo": "ada/blog", "token": "t"}),
    ):
        calls = _answer(adapter, monkeypatch, {})
        with pytest.raises(PublishError):
            adapter.update_title(blank, credentials, "ext-1")
        assert calls == []


# --------------------------------------------------------------------------- #
# Git: a commit rewrites the file, so only the headline lines may change      #
# --------------------------------------------------------------------------- #


def test_git_retitle_edits_the_front_matter_and_leaves_the_body_alone(monkeypatch):
    """A repo has no partial update, so the risk here is rewriting too much.

    Re-rendering from the request would be less code and would also push
    whatever the body has become in Herald since, plus a fresh ``date``, under a
    commit message that says the title changed.
    """
    adapter = GitAdapter()
    existing = (
        "---\n"
        "title: Original headline\n"
        "ogTitle: Original headline\n"
        "description: A description nobody asked to change\n"
        "date: 2026-01-01T00:00:00+00:00\n"
        "tags: [python]\n"
        "---\n"
        "\n"
        "The body, which title: is not a front matter key down here.\n"
    )
    calls = _answer(
        adapter,
        monkeypatch,
        {"sha": "blob-1", "content": base64.b64encode(existing.encode()).decode()},
    )

    adapter.update_title(REQUEST, {"repo": "ada/blog", "token": "t"}, "commit-sha")

    put = [call for call in calls if call["method"] == "PUT"]
    assert len(put) == 1
    written = base64.b64decode(put[0]["json_body"]["content"]).decode()
    assert 'title: "The headline that won"' in written
    assert 'ogTitle: "The headline that won"' in written
    assert "description: A description nobody asked to change" in written
    assert "date: 2026-01-01T00:00:00+00:00" in written
    # The body is untouched, including the line that looks like a front matter key.
    assert "The body, which title: is not a front matter key down here." in written
    assert put[0]["json_body"]["sha"] == "blob-1"


def test_git_retitle_does_not_invent_front_matter():
    """A file published without a title key does not gain one.

    Adding a key the published file never had is a content change wearing a
    retitle's commit message.
    """
    plain = "Just a body, no front matter at all.\n"
    assert GitAdapter._retitle_front_matter(plain, "New") == plain

    unterminated = "---\ntitle: Original\nstill going\n"
    assert GitAdapter._retitle_front_matter(unterminated, "New") == unterminated


def test_git_retitle_escapes_a_headline_that_would_break_the_yaml():
    """The value goes through the same escaping the publish path uses."""
    nasty = 'A "quoted": headline'
    out = GitAdapter._retitle_front_matter("---\ntitle: Old\n---\n\nBody\n", nasty)

    assert "Body" in out
    assert "title: Old" not in out
    # Whatever the quoting rule is, the block still parses as one key per line.
    body_start = out.index("\n---", 3)
    assert out[4:body_start].count("\n") == 0


def test_git_retitle_skips_the_commit_when_nothing_changes(monkeypatch):
    """A no-op commit is a commit, and it lands in the repo's history."""
    adapter = GitAdapter()
    # Quoted exactly as ``formatting.front_matter`` writes it, which is what a
    # file this adapter published actually looks like.
    existing = '---\ntitle: "The headline that won"\n---\n\nBody\n'
    calls = _answer(
        adapter,
        monkeypatch,
        {"sha": "blob-1", "content": base64.b64encode(existing.encode()).decode()},
    )

    adapter.update_title(REQUEST, {"repo": "ada/blog", "token": "t"}, "commit-sha")

    assert [call["method"] for call in calls] == ["GET"]


def test_git_retitle_refuses_a_file_it_cannot_read(monkeypatch):
    adapter = GitAdapter()
    _answer(adapter, monkeypatch, {"sha": "blob-1", "content": None})

    with pytest.raises(PublishError):
        adapter.update_title(REQUEST, {"repo": "ada/blog", "token": "t"}, "commit-sha")


def test_git_retitle_refuses_a_file_that_is_not_utf8(monkeypatch):
    adapter = GitAdapter()
    _answer(
        adapter,
        monkeypatch,
        {"sha": "blob-1", "content": base64.b64encode(b"\xff\xfe not utf-8").decode()},
    )

    with pytest.raises(PublishError):
        adapter.update_title(REQUEST, {"repo": "ada/blog", "token": "t"}, "commit-sha")


# --------------------------------------------------------------------------- #
# The sync                                                                     #
# --------------------------------------------------------------------------- #


def test_a_swap_is_pushed_to_the_destination_that_can_take_it(db, piece, connect, monkeypatch):
    connect(Platform.DEVTO)
    publication = _publication(db, piece, Platform.DEVTO)
    headlines.apply_headline(piece, "The headline that won")
    db.commit()

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        DevToAdapter,
        "update_title",
        lambda self, request, credentials, external_id: sent.append(
            (external_id, request.title)
        ),
    )

    outcomes = headline_sync.sync_title(db, piece)

    assert sent == [("ext-1", "The headline that won")]
    assert [o.status for o in outcomes] == [headline_sync.UPDATED]
    # And the row now records what the reader is seeing.
    db.refresh(publication)
    assert publication.live_title == "The headline that won"


def test_a_platform_that_cannot_be_retitled_is_reported_not_attempted(db, piece, connect):
    """No request, no failure, and a reason the UI can show."""
    connect(Platform.BLUESKY)
    _publication(db, piece, Platform.BLUESKY)
    headlines.apply_headline(piece, "The headline that won")
    db.commit()

    (outcome,) = headline_sync.sync_title(db, piece)

    assert outcome.status == headline_sync.UNSUPPORTED
    assert not outcome.reached
    assert "bluesky" in outcome.detail


def test_a_failed_retitle_leaves_the_row_saying_what_is_actually_live(
    db, piece, connect, monkeypatch
):
    """``live_title`` is a statement about the platform, not about the attempt.

    Recording the new title on a failed push would be the original bug in a
    smaller box: the row would claim a headline the reader is not seeing, and
    attribution would go back to counting it.
    """
    connect(Platform.DEVTO)
    publication = _publication(db, piece, Platform.DEVTO)
    headlines.apply_headline(piece, "The headline that won")
    db.commit()

    def boom(self, request, credentials, external_id):
        raise PublishError("Dev.to said no")

    monkeypatch.setattr(DevToAdapter, "update_title", boom)

    (outcome,) = headline_sync.sync_title(db, piece)

    assert outcome.status == headline_sync.FAILED
    assert "Dev.to said no" in outcome.detail
    db.refresh(publication)
    assert publication.live_title == "Original headline"


def test_a_missing_connection_is_a_failed_sync_not_a_crash(db, piece):
    """The account disconnected Dev.to between publishing and the swap."""
    _publication(db, piece, Platform.DEVTO)
    headlines.apply_headline(piece, "The headline that won")
    db.commit()

    (outcome,) = headline_sync.sync_title(db, piece)

    assert outcome.status == headline_sync.FAILED
    assert not outcome.reached


def test_a_destination_already_showing_the_headline_is_not_asked_again(
    db, piece, connect, monkeypatch
):
    """The sweep runs daily and most days there is nothing to say."""
    connect(Platform.DEVTO)
    _publication(db, piece, Platform.DEVTO, live_title="Original headline")

    def boom(self, request, credentials, external_id):  # pragma: no cover - must not run
        raise AssertionError("nothing to retitle")

    monkeypatch.setattr(DevToAdapter, "update_title", boom)

    (outcome,) = headline_sync.sync_title(db, piece)

    assert outcome.status == headline_sync.UNCHANGED
    assert outcome.reached


def test_a_draft_and_an_unaddressable_row_are_skipped(db, piece, connect):
    connect(Platform.DEVTO, Platform.WORDPRESS)
    draft = _publication(db, piece, Platform.DEVTO)
    draft.as_draft = True
    _publication(db, piece, Platform.WORDPRESS, external_id=None)
    db.commit()
    headlines.apply_headline(piece, "The headline that won")
    db.commit()

    outcomes = headline_sync.sync_title(db, piece)

    assert {o.status for o in outcomes} == {headline_sync.SKIPPED}


def test_a_secret_in_a_platform_error_does_not_reach_the_outcome(
    db, piece, user, monkeypatch
):
    """The same redaction the publish path applies, for the same reason.

    ``detail`` is rendered in the UI, and adapter messages quote the platform's
    response body on purpose — a platform that validates by echoing
    ("invalid api_key: …") would put the token in a column the owner reads.
    """
    from app.models.platform_connection import ConnectionStatus, PlatformConnection
    from app.services.crypto import encrypt_credentials

    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "sk-not-in-a-log"}),
            display_name="@herald-devto",
        )
    )
    db.commit()
    _publication(db, piece, Platform.DEVTO)
    headlines.apply_headline(piece, "The headline that won")
    db.commit()

    def leak(self, request, credentials, external_id):
        raise PublishError(f"invalid api_key: {credentials['api_key']}")

    monkeypatch.setattr(DevToAdapter, "update_title", leak)

    (outcome,) = headline_sync.sync_title(db, piece)

    assert "sk-not-in-a-log" not in outcome.detail


# --------------------------------------------------------------------------- #
# Attribution: the half that was measuring nothing                            #
# --------------------------------------------------------------------------- #


def test_a_destination_that_never_saw_the_headline_is_not_evidence_for_it(db, piece):
    """The bug, stated as a test.

    Bluesky cannot be retitled, so its post still says the old headline. Every
    view it earns after the swap was earned by that old headline, and crediting
    them to the new one is how a headline that nobody has read wins a contest.
    """
    bluesky = _publication(db, piece, Platform.BLUESKY)
    headlines.apply_headline(piece, "The headline that won")
    db.commit()
    _snapshot(db, bluesky, at=_now() - timedelta(hours=2), views=5000)

    windows = headlines.performance(piece, db)

    assert [w.views for w in windows] == [0, 0]
    assert [w.snapshots for w in windows] == [0, 0]


def test_a_destination_that_was_told_still_counts(db, piece):
    """The other side of the same rule — this is not a blanket exclusion."""
    devto = _publication(db, piece, Platform.DEVTO)
    headlines.apply_headline(
        piece, "The headline that won", now=_now() - timedelta(days=5)
    )
    devto.live_title = "The headline that won"
    db.commit()
    _snapshot(db, devto, at=_now() - timedelta(hours=2), views=300)

    _, current = headlines.performance(piece, db)

    assert current.views == 300


def test_a_retitle_that_failed_stops_the_piece_being_judged(db, piece):
    """Dev.to *can* be retitled and this time it was not.

    Nothing distinguishes this from the unsupported case as far as the reader is
    concerned, and the numbers must not either.
    """
    devto = _publication(db, piece, Platform.DEVTO)
    headlines.apply_headline(piece, "The headline that won")
    db.commit()  # live_title still the old headline
    _snapshot(db, devto, at=_now() - timedelta(hours=2), views=300)

    windows = headlines.performance(piece, db)

    assert [w.views for w in windows] == [0, 0]


def test_a_row_from_before_the_column_existed_is_still_counted(db, piece):
    """No backfill, and no silent loss of every piece published before today.

    NULL means "published before Herald recorded this", which is exactly how
    those rows were already being counted. The guard that does the real work for
    them is the platform capability, which needs no stored state.
    """
    devto = _publication(db, piece, Platform.DEVTO, live_title=None)
    _snapshot(db, devto, at=_now() - timedelta(hours=2), views=300)

    (current,) = headlines.performance(piece, db)

    assert current.views == 300


def test_a_piece_nobody_can_retitle_never_produces_a_confident_winner(db, piece):
    """The end-to-end consequence, and the reason this is worth the machinery.

    Before, a piece published only to Bluesky would accumulate windows, rank
    them on views its headlines never earned, and — for a project that opted
    into ``auto_headline_winner`` — swap its title on that evidence, daily.
    """
    bluesky = _publication(db, piece, Platform.BLUESKY)
    _snapshot(db, bluesky, at=piece.created_at + timedelta(days=1), views=1000)
    headlines.apply_headline(piece, "The headline that won", now=_now() - timedelta(days=5))
    db.commit()
    _snapshot(db, bluesky, at=_now() - timedelta(hours=1), views=9000)

    verdict = headlines.pick_winner(headlines.performance(piece, db))

    assert not verdict.confident
    assert "Not enough data" in verdict.reason


def test_a_translated_publication_is_not_evidence_about_an_english_headline(db, piece):
    """Its readers saw neither English headline.

    ``live_title`` is written from the *request*, so a French post records the
    French title and never matches the source — which excludes it without
    needing a rule about translations at all.
    """
    devto = _publication(db, piece, Platform.DEVTO, live_title="Le titre d'origine")
    _snapshot(db, devto, at=_now() - timedelta(hours=2), views=300)

    (current,) = headlines.performance(piece, db)

    assert current.views == 0


# --------------------------------------------------------------------------- #
# Saying so                                                                    #
# --------------------------------------------------------------------------- #


def test_reach_names_both_reasons_a_destination_is_not_showing_the_headline(
    db, piece, client, auth
):
    """"Not enough data" beside plenty of engagement needs an explanation."""
    _publication(db, piece, Platform.DEVTO, external_id="a")
    _publication(db, piece, Platform.BLUESKY, external_id="b")
    _publication(db, piece, Platform.WORDPRESS, external_id="c", live_title="Original headline")
    headlines.apply_headline(piece, "The headline that won")
    db.query(Publication).filter(Publication.external_id == "c").update(
        {"live_title": "The headline that won"}
    )
    db.commit()

    resp = client.get(f"/api/v1/content/{piece.id}/headlines/reach", headers=auth)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["live"] == 3
    assert body["tracking"] == 1
    reasons = {row["platform"]: row["reason"] for row in body["unreachable"]}
    assert reasons == {"devto": "stale", "bluesky": "unsupported"}


def test_the_capability_is_published_so_the_ui_can_explain_itself(client, auth):
    resp = client.get("/api/v1/settings/platforms", headers=auth)

    assert resp.status_code == 200, resp.text
    by_platform = {row["platform"]: row for row in resp.json()}
    assert by_platform["devto"]["supports_title_update"] is True
    assert by_platform["bluesky"]["supports_title_update"] is False


# --------------------------------------------------------------------------- #
# Wiring: nothing above matters if the swap never calls it                     #
# --------------------------------------------------------------------------- #


def test_applying_a_headline_through_the_api_tells_the_destinations(
    db, piece, client, auth, connect, monkeypatch
):
    """The endpoint a human clicks, end to end.

    Inline here because ``celery_enabled`` is off in the suite, which is also
    the single-process deployment: a swap that only queued the push would leave
    those installs permanently diverged with nothing to reconcile them.
    """
    connect(Platform.DEVTO)
    publication = _publication(db, piece, Platform.DEVTO)
    monkeypatch.setattr("app.tasks.headline_tasks.SessionLocal", _no_close(db))
    sent: list[str] = []
    monkeypatch.setattr(
        DevToAdapter,
        "update_title",
        lambda self, request, credentials, external_id: sent.append(request.title),
    )

    resp = client.post(
        f"/api/v1/content/{piece.id}/headlines/apply",
        json={"title": "The headline that won"},
        headers=auth,
    )

    assert resp.status_code == 200, resp.text
    assert sent == ["The headline that won"]
    db.refresh(publication)
    assert publication.live_title == "The headline that won"


def test_a_broken_sync_does_not_fail_the_swap(db, piece, client, auth, monkeypatch):
    """The title is already changed and committed by the time the push runs.

    Returning 500 would tell the user their swap did not happen when it did.
    """

    def boom(content_id):
        raise RuntimeError("broker on fire")

    monkeypatch.setattr("app.tasks.headline_tasks.sync_headline", boom)

    resp = client.post(
        f"/api/v1/content/{piece.id}/headlines/apply",
        json={"title": "The headline that won"},
        headers=auth,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "The headline that won"


def test_the_sweep_pushes_the_headline_it_just_swapped(db, piece, project, connect, monkeypatch):
    """An automated swap has the same obligation as a manual one."""
    connect(Platform.DEVTO)
    project.auto_headline_winner = True
    devto = _publication(db, piece, Platform.DEVTO)
    # A closed window with a real run behind it, and a live headline that has
    # had a fair run of its own and is losing.
    _snapshot(db, devto, at=piece.created_at + timedelta(days=1), views=100)
    _snapshot(db, devto, at=piece.created_at + timedelta(days=2), views=5000)
    headlines.apply_headline(piece, "A quieter headline", now=_now() - timedelta(days=8))
    devto.live_title = "A quieter headline"
    db.commit()
    _snapshot(db, devto, at=_now() - timedelta(days=7), views=5010)
    _snapshot(db, devto, at=_now() - timedelta(hours=1), views=5020)

    monkeypatch.setattr("app.tasks.headline_tasks.SessionLocal", _no_close(db))
    sent: list[str] = []
    monkeypatch.setattr(
        DevToAdapter,
        "update_title",
        lambda self, request, credentials, external_id: sent.append(request.title),
    )

    result = auto_select_headlines()

    assert result["swapped"] == 1
    assert sent == ["Original headline"]
    db.refresh(devto)
    assert devto.live_title == "Original headline"


def test_the_sync_task_shrugs_at_a_piece_that_was_deleted(db, monkeypatch):
    """A swap and a delete can race; the swap's follow-up must not crash a worker."""
    monkeypatch.setattr("app.tasks.headline_tasks.SessionLocal", _no_close(db))

    assert sync_headline(999_999) == {"content_id": 999_999, "outcomes": []}
