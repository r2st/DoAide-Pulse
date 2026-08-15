"""Celery application + beat schedule."""
from __future__ import annotations

from typing import TYPE_CHECKING

from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging

from app.config import settings
from app.logging_config import configure_logging

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any, ParamSpec, Protocol, TypeVar

    from celery.result import AsyncResult

    _P = ParamSpec("_P")
    _R_co = TypeVar("_R_co", covariant=True)

    class _Task(Protocol[_P, _R_co]):
        """A ``@task``-decorated function, as this tree actually uses one."""

        name: str

        # Run it here and now, on the calling thread. This is the inline
        # fallback every dispatch site has when the broker is unreachable.
        def __call__(self, *args: _P.args, **kwargs: _P.kwargs) -> _R_co: ...

        # Hand it to the broker.
        def delay(self, *args: _P.args, **kwargs: _P.kwargs) -> AsyncResult: ...

        # The same, with delivery options.
        def apply_async(
            self,
            args: tuple[object, ...] | None = ...,
            kwargs: dict[str, object] | None = ...,
            **options: Any,
        ) -> AsyncResult: ...

    class _TaskDecorator(Protocol):
        def __call__(
            self, **opts: Any
        ) -> Callable[[Callable[_P, _R_co]], _Task[_P, _R_co]]:
            ...


@setup_logging.connect
def _configure_logging(**_kwargs: object) -> None:
    """Give the worker and beat the same log output as the API.

    Connecting to ``setup_logging`` at all is what stops Celery configuring
    logging itself: the signal is documented as an override, and a receiver on
    it disables the whole of Celery's setup, root-logger hijack included. So
    the worker's own lines and the application's now go through one handler with
    one format, and ``LOG_LEVEL`` means the same thing in all three units.

    Without it, ``--loglevel=info`` in the systemd units set the *root* logger
    to INFO as a side effect, which is why worker logs looked complete while the
    API's were not — the same code, logging to the same names, kept or dropped
    depending on which process it happened to run in.
    """
    configure_logging()

celery_app = Celery(
    "herald",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.publish_tasks",
        "app.tasks.autopilot_tasks",
        "app.tasks.metrics_tasks",
        "app.tasks.maintenance_tasks",
        "app.tasks.headline_tasks",
        "app.tasks.digest_tasks",
        "app.tasks.webhook_tasks",
        "app.tasks.trigger_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Nothing ever reads a task result — every task records its outcome in
    # Postgres. Ignoring results keeps apply_async from touching the result
    # store, which otherwise blocks the caller for ~20s reconnecting whenever
    # Redis is unavailable.
    task_ignore_result=True,
    # Fail fast when the broker is unreachable instead of blocking the caller.
    # The API dispatches publishes with .delay() from inside a request and falls
    # back to running them inline (see routers/content._dispatch) — that
    # fallback only works if the publish raises promptly rather than retrying
    # for minutes. Workers are unaffected; they reconnect via broker_pool_limit.
    broker_connection_retry_on_startup=False,
    # NB: 0 (and None) mean "retry forever" in Celery — 1 is the fail-fast value.
    broker_connection_max_retries=1,
    broker_transport_options={"socket_connect_timeout": 2, "socket_timeout": 2},
    task_publish_retry=False,
)

# ``celery_app.task``, with a signature. Celery ships no ``py.typed``, so a type
# checker reading its source infers the decorator as returning the undecorated
# function — which typechecks the inline calls fine but loses ``.delay``, the
# whole point of the decorator, at every dispatch site. The annotation is
# never evaluated (``from __future__ import annotations``); at runtime this is
# the identical object, so task registration is unchanged.
task: _TaskDecorator = celery_app.task

