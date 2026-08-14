"""Rate limits on the endpoints an outsider can reach without a token.

``app.ratelimit`` used to say that everything outside ``/auth/*`` needed a
bearer token. That was true when it was written and quietly stopped being true:
the public RSS feed and the liveness probe both landed afterwards, both do real
work per request, and neither carried a limit. The feed is the one that matters
— two queries and an XML render, keyed on a small integer anyone can walk.

So the interesting test here is not any single limit. It is
:func:`test_every_unauthenticated_route_carries_a_rate_limit`, which walks the
live route table and fails when a new endpoint is reachable anonymously without
one. A list in a docstring goes stale; that one cannot.

The limits are the real configured ones — slowapi reads the limit string when
the decorator is applied at import time, so a test cannot lower them and has to
spend the actual budget instead (same reasoning as ``test_ratelimit``).
"""
from __future__ import annotations

import inspect

import pytest
from fastapi.routing import APIRoute

from app.config import settings
from app.main import app
from app.models.content import Content, ContentStatus, ContentType
from app.routers import misc

FEED = "/api/v1/projects/{}/feed.xml"
HEALTH = "/api/v1/health"
EVENTS = "/api/v1/webhooks/events"
KINDS = "/api/v1/triggers/kinds"

#: Dependencies that mean "a valid token got you here".
_AUTH_DEPENDENCIES = {"get_current_user"}

#: The one anonymous route with no limit, and why that is allowed: it is a
#: two-key dict built from settings, with no database, no I/O and no user input.
#: Anything else added to this set needs the same argument made for it.
_UNLIMITED_BY_DESIGN = {("GET", "/")}


def _api_routes():
    """Every APIRoute in the app, including those behind an included router.

    FastAPI nests included routers rather than flattening them into
    ``app.routes``, so a naive walk finds only ``/``  — which would make the
    sweep below pass by looking at nothing.
    """

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            elif hasattr(route, "original_router"):
                yield from walk(route.original_router.routes)
            elif hasattr(route, "routes"):
                yield from walk(route.routes)

    return list(walk(app.routes))


def _dependency_names(dependant, found: set[str]) -> set[str]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            found.add(getattr(sub.call, "__name__", ""))
        _dependency_names(sub, found)
    return found


def _is_anonymous(route: APIRoute) -> bool:
    return not (_dependency_names(route.dependant, set()) & _AUTH_DEPENDENCIES)


def _is_limited(route: APIRoute) -> bool:
    """Whether a limiter decorator wraps this endpoint.

    Read off the source rather than the function object: slowapi's decorator
    preserves the signature via ``functools.wraps``, so there is no attribute on
    the wrapper that distinguishes it.
    """
    try:
        return "limiter.limit" in inspect.getsource(route.endpoint)
    except (OSError, TypeError):  # pragma: no cover — every endpoint has source
        return False


# --------------------------------------------------------------------------- #
# The sweep                                                                    #
# --------------------------------------------------------------------------- #


def test_the_route_walk_actually_finds_the_api():
    """Guard the guard.

    Every assertion below is "no route is unlimited", which passes trivially if
    the walk returns nothing — and it did return nothing on the first attempt,
    because included routers are nested. Pin a floor.
    """
    routes = _api_routes()
    assert len(routes) > 50
    paths = {r.path for r in routes}
    assert "/health" in paths
    assert "/projects/{project_id}/feed.xml" in paths


def test_every_unauthenticated_route_carries_a_rate_limit():
    """The regression guard for the whole anonymous surface.

    A new endpoint that forgets ``get_current_user`` is a authorization bug and
    something else will catch it. A new endpoint that is *meant* to be public
    and forgets the limiter is silent: it works perfectly, for everybody, as
    fast as they can ask.
    """
    unlimited = sorted(
        (sorted(route.methods)[0], route.path)
        for route in _api_routes()
        if _is_anonymous(route) and not _is_limited(route)
    )
    assert set(unlimited) == _UNLIMITED_BY_DESIGN


def test_every_limited_endpoint_can_receive_the_rate_limit_headers():
    """The trap that comes with the limiter, and it fails *open*.

    ``headers_enabled=True`` makes slowapi write ``X-RateLimit-*`` onto the
    response after the endpoint returns. It needs somewhere to write them: an
    endpoint that returns a ``Response`` is fine, but one that returns a model
    must declare ``response: Response`` so FastAPI supplies the object. Without
    it slowapi raises — on *every* call, not only a limited one, so adding a
    limit to a JSON endpoint turns it into a 500 and the limit is what gets
    blamed last. Both new constant-list endpoints hit this before it was found.
    """
    def names(annotation) -> str:
        # The routers use `from __future__ import annotations`, so annotations
        # arrive as strings rather than classes. Handle both.
        return getattr(annotation, "__name__", None) or str(annotation)

    missing = []
    for route in _api_routes():
        if not _is_limited(route):
            continue
        signature = inspect.signature(route.endpoint)
        takes_response = any(
            names(p.annotation) == "Response" for p in signature.parameters.values()
        )
        returns_response = names(signature.return_annotation) == "Response"
        if not (takes_response or returns_response):
            missing.append((sorted(route.methods)[0], route.path))

    assert missing == []


