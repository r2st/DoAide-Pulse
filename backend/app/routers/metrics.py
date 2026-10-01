"""``/api/v1/metrics`` — the operational read surface.

Five docstrings in the tree named this endpoint before it existed —
:mod:`app.services.llm_usage`, :mod:`app.services.llm_router`,
:mod:`app.models.llm_usage`, :mod:`app.models.project` and the ``s3m5i7k9l1h3``
migration all describe data as being "for ``/api/v1/metrics``". The write paths
were all live; only the reader was missing. This is it.

Distinct from ``/api/v1/analytics/*``, which is about how the *posts* did, for
the person who wrote them. This is about how *Pulse* is doing, for the person
running it: what the quota went on, whether the autopilot is scanning, whether
publishing is landing, which upstreams are standing down.

Distinct from ``/health/detail``, which answers "is it up, and if not why" for
a probe and reports the same breaker snapshot. This answers "what has it been
doing", which needs the tables rather than the process.

Authenticated, like every other read here. The prior design for this endpoint
had it open — see :mod:`app.services.llm_usage` — which would have made token
spend and per-provider failure rates public.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.errors import AUTHENTICATED, errors
from app.schemas.metrics import MetricsOut
from app.services import ops_metrics

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get(
    "",
    response_model=MetricsOut,
    summary="How the install is running",
    responses=errors(*AUTHENTICATED),
)
def metrics(
    hours: int = Query(
        default=24,
        ge=1,
        le=720,
        description=(
            "The window for the LLM section, in hours. Capped at 30 days "
            "because that is the default retention on the usage table — ask "
            "for more and the answer is bounded by the purge, not by the "
            "question. The other sections are all-time and ignore this."
        ),
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Content counts, scan frequency, publish rates, token spend, breakers.

    The account-scoped sections — ``content``, ``projects``, ``publishing`` —
    are filtered to the caller like every other read. The ``llm`` and
    ``breakers`` sections describe the deployment: the usage table has no owner
    column and a circuit breaker is process state, neither of which can be
    attributed to an account. :mod:`app.services.ops_metrics` says why, and why
    the breaker block is one process's view rather than the install's.
    """
    return ops_metrics.build(db, user.id, hours=hours)
