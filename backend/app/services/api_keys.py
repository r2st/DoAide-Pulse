"""Minting, checking and rotating the machine credentials in ``api_keys``.

The token is three parts joined by underscores::

    hrld_a1b2c3d4e5f6_9tX...43-urlsafe-characters

* ``hrld`` — a fixed marker. Present so a leaked string is recognisably a
  Herald credential: GitHub's and GitLab's secret scanners match on prefixes,
  and a token that looks like any other base64 blob is one nobody can grep the
  logs for either.
* the **prefix** — twelve hex characters, stored in the clear and unique. This
  is the lookup handle, and the reason authentication is one indexed SELECT
  rather than a table scan comparing digests.
* the **secret** — 32 bytes from :mod:`secrets`, URL-safe. The only part that
  matters, and the only part Herald does not keep.

The stored form is a plain SHA-256 of the whole token. That is a deliberate
departure from :func:`app.security.hash_password`, which uses bcrypt at a work
factor chosen to be slow, and the reasoning does not carry over:

* bcrypt is slow because a *password* has perhaps 40 bits of entropy and a
  stolen digest is therefore worth grinding. This secret has 256 bits from a
  CSPRNG. There is no dictionary, no reuse across sites, and no offline attack
  that finishes.
* the cost lands in a different place. A password is verified once per login;
  an API key is verified on *every request*, so a 230ms hash would be 230ms
  added to each call — turning a rate limit into a queue and a CI poll into a
  timeout.

What SHA-256 does still buy is that a database dump does not hand over working
credentials, which is the property that actually matters here. The comparison
goes through :func:`hmac.compare_digest` regardless: the digests are the same
length either way, but a timing-safe compare costs nothing and means one less
thing to reason about.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.api_key import ApiKey, ApiKeyScope
from app.models.mixins import as_aware, utcnow
from app.models.project import Project
from app.models.user import User

logger = logging.getLogger(__name__)

#: Marks the string as a Herald credential. See the module docstring.
TOKEN_MARKER = "hrld"

#: Bytes of randomness in the lookup prefix. Six bytes is twelve hex characters
#: — enough that colliding is not a thing that happens, short enough to read out
#: over a call when somebody is identifying which key is failing.
_PREFIX_BYTES = 6

#: Bytes of randomness in the secret half.
_SECRET_BYTES = 32

#: How stale ``last_used_at`` is allowed to get. Writing it on every
#: authentication would put an UPDATE and a COMMIT on the read path of an
#: endpoint whose entire job is to be cheap — a status badge polling every
#: thirty seconds would generate a write every thirty seconds, forever, per key.
#:
#: The column answers "is anything still using this key?", and for that question
#: an hour's resolution is indistinguishable from a millisecond's.
TOUCH_INTERVAL = timedelta(hours=1)

#: The longest life a key may be given at mint time, in days. Not a security
#: boundary — a key with no expiry at all is still allowed, because a CI job
#: that dies on a Sunday because somebody chose a year is worse than a key that
#: outlives its usefulness. It is a bound on a number that reaches a
#: ``timedelta``, where a caller's ``99999999`` would otherwise raise
#: ``OverflowError`` from inside a request.
MAX_EXPIRY_DAYS = 3650

#: How long the previous key may keep working after a rotation, at most. Long
#: enough for a deploy that has to go through a change window; short enough
#: that "rotated" and "still live for a quarter" are not the same state.
MAX_GRACE_HOURS = 720


class ApiKeyError(ValueError):
    """A key cannot be minted or rotated as asked. The message is user-facing."""


def fingerprint(token: str) -> str:
    """The stored digest of *token* — SHA-256, hex.

    Named for what it is rather than ``hash_token``: there is a
    ``hash_password`` in this tree that means something materially different,
    and two similarly-named functions with different threat models is how the
    wrong one gets called.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate() -> tuple[str, str]:
    """A fresh ``(token, prefix)`` pair. The token is returned exactly once."""
    prefix = secrets.token_hex(_PREFIX_BYTES)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    return f"{TOKEN_MARKER}_{prefix}_{secret}", prefix