def test_the_authenticated_surface_is_not_accidentally_anonymous():
    """The sweep above is only meaningful if `_is_anonymous` can say no."""
    anonymous = {
        (sorted(r.methods)[0], r.path) for r in _api_routes() if _is_anonymous(r)
    }
    assert ("GET", "/projects") not in anonymous
    assert ("GET", "/content") not in anonymous
    # And the ones that genuinely are public are seen as such.
    assert ("GET", "/projects/{project_id}/feed.xml") in anonymous
    assert ("GET", "/health") in anonymous


# --------------------------------------------------------------------------- #
# The feed                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def feed_project(project, db):
    """A project whose feed answers 200.

    The bare ``project`` fixture publishes nothing, and a feed with no
    published items is a 404 — it would otherwise name the project to an
    anonymous caller walking ids. These tests are about the limiter, so they
    need the served case, not the enumeration guard.
    """
    db.add(
        Content(
            project_id=project.id, content_type=ContentType.ANNOUNCEMENT,
            title="Herald 1.0", slug="herald-1-0", status=ContentStatus.PUBLISHED,
        )
    )
    db.commit()
    return project


def test_the_public_feed_is_limited(client, feed_project):
    """20/minute. The 21st request is refused rather than served."""
    assert settings.rate_limit_public_feed.startswith("20/minute")
    url = FEED.format(feed_project.id)

    for _ in range(20):
        assert client.get(url).status_code == 200

    resp = client.get(url)
    assert resp.status_code == 429
    assert "Too many requests" in resp.json()["detail"]
    assert resp.headers.get("retry-after")


def test_the_feed_budget_is_per_caller_not_global(client, feed_project):
    """One noisy reader must not take the feed down for everyone else.

    This is the failure mode a global counter would introduce while fixing the
    other one, and it matters more here than on login: the feed is a thing real
    readers poll on a schedule nobody controls.
    """
    url = FEED.format(feed_project.id)
    for _ in range(20):
        client.get(url, headers={"X-Forwarded-For": "203.0.113.10"})
    assert (
        client.get(url, headers={"X-Forwarded-For": "203.0.113.10"}).status_code == 429
    )

    other = client.get(url, headers={"X-Forwarded-For": "198.51.100.7"})
    assert other.status_code == 200


def test_a_missing_project_still_spends_the_budget(client):
    """Otherwise the limit is bypassed by the cheapest possible request.

    Walking project ids is the way you find which ones exist, and a 404 that
    did not count would leave that walk unbounded — the enumeration is the
    thing being rate-limited, not just the successful renders.
    """
    for _ in range(20):
        assert client.get(FEED.format(999_999)).status_code == 404
    assert client.get(FEED.format(999_999)).status_code == 429


def test_the_limit_does_not_change_what_the_feed_serves(client, feed_project):
    """A limit that altered the response would be a different bug."""
    resp = client.get(FEED.format(feed_project.id))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/rss+xml")
    assert "<rss" in resp.text


# --------------------------------------------------------------------------- #
# The liveness probe                                                           #
# --------------------------------------------------------------------------- #


@pytest.fixture
def redis_up(monkeypatch):
    # Same reasoning as test_health: whether a Redis is listening on the
    # machine running the tests is not what these assertions are about.
    monkeypatch.setattr(misc, "_check_redis", lambda: misc._Probe(True))


def test_health_leaves_caddy_an_order_of_magnitude_of_headroom():
    """The number is load-bearing in a way the others are not.

    Caddy polls ``/api/v1/health`` every 30s (deploy/Caddyfile.herald) and pulls
    the upstream out of the pool when it does not get a 200 — so a limit set
    below the poll rate would not throttle an attacker, it would take the site
    down by itself. Two requests a minute against sixty is the margin, and a
    deploy's health retries share that same bucket.
    """
    limit, _, window = settings.rate_limit_health.partition("/")
    assert window == "minute"
    caddy_polls_per_minute = 2
    assert int(limit) >= caddy_polls_per_minute * 10


def test_health_is_limited_but_only_after_real_use(client, redis_up):
    """60/minute: the probe answers well past any plausible poll rate."""
    for _ in range(60):
        assert client.get(HEALTH).status_code == 200

    assert client.get(HEALTH).status_code == 429


def test_a_flooded_health_bucket_does_not_affect_another_caller(client, redis_up):
    """Caddy's own probe survives a flood arriving through Caddy.

    The active health check carries no ``X-Forwarded-For`` — Caddy generates it
    rather than proxying it — so it buckets against the peer address while
    public traffic buckets per visitor. If those shared a counter, anyone could
    fail the health check for the whole site.
    """
    for _ in range(60):
        client.get(HEALTH, headers={"X-Forwarded-For": "203.0.113.10"})
    assert (
        client.get(HEALTH, headers={"X-Forwarded-For": "203.0.113.10"}).status_code
        == 429
    )

    # The unforwarded request — the shape Caddy's own probe has.
    assert client.get(HEALTH).status_code == 200


# --------------------------------------------------------------------------- #
# The constant lists                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url", [EVENTS, KINDS])
def test_the_public_constant_lists_are_limited(client, url):
    assert settings.rate_limit_public_read == "60/minute"
    for _ in range(60):
        assert client.get(url).status_code == 200
    assert client.get(url).status_code == 429


@pytest.mark.parametrize("url", [EVENTS, KINDS])
def test_the_constant_lists_still_return_their_contents(client, url):
    """The `request` parameter slowapi needs must not reach the response."""
    body = client.get(url).json()
    assert isinstance(body, list) and body
    assert "request" not in body[0]
