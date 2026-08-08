"""Per-target circuit breaker for outbound connector / action calls.

A failing downstream (a connector that is timing out or 5xx-ing) must not drag the
whole decisioning path down with it: without a breaker, every decision that touches
that connector pays the full timeout + retry budget, threads pile up, and latency
blows out tenant-wide. The breaker makes the failure *fast and bounded*:

    CLOSED    — calls flow; consecutive failures are counted.
    OPEN      — after `failure_threshold` consecutive failures the breaker trips;
                calls short-circuit immediately (CircuitOpenError) for `recovery_seconds`.
    HALF_OPEN — after the cooldown a limited number of *probe* calls are allowed
                through; a success closes the breaker (full recovery), any failure
                re-opens it for another cooldown.

State is per-process (per replica) and keyed by target (connector id, else URL host).
A breaker is a local latency/health signal, not shared truth — keeping it in-process
means no extra network hop on the hot path and no cross-tenant coupling. Config is
env-tunable so limits move without a code change.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, Optional


class CircuitOpenError(Exception):
    """Raised by allow() when the breaker is OPEN — the call was short-circuited, not attempted."""

    def __init__(self, key: str, retry_after: float) -> None:
        super().__init__("circuit open for {0} (retry in {1:.1f}s)".format(key, retry_after))
        self.key = key
        self.retry_after = retry_after


CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


class CircuitBreaker:
    """One target's breaker. Thread-safe: state transitions are guarded by a lock and the
    critical sections are tiny (counter + timestamp), so contention is negligible even under
    the concurrent per-line-item loop path."""

    def __init__(
        self,
        key: str,
        failure_threshold: Optional[int] = None,
        recovery_seconds: Optional[float] = None,
        half_open_max_calls: Optional[int] = None,
    ) -> None:
        self.key = key
        self.failure_threshold = failure_threshold or _env_int("CB_FAILURE_THRESHOLD", 5)
        self.recovery_seconds = recovery_seconds or _env_float("CB_RECOVERY_SECONDS", 30.0)
        self.half_open_max_calls = half_open_max_calls or _env_int("CB_HALF_OPEN_MAX", 1)
        self._lock = threading.Lock()
        self._state = CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_calls = 0
        # Observability counters (monotonic; cheap to expose in /health or metrics).
        self.trips = 0
        self.short_circuits = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._observed_state(time.monotonic())

    def _observed_state(self, now: float) -> str:
        # OPEN auto-advances to HALF_OPEN once the cooldown elapses (lazy, no timer thread).
        if self._state == OPEN and (now - self._opened_at) >= self.recovery_seconds:
            self._state = HALF_OPEN
            self._half_open_calls = 0
        return self._state

    def allow(self) -> None:
        """Gate a call. Returns normally if the call may proceed; raises CircuitOpenError if the
        breaker is OPEN (short-circuit). In HALF_OPEN, permits up to half_open_max_calls probes."""
        now = time.monotonic()
        with self._lock:
            state = self._observed_state(now)
            if state == OPEN:
                self.short_circuits += 1
                raise CircuitOpenError(self.key, self.recovery_seconds - (now - self._opened_at))
            if state == HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    # Probe budget spent; keep short-circuiting until a probe resolves.
                    self.short_circuits += 1
                    raise CircuitOpenError(self.key, self.recovery_seconds)
                self._half_open_calls += 1
            # CLOSED -> proceed.

    def record_success(self) -> None:
        with self._lock:
            # Any success (including a HALF_OPEN probe) fully heals the breaker.
            self._state = CLOSED
            self._consecutive_failures = 0
            self._half_open_calls = 0

    def record_failure(self) -> None:
        now = time.monotonic()
        with self._lock:
            state = self._observed_state(now)
            if state == HALF_OPEN:
                # A failed probe re-opens immediately for another cooldown.
                self._state = OPEN
                self._opened_at = now
                self.trips += 1
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._state = OPEN
                self._opened_at = now
                self.trips += 1

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "key": self.key,
                "state": self._observed_state(time.monotonic()),
                "consecutive_failures": self._consecutive_failures,
                "trips": self.trips,
                "short_circuits": self.short_circuits,
            }


_registry: Dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(key: str) -> CircuitBreaker:
    """Return the per-process breaker for `key` (connector id or URL host), creating it once."""
    with _registry_lock:
        breaker = _registry.get(key)
        if breaker is None:
            breaker = CircuitBreaker(key)
            _registry[key] = breaker
        return breaker


def enabled() -> bool:
    """Breakers are on by default; CIRCUIT_BREAKER=0 disables (e.g. for a deterministic test)."""
    return os.getenv("CIRCUIT_BREAKER", "1") != "0"


def all_snapshots() -> Dict[str, Dict[str, object]]:
    with _registry_lock:
        return {k: b.snapshot() for k, b in _registry.items()}


def reset_all() -> None:
    """Test hook: drop all breaker state so cases don't bleed into each other."""
    with _registry_lock:
        _registry.clear()
