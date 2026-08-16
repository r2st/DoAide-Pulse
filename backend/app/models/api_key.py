"""Per-project API keys: the credential a machine uses instead of a password.

Everything else in Herald authenticates with a bearer token minted by
``POST /auth/login``. That token is deliberately short-lived, it is tied to a
*person*, and it is invalidated wholesale when that person resets their
password — all correct for a browser, and all wrong for the CI job that wants
to ask "what has this project published this week?" every time it builds. A job
cannot re-enter a password, and the one thing it must not do is hold one.

So: a second credential type, with three properties the session token does not
have.

* **Scoped.** A key carries an explicit list of :class:`ApiKeyScope` values and
  can do nothing outside it. A build that only reads content cannot publish,
  even though the human who minted the key can. The session token has no such
  vocabulary — it is the whole account or nothing.

* **Bounded to one project.** ``project_id`` is not nullable. An account-wide
  machine credential is the thing people regret: it turns "the key on the
  staging repo leaked" into an incident spanning every project on the account.
  Narrowing it to one project is the difference between revoking one key and
  auditing everything.

* **Rotatable without an outage.** :func:`app.services.api_keys.rotate` mints
  the replacement *before* retiring the original, and the original can be given
  a grace window in which both work. Rotation that invalidates the old key at
  the instant the new one appears is rotation nobody does, because it requires
  redeploying every consumer in the same second.

The stored form is a SHA-256 digest, never the token. See
:func:`app.services.api_keys.fingerprint` for why that is the right hash here
and bcrypt — which every *password* in this tree uses — is not.
"""
from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy 2.0 resolves the
# `Mapped[...]` annotations at class-definition time and needs the real name.
from datetime import datetime  # noqa: TC003
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import TimestampMixin, as_aware, utcnow

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.user import User


class ApiKeyScope(str, Enum):
    """What a key is allowed to do.

    Coarse on purpose. A scope per endpoint reads as thorough and is in
    practice unusable: nobody can say which of thirty scopes their build needs,
    so everybody ticks all of them and the mechanism stops meaning anything.
    Three verbs over the two nouns a machine actually touches is a set somebody
    can choose from correctly on the first try.
    """

    #: Read pieces and their publication state. The scope a status badge, a
    #: changelog page or a "what shipped this week" bot needs.
    CONTENT_READ = "content:read"
    #: File an idea for a human or the autopilot to pick up. Deliberately the
    #: only *write* a key can do to content: a machine may put something in the
    #: queue, and may not put something in front of a reader.
    CONTENT_WRITE = "content:write"
    #: Read engagement numbers. Split from ``content:read`` because the
    #: dashboard-scraping use case wants only this, and a key that can read
    #: performance data has no reason to also read unpublished drafts.
    ANALYTICS_READ = "analytics:read"


#: Every scope, in the order the API presents them. A tuple rather than
#: ``list(ApiKeyScope)`` so the catalogue endpoint's order is a decision rather
#: than an accident of enum declaration.
ALL_SCOPES: tuple[ApiKeyScope, ...] = (
    ApiKeyScope.CONTENT_READ,
    ApiKeyScope.CONTENT_WRITE,
    ApiKeyScope.ANALYTICS_READ,
)


class ApiKey(Base, TimestampMixin):
    """One machine credential, scoped to one project."""

    __tablename__ = "api_keys"
    __table_args__ = (
        # The management listing is "this project's keys, newest first", and
        # the settings page renders it on every load.
        Index("ix_api_keys_project_created", "project_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Denormalised from ``project.user_id``. Authentication resolves a key to
    #: an owner on every machine request, and doing that through a join to
    #: ``projects`` would put a second query on the hot path of every call.
    #: Kept honest by :func:`app.services.api_keys.mint`, which is the only
    #: writer, and by the ownership guard on every management route.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: What a human calls it — "release-notes CI", "status page". Never used to
    #: look a key up; it exists so the revoke button names something.
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    #: The first segment of the token, stored in the clear and unique across
    #: the install. This is the lookup handle: authentication finds the row by
    #: prefix and *then* compares digests, so a request costs one indexed
    #: SELECT rather than a scan comparing every key's hash.
    #:
    #: Storing it is not a leak. It identifies the key; it does not
    #: authenticate as it — the secret half never appears here.
    prefix: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    #: SHA-256 of the whole token, hex. See ``api_keys.fingerprint``.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The scope values this key carries, as strings. Stored as JSON rather
    #: than a join table: it is a short list, it is read on every request, and
    #: nothing ever queries "which keys have scope X".
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    #: When the key stops working, or NULL for "until revoked". A rotation with
    #: a grace window sets this on the *old* key — see ``api_keys.rotate``.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set by an explicit revoke, and never unset. Revocation is one-way: a key
    #: that can be un-revoked is a key whose incident report is a guess.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Last successful authentication, coarsened — see ``api_keys.touch``. The
    #: question it answers is "is anything still using this?", which is what
    #: makes a key safe to delete.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: The key this one replaced, if it was minted by a rotation. Nullable and
    #: ``SET NULL`` on delete: the ancestor is history, and losing the history
    #: must not take the working key with it.
    #:
    #: Indexed like every other foreign key here. Nothing queries by it today —
    #: it is read one row at a time, by id — but ``ON DELETE SET NULL`` means
    #: the database itself looks rows up by this column whenever a key is
    #: deleted, and an unindexed one turns that into a scan of the table.
    rotated_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), index=True
    )

    user: Mapped[User] = relationship(back_populates="api_keys")
    project: Mapped[Project] = relationship(back_populates="api_keys")

    def has_scope(self, scope: ApiKeyScope) -> bool:
        """Whether this key carries *scope*.

        Compares against the stored strings rather than coercing them back to
        enum members: a scope this Herald no longer defines must read as absent,
        not raise ``ValueError`` deep inside an auth dependency.
        """
        return scope.value in (self.scopes or [])

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """Whether the key's clock has run out. No expiry means never."""
        if self.expires_at is None:
            return False
        return as_aware(self.expires_at) <= (now or utcnow())

    def is_usable(self, *, now: datetime | None = None) -> bool:
        """Whether the key would authenticate right now, ignoring the secret.

        The digest comparison is deliberately not part of this: the caller
        checks the secret, and this answers everything else — revoked, expired,
        both, or neither.
        """
        return self.revoked_at is None and not self.is_expired(now=now)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<ApiKey {self.prefix} project={self.project_id}>"


__all__ = ["ALL_SCOPES", "ApiKey", "ApiKeyScope"]
