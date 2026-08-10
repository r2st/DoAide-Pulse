"""Two lookups that must not throw on input from outside: enum coercion and
the rate-limit bucket.

Both sit in front of everything else. ``Platform(...)`` is applied to values
read out of a JSON column and off the wire, and ``client_key`` runs on every
single request before any handler does. A ``TypeError`` in either is a 500 on a
path that has no business failing.
"""
from __future__ import annotations

import pytest

from app.models.publication import Platform
from app.ratelimit import client_key

# --------------------------------------------------------------------------- #
# Platform._missing_                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("spelling", ["devto", "DEVTO", "  DevTo  "])
def test_every_spelling_of_a_real_platform_resolves(spelling):
    """Rows written by the ORM hold the member *name*; the API writes the value."""
    assert Platform(spelling) is Platform.DEVTO


@pytest.mark.parametrize("value", [123, None, 4.5, ["devto"], {"platform": "devto"}])
def test_a_non_string_is_refused_rather_than_crashing_the_lookup(value):
    """``_missing_`` is handed whatever was in the column.

    Returning ``None`` for a non-string is what turns it into the ``ValueError``
    the callers already handle, instead of an ``AttributeError`` from inside the
    enum machinery.
    """
    with pytest.raises(ValueError):
        Platform(value)


def test_a_string_that_is_not_a_platform_is_still_a_value_error():
    with pytest.raises(ValueError):
        Platform("tumblr")


# --------------------------------------------------------------------------- #
# client_key                                                                  #
# --------------------------------------------------------------------------- #


class _Request:
    """The two attributes ``client_key`` actually reads."""

    def __init__(self, headers: dict[str, str], peer: str = "10.0.0.1"):
        self.headers = headers
        self.client = type("Client", (), {"host": peer})()
        self.scope = {"client": (peer, 1234)}


@pytest.fixture
def trusting(monkeypatch):
    monkeypatch.setattr(
        "app.ratelimit.settings.rate_limit_trust_forwarded_for", True
    )


def test_the_peer_is_used_when_the_proxy_sent_no_header(trusting):
    """Trusting the header does not mean requiring it — a direct request to the
    app port has none, and must still land in a bucket."""
    assert client_key(_Request({}, peer="203.0.113.9")) == "203.0.113.9"


@pytest.mark.parametrize("header", ["", "   ", ",", " , "])
def test_a_header_with_nothing_usable_in_it_falls_back_to_the_peer(trusting, header):
    """An empty rightmost entry must not become an empty bucket key.

    Every caller sending the same junk header would otherwise share one budget —
    which is either a free denial of service against everyone else, or a way to
    sit in a bucket nobody else is counted against, depending on the endpoint.
    """
    assert client_key(_Request({"x-forwarded-for": header}, peer="203.0.113.9")) == (
        "203.0.113.9"
    )


def test_the_rightmost_entry_wins_when_there_is_one(trusting):
    key = client_key(
        _Request({"x-forwarded-for": "1.2.3.4, 198.51.100.7"}, peer="10.0.0.1")
    )

    assert key == "198.51.100.7"


def test_the_header_is_ignored_entirely_when_it_is_not_trusted(monkeypatch):
    monkeypatch.setattr(
        "app.ratelimit.settings.rate_limit_trust_forwarded_for", False
    )

    key = client_key(_Request({"x-forwarded-for": "198.51.100.7"}, peer="10.0.0.1"))

    assert key == "10.0.0.1"
