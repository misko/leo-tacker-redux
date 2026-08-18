"""Non-blocking bounded admission policy for optional full-dwell work."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Lock

from leo_flow.contracts.core import Digest, RecordingId


@dataclass(frozen=True)
class FullDwellQueuePolicyV0_1:
    maximum_pending: int = 8
    maximum_workers: int = 1
    estimated_worker_seconds_per_rx: float = 505.0

    def __post_init__(self) -> None:
        if not 1 <= self.maximum_pending <= 256:
            raise ValueError("full-dwell pending bound must lie in [1,256]")
        if not 1 <= self.maximum_workers <= 16:
            raise ValueError("full-dwell worker bound must lie in [1,16]")
        if self.estimated_worker_seconds_per_rx <= 0:
            raise ValueError("full-dwell worker estimate must be positive")


@dataclass(frozen=True)
class FullDwellWorkItemV0_1:
    recording_id: RecordingId
    request_digest: Digest


@dataclass(frozen=True)
class FullDwellAdmissionV0_1:
    accepted: bool
    backlog_depth: int
    truncated: bool
    reason: str | None


class BoundedFullDwellWorkQueueV0_1:
    """A non-blocking semantic adapter; production may replace it through a port."""

    def __init__(self, policy: FullDwellQueuePolicyV0_1) -> None:
        self.policy = policy
        self._pending: deque[FullDwellWorkItemV0_1] = deque()
        self._identities: set[tuple[RecordingId, Digest]] = set()
        self._lock = Lock()

    def try_submit(self, item: FullDwellWorkItemV0_1) -> FullDwellAdmissionV0_1:
        """Return immediately; capture callers never wait for analysis capacity."""
        identity = (item.recording_id, item.request_digest)
        with self._lock:
            if identity in self._identities:
                return FullDwellAdmissionV0_1(
                    True, len(self._pending), False, "exact-replay"
                )
            if len(self._pending) >= self.policy.maximum_pending:
                return FullDwellAdmissionV0_1(
                    False, len(self._pending), True, "bounded-queue-saturated"
                )
            self._pending.append(item)
            self._identities.add(identity)
            return FullDwellAdmissionV0_1(True, len(self._pending), False, None)

    def try_claim(self) -> FullDwellWorkItemV0_1 | None:
        with self._lock:
            return None if not self._pending else self._pending.popleft()

    @property
    def backlog_depth(self) -> int:
        with self._lock:
            return len(self._pending)
