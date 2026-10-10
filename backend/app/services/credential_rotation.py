"""Moving stored secrets onto the current encryption key, in the background.

``app.services.crypto`` makes ``TOKEN_ENCRYPTION_KEY`` a list so a rotation does
not break anything the moment it lands: the new key goes on the front, the old
one stays behind it, and every stored secret still reads. This is the other half
— the part that makes the old key eventually *removable*, by re-encrypting each
row under the head of the list.

Three tables hold something encrypted, and all three matter equally:

* ``platform_connections.encrypted_credentials`` — the API tokens Pulse
  publishes with. Losing these stops publishing until a human goes and fetches
  new ones from each platform.
* ``webhooks.encrypted_secret`` — what outbound deliveries are signed with.
  Losing one means every receiver rejects every delivery until its operator is
  given a new secret.
* ``triggers.encrypted_secret`` — what inbound firings are verified against.
  Losing one means the sender is rejected until it is reconfigured.

The sweep is deliberately dumb and re-runnable. It does not track which key a
row was written under, or record rotation state anywhere; it asks
:func:`crypto.is_current` per row and rewrites the ones that answer no. So it is
a no-op on a box that has never rotated, it finishes the job if it is
interrupted halfway, and running it by hand right after a rotation is the same
operation the nightly beat does — just sooner.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.platform_connection import PlatformConnection
from app.models.trigger import Trigger
from app.models.webhook import Webhook
from app.services import crypto

logger = logging.getLogger(__name__)


@dataclass
class RewrapResult:
    """What one sweep did, per table and in total."""

    rewrapped: int = 0
    #: Rows readable under no configured key. Counted, named in the log, and
    #: left exactly as they are — see :func:`rewrap_all`.
    unreadable: int = 0
    by_table: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        """A Celery task's return value has to be JSON, and this is not."""
        return {
            "rewrapped": self.rewrapped,
            "unreadable": self.unreadable,
            "by_table": dict(self.by_table),
        }


#: ``(table name, model, attribute)`` for everything that stores ciphertext.
#: A table added here is swept with no other change; a table *not* here keeps
#: its rows on an old key forever, which is why
#: ``test_credential_key_rotation`` checks this list against the schema rather
#: than trusting it.
_ENCRYPTED_COLUMNS: tuple[tuple[str, type, str], ...] = (
    ("platform_connections", PlatformConnection, "encrypted_credentials"),
    ("webhooks", Webhook, "encrypted_secret"),
    ("triggers", Trigger, "encrypted_secret"),
)


def pending(db: Session) -> int:
    """How many stored secrets are not yet under the current key.

    Read-only, so it is safe to call from a health check or before deciding
    whether the old key can come out of the setting. Zero means the previous key
    is no longer holding anything up.
    """
    total = 0
    for _, model, attribute in _ENCRYPTED_COLUMNS:
        column = getattr(model, attribute)
        for stored in db.scalars(select(column)):
            if not crypto.is_current(stored or ""):
                total += 1
    return total


def rewrap_all(db: Session) -> RewrapResult:
    """Re-encrypt every stale stored secret under the current key.

    Commits once per table rather than once per row or once overall: a rotation
    on a busy install is thousands of rows, one transaction that holds them all
    open blocks the publisher, and one transaction per row turns a sweep into a
    write storm. Per table is small enough to be quick and big enough that a
    partial run leaves a coherent table behind.

    A row that cannot be read under *any* configured key is counted and skipped,
    never deleted or blanked. That row is a secret whose key was dropped from the
    setting too early, and the fix is to put the old key back — which only works
    if the ciphertext is still there. Blanking it would turn a recoverable
    mistake into a permanent one.

    The scan is two passes per table: the first reads only the primary key and
    the encrypted column to identify stale rows without loading the full entity;
    the second loads and updates only the rows that need re-encrypting. On a box
    that has never rotated — the common case — the first pass touches every row
    and the second touches none.
    """
    result = RewrapResult()
    if not crypto.encryption_enabled():
        # A keyless box stores plaintext deliberately. Nothing to move it to.
        return result

    for table, model, attribute in _ENCRYPTED_COLUMNS:
        moved = 0
        column = getattr(model, attribute)
        stale_ids = [
            row_id
            for row_id, stored in db.execute(select(model.id, column))
            if not crypto.is_current(stored or "")
        ]

        if not stale_ids:
            db.rollback()
            continue

        for row in db.scalars(select(model).where(model.id.in_(stale_ids))):
            stored = getattr(row, attribute) or ""
            try:
                setattr(row, attribute, crypto.rewrap(stored))
            except crypto.CredentialEncryptionError as exc:
                # Never the secret itself, and never the ciphertext: this line
                # goes to a log file that is not held to the same standard as
                # the column it describes.
                logger.warning(
                    "%s row %s could not be re-encrypted: %s", table, row.id, exc
                )
                result.unreadable += 1
                continue
            moved += 1

        if moved:
            db.commit()
            result.by_table[table] = moved
            result.rewrapped += moved
        else:
            # Nothing written, but the SELECT opened a transaction.
            db.rollback()

    if result.rewrapped:
        logger.info(
            "re-encrypted %d stored secret(s) under the current "
            "TOKEN_ENCRYPTION_KEY: %s",
            result.rewrapped,
            ", ".join(f"{table}={n}" for table, n in sorted(result.by_table.items())),
        )
    if result.unreadable:
        logger.warning(
            "%d stored secret(s) match no configured TOKEN_ENCRYPTION_KEY — put "
            "the previous key back on the end of the setting, or reconnect the "
            "affected platform/webhook/trigger",
            result.unreadable,
        )
    return result
