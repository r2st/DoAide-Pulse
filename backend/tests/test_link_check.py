"""Link validation.

The network is stubbed with `httpx.MockTransport`, so what is under test is the
extraction and the three-valued verdict — which is where the judgement lives.
The one rule worth stating twice: only a definitive 404/410 counts as broken,
because a checker that blocks on timeouts gets switched off within a week.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.services import link_check


def _soon() -> str:
    """A publish time far enough ahead that nothing goes out during the test.

    Not a far-future sentinel: scheduling refuses anything past a one-year
    horizon, so that a mistyped year cannot park a post for a decade.
    """
    return (datetime.now(UTC) + timedelta(days=7)).isoformat()


# --------------------------------------------------------------------------- #
# Extraction                                                                   #
# --------------------------------------------------------------------------- #


def test_extracts_every_link_shape_once():
    body = """
## Setup

See the [docs](https://example.com/docs) and the [same docs](https://example.com/docs).
Bare: https://example.com/bare — and an autolink <https://example.com/auto>.
An image: ![chart](https://cdn.example.com/chart.png)
"""
    assert link_check.extract_urls(body) == [
        "https://example.com/docs",
        "https://cdn.example.com/chart.png",
        "https://example.com/auto",
        "https://example.com/bare",
    ]


def test_a_markdown_title_is_not_part_of_the_url():
    body = '[docs](https://example.com/docs "The documentation")'
    assert link_check.extract_urls(body) == ["https://example.com/docs"]


def test_code_is_not_checked():
    """A URL in a snippet is illustrative — HEAD-checking it proves nothing."""
    body = """
Run this:

```bash
curl https://api.example.com/v1/things
```

Inline `https://api.example.com/inline` too, but do check
https://example.com/real.
"""
    assert link_check.extract_urls(body) == ["https://example.com/real"]


def test_trailing_sentence_punctuation_is_not_part_of_the_url():
    body = "Read https://example.com/docs. Then https://example.com/other, maybe."
    assert link_check.extract_urls(body) == [
        "https://example.com/docs",
        "https://example.com/other",
    ]


def test_non_http_links_are_out_of_scope():
    body = "[mail](mailto:a@example.com) [anchor](#setup) [rel](/docs/setup) [ftp](ftp://x.example.com)"
    assert link_check.extract_urls(body) == []


def test_extraction_respects_a_limit():
    body = " ".join(f"https://example.com/{n}" for n in range(10))
    assert len(link_check.extract_urls(body, limit=3)) == 3


# --------------------------------------------------------------------------- #
# Verdicts                                                                     #
# --------------------------------------------------------------------------- #


def _client(handler) -> httpx.Client:
    # follow_redirects=False mirrors the production client; redirect following
    # is now handled by _follow_safely with SSRF checks on each hop.
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """Every hostname resolves to a public address unless a test says otherwise.

    Without this the verdict tests would depend on the machine's resolver.
    """
    monkeypatch.setattr(
        link_check, "_unreachable_for_a_reader", lambda url: None
    )


@pytest.mark.parametrize("code", [200, 204, 301, 302, 308])
def test_a_reachable_page_is_ok(code):
    with _client(lambda request: httpx.Response(code)) as client:
        status = link_check.check_url("https://example.com/x", client=client)
    assert status.status == link_check.OK
    assert not status.is_broken


@pytest.mark.parametrize("code", [404, 410])
def test_gone_means_broken(code):
    with _client(lambda request: httpx.Response(code)) as client:
        status = link_check.check_url("https://example.com/x", client=client)
    assert status.status == link_check.BROKEN
    assert status.http_status == code
    assert "does not exist" in status.detail


@pytest.mark.parametrize("code", [401, 429, 500, 503])
def test_everything_else_is_unknown_not_broken(code):
    """A paywall, a rate limit or a bad afternoon is not evidence of a dead page."""
    with _client(lambda request: httpx.Response(code)) as client:
        status = link_check.check_url("https://example.com/x", client=client)
    assert status.status == link_check.UNKNOWN
    assert not status.is_broken


def test_a_timeout_is_unknown_not_broken():
    def _timeout(request):
        raise httpx.ConnectTimeout("too slow", request=request)

    with _client(_timeout) as client:
        status = link_check.check_url("https://example.com/x", client=client)
    assert status.status == link_check.UNKNOWN
    assert "by hand" in status.detail


@pytest.mark.parametrize("code", [403, 405, 501])
def test_a_head_refusal_is_retried_with_get(code):
    """Plenty of servers refuse HEAD and serve GET perfectly well."""
    seen: list[str] = []

    def _handler(request):
        seen.append(request.method)
        return httpx.Response(code if request.method == "HEAD" else 200)

    with _client(_handler) as client:
        status = link_check.check_url("https://example.com/x", client=client)

    assert seen == ["HEAD", "GET"]
    assert status.status == link_check.OK


def test_a_head_only_404_is_not_retried():
    """404 is already an answer; a second request would only be slower."""
    seen: list[str] = []

    def _handler(request):
        seen.append(request.method)
        return httpx.Response(404)

    with _client(_handler) as client:
        link_check.check_url("https://example.com/x", client=client)

    assert seen == ["HEAD"]


# --------------------------------------------------------------------------- #
# Addresses nobody can reach                                                   #
# --------------------------------------------------------------------------- #


def test_a_loopback_link_is_broken_without_a_request(monkeypatch):
    """It is dead for every reader, and probing it would scan our own network."""
    monkeypatch.undo()  # drop the _no_dns override for this one
    monkeypatch.setattr(
        link_check.socket,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("127.0.0.1", 0))],
    )

    def _explode(request):  # pragma: no cover - must never run
        raise AssertionError("a private address must not be requested")

    with _client(_explode) as client:
        status = link_check.check_url("http://localhost:8000/docs", client=client)

    assert status.status == link_check.BROKEN
    assert "private or loopback" in status.detail


def test_one_private_record_is_enough_to_refuse_the_host(monkeypatch):
    """A name may carry several A records, and the client picks, not us.

    ``evil.example. A 93.184.216.34`` / ``A 169.254.169.254`` is a two-line
    zone file. Refusing only when *every* address is private waves that
    through and leaves httpx to choose which one it connects to.
    """
    monkeypatch.undo()  # drop the _no_dns override
    monkeypatch.setattr(
        link_check.socket,
        "getaddrinfo",
        lambda host, port: [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("169.254.169.254", 0)),
        ],
    )

    def _explode(request):  # pragma: no cover - must never run
        raise AssertionError("a host with a private record must not be requested")

    with _client(_explode) as client:
        status = link_check.check_url("https://split.example/x", client=client)

    assert status.status == link_check.BROKEN
    assert "169.254.169.254" in status.detail


def test_a_wholly_public_host_still_passes_the_pre_flight(monkeypatch):
    """The guard above must not refuse every host with more than one record."""
    monkeypatch.undo()
    monkeypatch.setattr(
        link_check.socket,
        "getaddrinfo",
        lambda host, port: [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("93.184.216.35", 0)),
        ],
    )
    assert link_check._unreachable_for_a_reader("https://public.example/x") is None


def test_a_webhook_url_with_one_private_record_is_refused(monkeypatch):
    """The same guard, through the door outbound webhooks come in by."""
    from app.services import webhooks

    monkeypatch.undo()
    monkeypatch.setattr(
        link_check.socket,
        "getaddrinfo",
        lambda host, port: [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ],
    )
    with pytest.raises(webhooks.WebhookUrlError, match="127.0.0.1"):
        webhooks.validate_url("https://split.example/hook")


def test_redirect_to_private_ip_is_blocked(monkeypatch):
    """A redirect that targets a private address must be caught (SSRF)."""
    monkeypatch.undo()  # drop the _no_dns override

    call_count = 0

    def _ssrf_check(url):
        # First call (the original URL) passes; redirect target is private.
        nonlocal call_count
        call_count += 1
        if "169.254" in url:
            return "Resolves to a private or loopback address — SSRF blocked."
        return None

    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", _ssrf_check)

    def _redirect(request):
        return httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data/"},
        )

    with _client(_redirect) as client:
        status = link_check.check_url("https://evil.example.com/redir", client=client)

    assert status.status == link_check.BROKEN
    assert "private address" in status.detail.lower() or "SSRF" in status.detail


def test_redirect_to_public_ip_is_followed(monkeypatch):
    """A redirect to a normal public URL should be followed and report OK."""
    monkeypatch.undo()
    monkeypatch.setattr(link_check, "_unreachable_for_a_reader", lambda url: None)

    def _handler(request):
        if "redir" in str(request.url):
            return httpx.Response(302, headers={"location": "https://example.com/dest"})
        return httpx.Response(200)

    with _client(_handler) as client:
        status = link_check.check_url("https://example.com/redir", client=client)

    assert status.status == link_check.OK


def test_an_unresolvable_host_is_left_to_the_request(monkeypatch):
    """A resolver blip and a nonexistent domain look identical from here."""
    monkeypatch.undo()
    import socket as socket_module

    monkeypatch.setattr(
        link_check.socket,
        "getaddrinfo",
        lambda host, port: (_ for _ in ()).throw(socket_module.gaierror("nope")),
    )
    assert link_check._unreachable_for_a_reader("https://nope.example/x") is None


# --------------------------------------------------------------------------- #
# The body-level entry point                                                   #
# --------------------------------------------------------------------------- #


def test_check_body_is_capped(monkeypatch):
    monkeypatch.setattr("app.services.link_check.settings.link_check_max_urls", 2)
    checked: list[list[str]] = []
    monkeypatch.setattr(
        link_check, "check", lambda urls, **kw: checked.append(urls) or []
    )

    body = " ".join(f"https://example.com/{n}" for n in range(9))
    link_check.check_body(body)
    assert checked == [["https://example.com/0", "https://example.com/1"]]


def test_check_body_includes_extra_urls(monkeypatch):
    checked: list[list[str]] = []
    monkeypatch.setattr(
        link_check, "check", lambda urls, **kw: checked.append(urls) or []
    )

    link_check.check_body(
        "See https://example.com/a", extra_urls=["https://cdn.example.com/cover.png"]
    )
    assert checked == [
        ["https://example.com/a", "https://cdn.example.com/cover.png"]
    ]


def test_check_of_nothing_makes_no_requests():
    assert link_check.check([]) == []


def test_broken_filters_to_the_definitive_ones():
    statuses = [
        link_check.LinkStatus("https://a.example/1", link_check.OK),
        link_check.LinkStatus("https://a.example/2", link_check.BROKEN, 404),
        link_check.LinkStatus("https://a.example/3", link_check.UNKNOWN, 403),
    ]
    assert [s.url for s in link_check.broken(statuses)] == ["https://a.example/2"]


# --------------------------------------------------------------------------- #
# The API surface                                                              #
# --------------------------------------------------------------------------- #


def _stub_verdicts(monkeypatch, statuses):
    monkeypatch.setattr(link_check, "check_body", lambda body, **kw: statuses)


@pytest.fixture
def piece(db, project):
    from app.models.content import Content, ContentType

    row = Content(
        project_id=project.id,
        content_type=ContentType.HOW_TO,
        title="Linking things",
        slug="linking-things",
        body_markdown="See the [docs](https://example.com/gone).",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def devto_connected(db, user):
    from app.models.platform_connection import ConnectionStatus, PlatformConnection
    from app.models.publication import Platform
    from app.services.crypto import encrypt_credentials

    db.add(
        PlatformConnection(
            user_id=user.id,
            platform=Platform.DEVTO,
            status=ConnectionStatus.CONNECTED,
            encrypted_credentials=encrypt_credentials({"api_key": "k"}),
        )
    )
    db.commit()


def test_the_links_endpoint_reports_each_verdict(client, auth, piece, monkeypatch):
    _stub_verdicts(
        monkeypatch,
        [
            link_check.LinkStatus("https://example.com/gone", link_check.BROKEN, 404, "d"),
            link_check.LinkStatus("https://example.com/fine", link_check.OK, 200),
        ],
    )

    resp = client.get(f"/api/v1/content/{piece.id}/links", headers=auth)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["checked"] == 2
    assert body["broken_count"] == 1
    assert body["links"][0]["status"] == "broken"
    assert body["links"][0]["http_status"] == 404


def test_publishing_with_a_dead_link_is_refused(
    client, auth, piece, devto_connected, monkeypatch
):
    _stub_verdicts(
        monkeypatch,
        [link_check.LinkStatus("https://example.com/gone", link_check.BROKEN, 404, "d")],
    )

    resp = client.post(
        f"/api/v1/content/{piece.id}/publish",
        headers=auth,
        json={"platforms": ["devto"], "scheduled_for": _soon()},
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "https://example.com/gone" in detail
    assert "allow_broken_links" in detail
    # Nothing was queued — the refusal has to be a refusal.
    assert piece.publications == []


def test_an_unknown_verdict_never_blocks_a_publish(
    client, auth, piece, devto_connected, monkeypatch
):
    """A timeout or a bot-block must not stop the post going out."""
    _stub_verdicts(
        monkeypatch,
        [
            link_check.LinkStatus("https://example.com/slow", link_check.UNKNOWN, None, "d"),
            link_check.LinkStatus("https://example.com/403", link_check.UNKNOWN, 403, "d"),
        ],
    )

    resp = client.post(
        f"/api/v1/content/{piece.id}/publish",
        headers=auth,
        json={"platforms": ["devto"], "scheduled_for": _soon()},
    )
    assert resp.status_code == 200, resp.text


def test_a_dead_link_can_be_overridden(
    client, auth, piece, devto_connected, monkeypatch
):
    _stub_verdicts(
        monkeypatch,
        [link_check.LinkStatus("https://example.com/gone", link_check.BROKEN, 404, "d")],
    )

    resp = client.post(
        f"/api/v1/content/{piece.id}/publish",
        headers=auth,
        json={
            "platforms": ["devto"],
            "allow_broken_links": True,
            "scheduled_for": _soon(),
        },
    )
    assert resp.status_code == 200, resp.text


def test_the_gate_can_be_switched_off(
    client, auth, piece, devto_connected, monkeypatch
):
    monkeypatch.setattr("app.routers.content.settings.link_check_enabled", False)

    def _explode(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("the checker ran with link_check_enabled off")

    monkeypatch.setattr(link_check, "check_body", _explode)

    resp = client.post(
        f"/api/v1/content/{piece.id}/publish",
        headers=auth,
        json={"platforms": ["devto"], "scheduled_for": _soon()},
    )
    assert resp.status_code == 200, resp.text
