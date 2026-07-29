"""Provider health tracking.

R3 requires failover that is **health-based, not round-robin**. The difference
matters: round-robin sends every other request into a provider that is known to
be rate-limiting, so half of all traffic fails while the system reports itself
as working.

This is a circuit breaker. A provider that fails repeatedly is taken out of
rotation, retried after a cooling-off period, and restored only after it
actually succeeds.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from mitta.llm.models import HealthState

# Consecutive failures before a provider is taken out of rotation.
DEFAULT_FAILURE_THRESHOLD = 3

# How long to wait before letting one request through to test the water.
DEFAULT_COOLDOWN_SECONDS = 30.0

# Cap on the cooldown as repeated probes keep failing.
DEFAULT_MAX_COOLDOWN_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class HealthPolicy:
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    max_cooldown_seconds: float = DEFAULT_MAX_COOLDOWN_SECONDS


@dataclass
class ProviderHealth:
    """Health of one provider. Mutable; guarded by the tracker's lock."""

    name: str
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    opened_at: float | None = None
    cooldown: float = DEFAULT_COOLDOWN_SECONDS
    last_error: str | None = None
    last_success_at: float | None = None

    @property
    def state(self) -> HealthState:
        if self.opened_at is not None:
            return "unavailable"
        return "degraded" if self.consecutive_failures > 0 else "healthy"


class HealthTracker:
    """Thread-safe health for every provider.

    Locked because the gateway may serve a chat turn, a background
    summarisation and a personality rewrite concurrently, and all three update
    the same counters.
    """

    def __init__(self, policy: HealthPolicy | None = None) -> None:
        self._policy = policy or HealthPolicy()
        self._lock = threading.Lock()
        self._providers: dict[str, ProviderHealth] = {}

    def _entry(self, name: str) -> ProviderHealth:
        entry = self._providers.get(name)
        if entry is None:
            entry = ProviderHealth(name=name, cooldown=self._policy.cooldown_seconds)
            self._providers[name] = entry
        return entry

    def is_available(self, name: str, *, now: float | None = None) -> bool:
        """Whether to send a request to this provider.

        A provider in cooldown becomes available again when the window expires —
        the next request is a probe. Success closes the breaker; failure reopens
        it with a longer window.
        """
        moment = now if now is not None else time.monotonic()
        with self._lock:
            entry = self._entry(name)
            if entry.opened_at is None:
                return True
            # Cooldown elapsed: let one request through as a probe. The
            # breaker stays open until that request reports back, so a burst
            # does not all probe at once.
            return moment - entry.opened_at >= entry.cooldown

    def record_success(self, name: str, *, now: float | None = None) -> None:
        moment = now if now is not None else time.monotonic()
        with self._lock:
            entry = self._entry(name)
            entry.consecutive_failures = 0
            entry.total_successes += 1
            entry.last_success_at = moment
            entry.last_error = None
            # Full reset, not a decrement. A provider that just answered is
            # working, and making it serve three more requests to earn its way
            # back would prolong a fault that has already cleared.
            entry.opened_at = None
            entry.cooldown = self._policy.cooldown_seconds

    def record_failure(self, name: str, error: str, *, now: float | None = None) -> None:
        moment = now if now is not None else time.monotonic()
        with self._lock:
            entry = self._entry(name)
            entry.consecutive_failures += 1
            entry.total_failures += 1
            entry.last_error = error

            if entry.consecutive_failures >= self._policy.failure_threshold:
                if entry.opened_at is not None:
                    # A failed probe: back off further rather than retrying at
                    # the same rate against a provider that is still down.
                    entry.cooldown = min(entry.cooldown * 2, self._policy.max_cooldown_seconds)
                entry.opened_at = moment

    def state(self, name: str) -> HealthState:
        with self._lock:
            return self._entry(name).state

    def snapshot(self) -> dict[str, ProviderHealth]:
        """A copy, for status reporting.

        Copied rather than handed out live: a caller iterating the real dict
        while a request completes would see it mutate underneath them.
        """
        with self._lock:
            return {
                name: ProviderHealth(
                    name=entry.name,
                    consecutive_failures=entry.consecutive_failures,
                    total_failures=entry.total_failures,
                    total_successes=entry.total_successes,
                    opened_at=entry.opened_at,
                    cooldown=entry.cooldown,
                    last_error=entry.last_error,
                    last_success_at=entry.last_success_at,
                )
                for name, entry in self._providers.items()
            }

    def reset(self, name: str | None = None) -> None:
        with self._lock:
            if name is None:
                self._providers.clear()
            else:
                self._providers.pop(name, None)
