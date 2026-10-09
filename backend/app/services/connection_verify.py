"""Periodic re-verification of stored platform connections.

A credential that was revoked or expired on the platform side sits at
CONNECTED in Pulse until something tries to use it. On a quiet project
that can be weeks — and the first sign is a failed publication at 3am.

This module is the early-warning half. It decrypts each connection's
stored credentials, calls the same ``adapter.verify`` the settings page
uses, and stamps the result. The maintenance task in
:mod:`app.tasks.maintenance_tasks` runs it on a schedule.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mixins import utcnow
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.services import publishers
from app.services.crypto import CredentialEncryptionError, decrypt_credentials
from app.services.errors import clip_error, redact
from app.services.publishers.base import (
    CredentialError,
    NotImplementedAdapter,
    PublishError,
)

logger = logging.getLogger(__name__)


def verify_all(db: Session) -> dict:
    """Re-verify every connection and return a summary.

    Each connection is handled independently: one dead token must not stop
    the rest from being checked.
    """
    connections = list(
        db.scalars(
            select(PlatformConnection).where(
                PlatformConnection.status == ConnectionStatus.CONNECTED,
            )
        )
    )

    valid = 0
    invalid = 0
    unreachable = 0

    for connection in connections:
        adapter = publishers.get_adapter(connection.platform)
        credentials: dict = {}
        previous_status = connection.status

        try:
            credentials = decrypt_credentials(connection.encrypted_credentials)
            connection.display_name = adapter.verify(credentials)
            connection.status = ConnectionStatus.CONNECTED
            connection.last_verified_at = utcnow()
            connection.last_error = None
            valid += 1
        except (CredentialError, CredentialEncryptionError) as exc:
            connection.status = ConnectionStatus.INVALID
            connection.last_verified_at = utcnow()
            connection.last_error = clip_error(
                redact(str(exc), publishers.secret_values(adapter, credentials))
            )
            invalid += 1
        except (PublishError, NotImplementedAdapter) as exc:
            connection.last_error = clip_error(
                redact(str(exc), publishers.secret_values(adapter, credentials))
            )
            unreachable += 1
        except Exception:
            logger.exception(
                "unexpected error verifying connection %d (%s for user %d)",
                connection.id,
                connection.platform.value,
                connection.user_id,
            )
            unreachable += 1
            continue

        if connection.status != previous_status:
            logger.warning(
                "connection %d (%s, user %d) status changed: %s -> %s",
                connection.id,
                connection.platform.value,
                connection.user_id,
                previous_status.value,
                connection.status.value,
            )

    db.commit()
    return {
        "checked": len(connections),
        "valid": valid,
        "invalid": invalid,
        "unreachable": unreachable,
    }
