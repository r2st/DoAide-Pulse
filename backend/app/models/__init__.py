"""SQLAlchemy models.

Importing this package registers every model on the shared ``Base.metadata`` so
that ``Base.metadata.create_all`` and Alembic autogenerate see them all.
"""
from app.models.content import (
    TARGET_WORDS,
    Content,
    ContentIdea,
    ContentStatus,
    ContentType,
)
from app.models.metrics import ContentMetric
from app.models.password_reset import PasswordResetToken
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.project import AutopilotMode, Project, Tone, slugify
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.user import User
from app.models.webhook import (
    SUBSCRIBABLE_EVENTS,
    DeliveryStatus,
    Webhook,
    WebhookDelivery,
    WebhookEvent,
)

__all__ = [
    "SUBSCRIBABLE_EVENTS",
    "TARGET_WORDS",
    "AutopilotMode",
    "ConnectionStatus",
    "Content",
    "ContentIdea",
    "ContentMetric",
    "ContentStatus",
    "ContentType",
    "DeliveryStatus",
    "PasswordResetToken",
    "Platform",
    "PlatformConnection",
    "Project",
    "Publication",
    "PublicationStatus",
    "Tone",
    "User",
    "Webhook",
    "WebhookDelivery",
    "WebhookEvent",
    "slugify",
]
