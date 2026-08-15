"""A consecutive-failure breaker with a cool-down, shared by the two things that
call somebody else's server on Herald's behalf.

It was written for :mod:`app.services.llm_router` and lived there, which was
right while there was one caller. There are two now — the publishing adapters
need the same protection for the same reason — and the alternative to moving it
was a second copy, which is how two breakers end up disagreeing about what
"open" means.

Nothing about the mechanism is LLM-specific: it counts failures against a key,
opens for a cool-down when they run consecutive, and honours an upstream that
names its own window. The *keys* differ, and that is the whole of the difference
between the two callers — see :mod:`app.services.publishers.breaker` for why the
publishing side keys per account rather than per platform.

**Per-process and in-memory.** With several workers each keeps its own view, so
a platform that is down is discovered once per process rather than once. That is
the same trade the LLM breaker has always made and the reasoning is unchanged: a
shared breaker in Redis would be a new hard dependency on the path of every
outbound call, and being wrong in the direction of "try again" is the safe way
to be wrong.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _BreakerState:
    failures: int = 0
    open_until: float = 0.0


class CircuitBreaker:
    """Per-key consecutive-failure counter with a cool-down.

    Deliberately trips on *consecutive* failures: an upstream that fails one
    request in ten is degraded, not down, and tripping on a cumulative count
    would eventually take it out of rotation permanently.
    """

    def __init__(self, threshold: int, cooldown_seconds: float) -> None:
        self.threshold = threshold
        self.cooldown = cooldown_seconds
        self._state: dict[str, _BreakerState] = {}
        self._lock = threading.Lock()

    def is_open(self, name: str, *, now: float | None = None) -> bool:
        """True when *name* should be skipped right now."""
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.get(name)
            return state is not None and state.open_until > now

    def seconds_remaining(self, name: str, *, now: float | None = None) -> float:
        """How long *name* stays shut, in seconds. ``0.0`` when it is not open.

        The publishing side parks a row for exactly this long rather than for a
        backoff of its own invention: a breaker that says "not for another four
        minutes" and a row that comes back in thirty seconds are two answers to
        one question, and the row's is the one that gets acted on.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.get(name)
            if state is None:
                return 0.0
            return max(0.0, state.open_until - now)

    def record_failure(self, name: str, *, now: float | None = None) -> bool:
        """Count a failure; return True if this one tripped the breaker."""
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.setdefault(name, _BreakerState())
            state.failures += 1
            if state.failures >= self.threshold:
                state.open_until = now + self.cooldown
                state.failures = 0
                return True
            return False

    def open_for(self, name: str, seconds: float, *, now: float | None = None) -> None:
        """Skip *name* for *seconds*, whatever its failure count.

        For the one case the consecutive-failure counter reads wrong: an
        upstream that answered 429 with a long ``Retry-After`` has told us its
        quota is spent, and there is nothing to learn from the two further
        failures the threshold would otherwise wait for. Never shortens a window
        already open — a later, vaguer refusal must not undo a definite one.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state.setdefault(name, _BreakerState())
            state.open_until = max(state.open_until, now + max(0.0, seconds))
            state.failures = 0

    def record_success(self, name: str) -> None:
        """Clear *name*'s failure count and any open circuit.

        Discards the state rather than decrementing it. An upstream that
        answered is working now, and a half-remembered run of failures from an
        outage an hour ago would trip the breaker early on the next unrelated
        blip.
        """
        with self._lock:
            self._state.pop(name, None)

    def reset(self) -> None:
        """Clear all state — used by tests and after a config change."""
        with self._lock:
            self._state.clear()

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Current state, for the health endpoint / debugging."""
        now = time.monotonic()
        with self._lock:
            return {
                name: {
                    "failures": state.failures,
                    "seconds_until_retry": max(0.0, state.open_until - now),
                }
                for name, state in self._state.items()
            }


__all__ = ["CircuitBreaker"]
