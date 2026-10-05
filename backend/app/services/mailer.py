"""Outbound email over SMTP.

Pulse sends exactly one kind of message — the password reset link — so this is
deliberately the smallest thing that can do that: no templating, no queue, no
provider SDK. ``smtplib`` against whatever host the operator points it at.

**When SMTP is not configured the message is written to the log instead**, at
WARNING, link and all. That is not a stub: Pulse is single-user and self-hosted,
and the alternative for an operator who never set up a mail server is a reset
flow that cannot complete at all. ``journalctl -u herald-api`` is a legitimate
way to collect your own reset link. It does mean the link passes through the
logs, which is why :func:`configured` is surfaced on the health endpoint and why
the log line says so out loud.

Sending is best-effort by design: :func:`send` returns False rather than raising,
because a reset request must answer the same way whether or not the mail went
out (see :mod:`app.services.password_reset`).
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger(__name__)


def configured() -> bool:
    """True when there is an SMTP host to talk to."""
    return bool(settings.smtp_host.strip())


def _from_address() -> str:
    return settings.smtp_from.strip() or f"herald@{settings.smtp_host.strip()}"


def send(*, to: str, subject: str, body: str, html: str | None = None) -> bool:
    """Deliver a message. Never raises; returns whether it went out.

    *body* is the plain-text version and is always required — an HTML-only
    email is unreadable to anything that cannot render it, and it is the part
    that ends up in the log when SMTP is not configured. Passing *html* adds an
    alternative part; the client picks.
    """
    if not configured():
        logger.warning(
            "SMTP is not configured (SMTP_HOST is blank) — not sending %r. "
            "Check SMTP_HOST / SMTP_PORT settings.",
            subject,
        )
        return False

    message = EmailMessage()
    message["From"] = _from_address()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    if html:
        # Added second, so it becomes the preferred alternative while the text
        # part stays the fallback.
        message.add_alternative(html, subtype="html")

    timeout = settings.smtp_timeout_seconds
    try:
        if settings.smtp_use_ssl:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=timeout
            )
        else:
            client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
        with client:
            if settings.smtp_starttls and not settings.smtp_use_ssl:
                client.starttls()
            if settings.smtp_user:
                client.login(settings.smtp_user, settings.smtp_password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        # Includes auth failures, DNS, TLS and timeouts. The caller cannot act
        # on any of them and must not leak which one happened to the requester.
        logger.error("SMTP send to=%s subject=%r failed: %s", to, subject, exc)
        return False

    logger.info("sent %r to %s", subject, to)
    return True


__all__ = ["configured", "send"]
