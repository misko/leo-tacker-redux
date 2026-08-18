"""Pull-based optional timeline worker, isolated from capture and primary analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import ArtifactRef
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    FullDwellRefinementDispatchPortV0_1,
    FullDwellRefinementRequestV0_1,
    FullDwellTimelinePlanV0_1,
    FullDwellTimelineStreamSelectionV0_1,
)
from leo_flow.contracts.storage import RecordingObjectRef


@dataclass(frozen=True)
class FullDwellTimelineLeaseV0_1:
    work_id: str
    lease_token: str
    attempt: int
    recording_ref: RecordingObjectRef
    plan: FullDwellTimelinePlanV0_1
    stream_selections: tuple[FullDwellTimelineStreamSelectionV0_1, ...]

    def __post_init__(self) -> None:
        if not self.work_id or not self.lease_token or self.attempt <= 0:
            raise ValueError("invalid timeline work lease")
        if not self.stream_selections:
            raise ValueError("timeline work lease requires streams")


@dataclass(frozen=True)
class ProducedFullDwellTimelineV0_1:
    timeline_ref: ArtifactRef
    refinement_request: FullDwellRefinementRequestV0_1


class FullDwellTimelineWorkRepositoryV0_1(Protocol):
    """Timeline workers pull durable work; acquisition never calls the producer."""

    def claim(self, worker_id: str) -> FullDwellTimelineLeaseV0_1 | None: ...

    def complete_timeline(
        self, lease: FullDwellTimelineLeaseV0_1, result: ArtifactRef
    ) -> None: ...

    def retry_timeline(
        self, lease: FullDwellTimelineLeaseV0_1, reason: str
    ) -> None: ...

    def record_refinement_dispatch_failure(
        self, lease: FullDwellTimelineLeaseV0_1, reason: str
    ) -> None: ...


class FullDwellTimelineLeaseProducerV0_1(Protocol):
    def produce(
        self, lease: FullDwellTimelineLeaseV0_1
    ) -> ProducedFullDwellTimelineV0_1: ...


class IndependentFullDwellTimelineServiceV0_1:
    """Publish the cheap product first; only then enqueue optional exact work."""

    def __init__(
        self,
        work: FullDwellTimelineWorkRepositoryV0_1,
        producer: FullDwellTimelineLeaseProducerV0_1,
        refinements: FullDwellRefinementDispatchPortV0_1,
        *,
        worker_id: str,
    ) -> None:
        if not worker_id:
            raise ValueError("timeline worker id cannot be empty")
        self._work, self._producer, self._refinements = work, producer, refinements
        self._worker_id = worker_id

    def run_once(self) -> bool:
        lease = self._work.claim(self._worker_id)
        if lease is None:
            return False
        try:
            result = self._producer.produce(lease)
        except Exception:  # noqa: BLE001 - isolated optional-work retry boundary
            self._work.retry_timeline(lease, "timeline-production-failed")
            return True
        # The complete cheap product is committed before optional refinement
        # admission. A broken or saturated exact queue cannot retract it.
        self._work.complete_timeline(lease, result.timeline_ref)
        try:
            self._refinements.dispatch(result.refinement_request)
        except Exception:  # noqa: BLE001 - exact overlay is independently optional
            self._work.record_refinement_dispatch_failure(
                lease, "refinement-dispatch-failed"
            )
        return True
