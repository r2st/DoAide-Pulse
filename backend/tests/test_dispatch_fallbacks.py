"""What happens to queued work when the broker is not there to take it.

Both dispatchers exist to make the same promise: nothing is dropped because
Celery is unreachable. The publish one falls back to running inline; the webhook
one does too, but has to resume from where the broker stopped accepting rather
than from the top of the batch, or the receiver gets the head of the batch
twice. That resume point is the part worth pinning.
"""
from __future__ import annotations

import pytest

from app.services import content_pipeline, webhooks
from app.services.webhooks import WebhookUrlError

# --------------------------------------------------------------------------- #
# Webhook delivery                                                            #
# --------------------------------------------------------------------------- #


class _Recorder:
    """Stands in for ``webhook_tasks``, recording which arm sent what."""

    def __init__(self, *, fail_delay_after: int | None = None, inline_raises=()):
        self.queued: list[int] = []
        self.inline: list[int] = []
        self._fail_delay_after = fail_delay_after
        self._inline_raises = set(inline_raises)
        outer = self

        class _DeliverOne:
            def delay(self, delivery_id: int) -> None:
                if (
                    outer._fail_delay_after is not None
                    and len(outer.queued) >= outer._fail_delay_after
                ):
                    raise RuntimeError("broker gone")
                outer.queued.append(delivery_id)

            def __call__(self, delivery_id: int) -> None:
                if delivery_id in outer._inline_raises:
                    raise RuntimeError("receiver refused")
                outer.inline.append(delivery_id)

        self.deliver_one = _DeliverOne()


@pytest.fixture
def recorder(monkeypatch):
    def _install(**kwargs):
        rec = _Recorder(**kwargs)
        import app.tasks.webhook_tasks as real

        monkeypatch.setattr(real, "deliver_one", rec.deliver_one)
        return rec

    return _install


def test_an_empty_batch_does_not_reach_for_the_task_module(monkeypatch):
    """Called on every publish, including the ones with no webhooks at all."""
    monkeypatch.setattr(webhooks.settings, "celery_enabled", True)
    # No stubbing: if this touched the broker at all, it would raise here.
    webhooks.dispatch([])


def test_every_delivery_goes_to_the_broker_when_it_is_up(recorder, monkeypatch):
    rec = recorder()
    monkeypatch.setattr(webhooks.settings, "celery_enabled", True)

    webhooks.dispatch([1, 2, 3])

    assert rec.queued == [1, 2, 3]
    assert rec.inline == []


def test_a_broker_that_dies_mid_batch_resumes_inline_and_does_not_repeat_itself(
    recorder, monkeypatch
):
    """The receiver cannot tell a repeat from a retry — same id, same signature."""
    rec = recorder(fail_delay_after=2)
    monkeypatch.setattr(webhooks.settings, "celery_enabled", True)

    webhooks.dispatch([10, 11, 12, 13])

    assert rec.queued == [10, 11]
    assert rec.inline == [12, 13]


def test_a_delivery_that_fails_inline_does_not_take_the_rest_of_the_batch_with_it(
    recorder, monkeypatch
):
    """The caller's thread has just published something. That must not unwind."""
    rec = recorder(inline_raises=(11,))
    monkeypatch.setattr(webhooks.settings, "celery_enabled", False)

    webhooks.dispatch([10, 11, 12])

    assert rec.queued == []
    assert rec.inline == [10, 12]


def test_the_delivery_client_never_follows_a_redirect():
    """A 3xx can move a validated public URL to somewhere inside the network."""
    with webhooks._http_client() as client:
        assert client.follow_redirects is False
        assert client.timeout.read == webhooks.settings.webhook_timeout_seconds


# --------------------------------------------------------------------------- #
# URL validation                                                              #
# --------------------------------------------------------------------------- #


def test_an_absurdly_long_webhook_url_is_refused_before_anything_resolves_it():
    long_url = "https://example.com/" + "a" * 700

    with pytest.raises(WebhookUrlError, match="700 characters maximum"):
        webhooks.validate_url(long_url)


# --------------------------------------------------------------------------- #
# Publishing                                                                  #
# --------------------------------------------------------------------------- #


def test_publish_goes_to_the_broker_when_celery_is_enabled(monkeypatch):
    import app.tasks.publish_tasks as publish_tasks

    queued: list[int] = []
    inline: list[int] = []

    class _PublishOne:
        def delay(self, publication_id: int) -> None:
            queued.append(publication_id)

        def __call__(self, publication_id: int) -> None:
            inline.append(publication_id)

    monkeypatch.setattr(publish_tasks, "publish_one", _PublishOne())
    monkeypatch.setattr(content_pipeline.settings, "celery_enabled", True)

    content_pipeline.publish_now(7)

    assert queued == [7]
    assert inline == []
