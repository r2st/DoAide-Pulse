"""Publishing adapters and the registry that resolves them.

One adapter per platform, all behind :class:`~app.services.publishers.base.Adapter`.
The registry is the single place that answers "can Pulse actually publish
here?" — the ``Platform`` enum is only the vocabulary.

Adding a platform is: write the adapter, add it to :data:`_ADAPTERS`, add the
enum member. Nothing else in the codebase needs to know it exists.
"""
from __future__ import annotations

from app.models.publication import Platform
from app.services.publishers.base import (
    Adapter,
    CredentialError,
    CredentialField,
    MetricsSnapshot,
    NotImplementedAdapter,
    PublishError,
    PublishRequest,
    PublishResult,
)
from app.services.publishers.bluesky import BlueskyAdapter
from app.services.publishers.buttondown import ButtondownAdapter
from app.services.publishers.devto import DevToAdapter
from app.services.publishers.git import GitAdapter
from app.services.publishers.hashnode import HashnodeAdapter
from app.services.publishers.linkedin import LinkedInAdapter
from app.services.publishers.mastodon import MastodonAdapter
from app.services.publishers.medium import MediumAdapter
from app.services.publishers.twitter import TwitterAdapter
from app.services.publishers.wordpress import WordPressAdapter

_ADAPTERS: dict[Platform, Adapter] = {
    Platform.DEVTO: DevToAdapter(),
    Platform.MEDIUM: MediumAdapter(),
    Platform.HASHNODE: HashnodeAdapter(),
    Platform.LINKEDIN: LinkedInAdapter(),
    Platform.TWITTER: TwitterAdapter(),
    Platform.WORDPRESS: WordPressAdapter(),
    Platform.MASTODON: MastodonAdapter(),
    Platform.BLUESKY: BlueskyAdapter(),
    Platform.GIT: GitAdapter(),
    Platform.BUTTONDOWN: ButtondownAdapter(),
}


class UnknownPlatform(KeyError):
    """No adapter is registered for that platform."""


def get_adapter(platform: Platform | str) -> Adapter:
    """The adapter for *platform*, whether it is finished or not.

    Callers that need a *working* adapter should check ``adapter.implemented``
    or let :meth:`~base.Adapter.publish` raise :class:`NotImplementedAdapter`.
    """
    try:
        key = platform if isinstance(platform, Platform) else Platform(platform)
    except ValueError as exc:
        raise UnknownPlatform(str(platform)) from exc
    try:
        return _ADAPTERS[key]
    except KeyError as exc:  # pragma: no cover - every enum member is registered
        raise UnknownPlatform(str(platform)) from exc


def all_adapters() -> list[Adapter]:
    """Every adapter, in the order the settings page should list them:
    the ones that work first."""
    return sorted(
        _ADAPTERS.values(), key=lambda a: (not a.implemented, a.display_name.lower())
    )


def implemented_platforms() -> list[Platform]:
    """Platforms that can actually publish right now."""
    return [a.platform for a in all_adapters() if a.implemented]


def secret_values(adapter: Adapter, credentials: dict) -> list[str]:
    """The values *credentials* holds for the fields *adapter* calls secret.

    The input to :func:`app.services.errors.redact`, which strips them out of a
    failure message before it is written to a row. Lives here because
    ``credential_fields`` is the only declaration of which values are secrets,
    and both callers — the publish path and the settings page's re-verify —
    would otherwise re-derive it and drift.

    Non-secret fields are deliberately kept: a WordPress site URL, a Bluesky
    handle and a GitHub repo name are what tell a reader *which* connection
    failed, and removing them would leave a message about nothing.
    """
    return [
        str(credentials[field.key])
        for field in adapter.credential_fields
        if field.secret and credentials.get(field.key)
    ]


def capabilities() -> list[dict]:
    """A JSON-serializable description of every platform, for the UI.

    Credential *fields* are exposed (the settings form needs them); credential
    *values* never are.
    """
    return [
        {
            "platform": adapter.platform.value,
            "display_name": adapter.display_name,
            "implemented": adapter.implemented,
            "supports_metrics": adapter.supports_metrics,
            # What the headline-testing UI needs to explain itself: a piece
            # published only to platforms that answer False here can never
            # produce a headline verdict, however much engagement it earns.
            "supports_title_update": adapter.supports_title_update,
            "caveat": adapter.caveat,
            "credential_fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "help_text": f.help_text,
                    "secret": f.secret,
                    "required": f.required,
                }
                for f in adapter.credential_fields
            ],
        }
        for adapter in all_adapters()
    ]


__all__ = [
    "Adapter",
    "CredentialError",
    "CredentialField",
    "MetricsSnapshot",
    "NotImplementedAdapter",
    "PublishError",
    "PublishRequest",
    "PublishResult",
    "UnknownPlatform",
    "all_adapters",
    "capabilities",
    "get_adapter",
    "implemented_platforms",
]