def split(token: str) -> str | None:
    """The prefix inside *token*, or ``None`` if it is not one of ours.

    The ``maxsplit`` is load-bearing, not tidiness. ``secrets.token_urlsafe``
    emits base64url, whose alphabet includes ``-`` **and** ``_``, so roughly
    every second token has an underscore inside its secret half. A bare
    ``split("_")`` therefore returns four or more parts for a perfectly valid
    token and this function would answer ``None`` — an intermittent 401 on a
    credential that is fine, reproducible on about half the keys ever minted.

    Splitting into exactly three parts and checking the marker; anything else is
    not a Herald token and is refused without touching the database.
    """
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_MARKER:
        return None
    prefix, secret = parts[1], parts[2]
    if not prefix or not secret:
        return None
    return prefix


def normalize_scopes(scopes: list[str] | list[ApiKeyScope]) -> list[str]:
    """Validate and canonicalise a requested scope list.

    Deduplicated and put in :data:`app.models.api_key.ALL_SCOPES` order, so a
    key minted with ``["analytics:read", "content:read"]`` reads back the same
    as one minted with the reverse — two keys that can do the same things
    should not look different in the UI.
    """
    from app.models.api_key import ALL_SCOPES

    seen: set[str] = set()
    for raw in scopes:
        value = raw.value if isinstance(raw, ApiKeyScope) else str(raw)
        try:
            seen.add(ApiKeyScope(value).value)
        except ValueError as exc:
            allowed = ", ".join(s.value for s in ALL_SCOPES)
            raise ApiKeyError(
                f"Unknown scope: {value}. Choose from: {allowed}."
            ) from exc
    if not seen:
        raise ApiKeyError("Grant at least one scope.")
    return [s.value for s in ALL_SCOPES if s.value in seen]


def expiry_from_days(days: int | None, *, now: datetime | None = None) -> datetime | None:
    """Turn "expires in N days" into a timestamp, or ``None`` for never."""
    if days is None:
        return None
    if days <= 0:
        raise ApiKeyError("Expiry must be at least one day away.")
    if days > MAX_EXPIRY_DAYS:
        raise ApiKeyError(f"Expiry cannot be more than {MAX_EXPIRY_DAYS} days out.")
    return (now or utcnow()) + timedelta(days=days)


def mint(
    db: Session,
    *,
    project: Project,
    name: str,
    scopes: list[str] | list[ApiKeyScope],
    expires_at: datetime | None = None,
    rotated_from: ApiKey | None = None,
) -> tuple[ApiKey, str]:
    """Create a key on *project* and return it with its plaintext token.

    Does not commit. The caller decides what else belongs in the transaction —
    which matters for :func:`rotate`, where minting the replacement and
    retiring the original have to land together or not at all.

    ``user_id`` is copied from the project rather than taken as an argument.
    That is the whole of the denormalisation's correctness: there is one writer,
    and it reads the owner off the row it is attaching to.
    """
    clean = name.strip()
    if not clean:
        raise ApiKeyError("Give the key a name.")

    token, prefix = generate()
    key = ApiKey(
        user_id=project.user_id,
        project_id=project.id,
        name=clean[:120],
        prefix=prefix,
        token_hash=fingerprint(token),
        scopes=normalize_scopes(scopes),
        expires_at=expires_at,
        rotated_from_id=rotated_from.id if rotated_from is not None else None,
    )
    db.add(key)
    return key, token


def authenticate(
    db: Session, token: str, *, now: datetime | None = None
) -> ApiKey | None:
    """Resolve *token* to a usable key, or ``None``.

    ``None`` for every failure — malformed, unknown, revoked, expired, or
    belonging to a deactivated account. The caller turns that into one 401 with
    one message, because distinguishing "no such key" from "revoked key" tells
    somebody holding a stolen token which of their guesses was once real.

    The account's ``is_active`` is checked here for the same reason the metrics
    sweep checks it: switching an account off has to close the doors it opened,
    and a machine credential is a door that nothing else would have closed.
    """
    if not token:
        return None
    prefix = split(token)
    if prefix is None:
        return None

    key = db.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if key is None:
        # Still spend the hash. Returning early on an unknown prefix makes the
        # "no such key" answer measurably faster than the "wrong secret" one,
        # which turns the endpoint into an oracle for whether a prefix is real.
        fingerprint(token)
        return None

    if not hmac.compare_digest(key.token_hash, fingerprint(token)):
        return None
    if not key.is_usable(now=now):
        return None

    owner = db.get(User, key.user_id)
    if owner is None or not owner.is_active:
        return None
    return key


