"""SQLAlchemy models.

Importing this package registers every model on the shared ``Base.metadata`` so
that ``Base.metadata.create_all`` and Alembic autogenerate see them all.
"""
from app.models.api_key import ALL_SCOPES, ApiKey, ApiKeyScope
from app.models.content import (
    TARGET_WORDS,
    Content,
    ContentIdea,
    ContentStatus,
    ContentType,
)
from app.models.llm_usage import LLMUsage
from app.models.metrics import ContentMetric
from app.models.password_reset import PasswordResetToken
from app.models.platform_connection import ConnectionStatus, PlatformConnection
from app.models.preview_link import PreviewLink
from app.models.project import AutopilotMode, Project, Tone, slugify
from app.models.publication import Platform, Publication, PublicationStatus
from app.models.revision import ContentRevision, RevisionSource
from app.models.subscriber import Subscriber
from app.models.template import ContentTemplate, TemplateMode
from app.models.translation import ContentTranslation, TranslationStatus
from app.models.trigger import Trigger, TriggerEvent, TriggerEventStatus, TriggerKind
from app.models.user import User
from app.models.webhook import (
    SUBSCRIBABLE_EVENTS,
    DeliveryStatus,
    Webhook,
    WebhookDelivery,
    WebhookEvent,
)

__all__ = [
    "ALL_SCOPES",
    "SUBSCRIBABLE_EVENTS",
    "TARGET_WORDS",
    "ApiKey",
    "ApiKeyScope",
    "AutopilotMode",
    "ConnectionStatus",
    "Content",
    "ContentIdea",
    "ContentMetric",
    "ContentRevision",
    "ContentStatus",
    "ContentTemplate",
    "ContentTranslation",
    "ContentType",
    "DeliveryStatus",
    "LLMUsage",
    "PasswordResetToken",
    "Platform",
    "PlatformConnection",
    "PreviewLink",
    "Project",
    "Publication",
    "PublicationStatus",
    "RevisionSource",
    "Subscriber",
    "TemplateMode",
    "Tone",
    "TranslationStatus",
    "Trigger",
    "TriggerEvent",
    "TriggerEventStatus",
    "TriggerKind",
    "User",
    "Webhook",
    "WebhookDelivery",
    "WebhookEvent",
    "slugify",
]
