"""Health check and platform capability discovery."""
from __future__ import annotations

from fastapi import APIRouter

from app.config import settings
from app.schemas.settings import HealthOut
from app.services import llm_router, publishers
from app.services.crypto import encryption_enabled

router = APIRouter(tags=["misc"])


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """Liveness, plus enough state to answer "why isn't X working?".

    The breaker state is per-process, so this reports the API process's view —
    workers keep their own. Enough to answer "is AI degraded right now?".
    """
    return HealthOut(
        status="ok",
        llm_providers=llm_router.configured_providers(),
        llm_breakers_open=llm_router.breaker.snapshot(),
        github_configured=bool(settings.github_token),
        credential_encryption=encryption_enabled(),
        implemented_platforms=[p.value for p in publishers.implemented_platforms()],
    )