# Periodic jobs.
#
# Cadences differ by how fast the underlying thing moves: a scheduled post is
# due to the minute, repos ship a few times a day at most, and view counts move
# slowly enough that polling them more than four times a day is just API quota
# spent on noise.
celery_app.conf.beat_schedule = {
    "publish-due-content": {
        "task": "app.tasks.publish_tasks.publish_due",
        "schedule": float(settings.publish_scan_interval_seconds),
    },
    "release-approved-content": {
        # The backstop for an approved piece nothing ever queued. Rare by
        # design, so it runs an order of magnitude less often than the publish
        # sweep it feeds.
        "task": "app.tasks.publish_tasks.release_approved_content",
        "schedule": float(settings.publish_scan_interval_seconds) * 10,
    },
    "recover-unblocked-publications": {
        # Re-arms publications that failed only because a platform was not
        # connected, once it is. The cure happens in Settings, on nobody's
        # schedule, so this is a poll — on the same cadence as the backstop
        # above, because a piece that has been waiting for a connection is not
        # made worse by waiting one more sweep.
        "task": "app.tasks.publish_tasks.recover_unblocked_publications",
        "schedule": float(settings.publish_scan_interval_seconds) * 10,
    },
    "scan-project-repos": {
        "task": "app.tasks.autopilot_tasks.scan_all_projects",
        "schedule": float(settings.autopilot_scan_interval_seconds),
    },
    "collect-metrics": {
        "task": "app.tasks.metrics_tasks.collect_all_metrics",
        "schedule": float(settings.metrics_scan_interval_seconds),
    },
    "auto-select-headlines": {
        "task": "app.tasks.headline_tasks.auto_select_headlines",
        "schedule": float(settings.headline_auto_select_interval_seconds),
    },
    "weekly-digest": {
        "task": "app.tasks.digest_tasks.send_weekly_digests",
        # A calendar schedule, not an interval: "every 604800 seconds" drifts
        # against the week, and a summary that lands at 03:12 on a Thursday is
        # one nobody opens.
        "schedule": crontab(
            day_of_week=str(settings.digest_send_weekday),
            hour=str(settings.digest_send_hour),
            minute="0",
        ),
    },
    "deliver-webhooks": {
        # Only picks up deliveries whose backoff has elapsed — the first attempt
        # is dispatched by whatever emitted the event, not by this sweep.
        "task": "app.tasks.webhook_tasks.deliver_due",
        "schedule": float(settings.webhook_scan_interval_seconds),
    },
    "check-due-triggers": {
        # RSS, GitHub and schedule triggers. Inbound webhooks are not swept —
        # they fire on the request thread that carries them.
        "task": "app.tasks.trigger_tasks.check_due_triggers",
        "schedule": float(settings.trigger_scan_interval_seconds),
    },
    "purge-expired-tokens": {
        "task": "app.tasks.maintenance_tasks.purge_expired_tokens",
        "schedule": 86400.0,  # once a day
    },
    "purge-old-webhook-deliveries": {
        "task": "app.tasks.maintenance_tasks.purge_old_webhook_deliveries",
        "schedule": 86400.0,  # once a day
    },
    "purge-old-trigger-events": {
        "task": "app.tasks.maintenance_tasks.purge_old_trigger_events",
        "schedule": 86400.0,  # once a day
    },
    "purge-old-preview-links": {
        "task": "app.tasks.maintenance_tasks.purge_old_preview_links",
        "schedule": 86400.0,  # once a day
    },
    "rewrap-credentials": {
        # Re-encrypts stored secrets under the head of TOKEN_ENCRYPTION_KEY.
        # Daily because it does nothing at all until the key is rotated, and
        # after a rotation the operator runs it by hand rather than waiting.
        "task": "app.tasks.maintenance_tasks.rewrap_credentials",
        "schedule": 86400.0,
    },
}

# Imported for its side effect: the module is nothing but signal receivers, and
# a receiver that is never imported is never connected. It goes at the bottom
# because it imports back into ``app.services`` for the failure classification,
# and this module is imported by every dispatch site in the tree — including
# request handlers, which have no reason to pull in the publishing adapters
# before ``celery_app`` has finished defining the decorator they came for.
#
# Present in all three processes on purpose. The worker and beat need the timing
# and failure lines; the API needs ``before_task_publish``, which is the half
# that puts a request's id into the message before it crosses the broker.
from app.tasks import observability  # noqa: E402,F401