def touch(db: Session, key: ApiKey, *, now: datetime | None = None) -> bool:
    """Record that *key* was used, at most once per :data:`TOUCH_INTERVAL`.

    Returns whether it wrote. Commits when it does — the caller is on a read
    path that may not have a transaction of its own to join, and a usage stamp
    that only lands if something else happens to commit is a usage stamp that
    mostly does not land.

    Never raises. A failure to note that a key was used must not fail the
    request the key was used for.
    """
    moment = now or utcnow()
    last = key.last_used_at
    if last is not None and moment - as_aware(last) < TOUCH_INTERVAL:
        return False
    try:
        key.last_used_at = moment
        db.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to record use of api key %s", key.prefix)
        db.rollback()
        return False
    return True


def revoke(db: Session, key: ApiKey, *, now: datetime | None = None) -> ApiKey:
    """Stop *key* working, now and permanently. Idempotent.

    Re-revoking keeps the original timestamp: the answer to "when did this stop
    working" should not move because somebody pressed the button twice.
    """
    if key.revoked_at is None:
        key.revoked_at = now or utcnow()
        db.commit()
        db.refresh(key)
    return key


def rotate(
    db: Session,
    key: ApiKey,
    *,
    grace_hours: int = 0,
    now: datetime | None = None,
) -> tuple[ApiKey, str]:
    """Replace *key* with a new one carrying the same name and scopes.

    Returns the replacement and its plaintext token, and commits both halves
    together — the new key existing while the old one is still live is the
    entire point, and the reverse (old one retired, new one lost to a failed
    INSERT) is an outage.

    ``grace_hours`` is how long the original keeps working. Zero — the default
    — revokes it immediately, which is what somebody rotating a *leaked* key
    wants and is why it is the default rather than the convenient answer. A
    routine rotation passes enough hours to redeploy the consumers.

    The replacement inherits the original's ``expires_at`` only if that is
    still in the future; a rotation is not a way to resurrect an expired key,
    but neither should it silently extend a key that was deliberately dated.
    """
    moment = now or utcnow()
    if key.revoked_at is not None:
        raise ApiKeyError("That key is already revoked. Create a new one instead.")
    if grace_hours < 0:
        raise ApiKeyError("Grace period cannot be negative.")
    if grace_hours > MAX_GRACE_HOURS:
        raise ApiKeyError(
            f"Grace period cannot be more than {MAX_GRACE_HOURS} hours."
        )

    inherited = key.expires_at
    if inherited is not None and as_aware(inherited) <= moment:
        inherited = None

    project = db.get(Project, key.project_id)
    if project is None:  # pragma: no cover - FK makes this unreachable
        raise ApiKeyError("That key's project no longer exists.")

    replacement, token = mint(
        db,
        project=project,
        name=key.name,
        scopes=list(key.scopes or []),
        expires_at=inherited,
        rotated_from=key,
    )
    # Flush so the replacement has an id before it is pointed at its ancestor.
    db.flush()

    if grace_hours == 0:
        key.revoked_at = moment
    else:
        # An expiry, not a revocation: the old key is still a working
        # credential until the window closes, and ``is_usable`` reads the two
        # differently on purpose.
        window = moment + timedelta(hours=grace_hours)
        current = as_aware(key.expires_at) if key.expires_at else None
        # Never *extends* a life. A key already dated to die inside the window
        # keeps its own date.
        key.expires_at = window if current is None or current > window else current

    db.commit()
    db.refresh(replacement)
    db.refresh(key)
    return replacement, token


__all__ = [
    "MAX_EXPIRY_DAYS",
    "MAX_GRACE_HOURS",
    "TOKEN_MARKER",
    "TOUCH_INTERVAL",
    "ApiKeyError",
    "authenticate",
    "expiry_from_days",
    "fingerprint",
    "generate",
    "mint",
    "normalize_scopes",
    "revoke",
    "rotate",
    "split",
    "touch",
]
