from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from leo_flow.application.waterfall_projection_work import (
    WaterfallDashboardProjectionWorkerV0_1,
    WaterfallProjectionLeaseV0_1,
)
from tests.postgres.test_dashboard_recording_detail_waterfall import _waterfall
from tests.projection_writer_fixtures import published_recording, recording_manifest


def lease(attempt: int = 1) -> WaterfallProjectionLeaseV0_1:
    _bundle, ref = _waterfall(published_recording(recording_manifest(61)))
    from leo_flow.contracts.core import JobId

    return WaterfallProjectionLeaseV0_1(
        "wfwork_" + "1" * 64,
        JobId("job_waterfall_projection"),
        ref,
        "lease-token",
        1,
        attempt,
    )


class Work:
    def __init__(self, item: WaterfallProjectionLeaseV0_1 | None) -> None:
        self.item = item
        self.events: list[tuple[object, ...]] = []

    def claim(self, worker_id: str, lease_ttl_s: float):
        self.events.append(("claim", worker_id, lease_ttl_s))
        return self.item

    def complete(self, item) -> None:
        self.events.append(("complete", item.work_id))

    def retry(self, item, reason: str, delay_s: float) -> None:
        self.events.append(("retry", item.work_id, reason, delay_s))

    def park(self, item, reason: str) -> None:
        self.events.append(("park", item.work_id, reason))


@dataclass(frozen=True)
class DurableView:
    ref: object
    value: object

    def bundle(self):
        return self.value


class Durable:
    def __init__(self, bundle, ref) -> None:
        self.value = DurableView(ref, bundle)

    @contextmanager
    def open(self, ref):
        assert ref == self.value.ref
        yield self.value


class Projector:
    def __init__(self, *, failure: bool = False) -> None:
        self.failure = failure
        self.calls: list[tuple[object, object]] = []

    def project_complete(self, bundle, ref) -> int:
        self.calls.append((bundle, ref))
        if self.failure:
            raise RuntimeError("projection unavailable")
        return 7


def worker(work: Work, projector: Projector):
    bundle, ref = _waterfall(published_recording(recording_manifest(61)))
    return WaterfallDashboardProjectionWorkerV0_1(
        work,
        Durable(bundle, ref),
        projector,
        worker_id="waterfall-worker",
        lease_ttl_s=30,
        maximum_attempts=3,
        retry_delay_s=2,
    )


def test_projection_worker_validates_durable_bundle_then_completes() -> None:
    work = Work(lease())
    projector = Projector()
    assert worker(work, projector).process_one_work()
    assert len(projector.calls) == 1
    assert work.events == [
        ("claim", "waterfall-worker", 30),
        ("complete", "wfwork_" + "1" * 64),
    ]


def test_projection_worker_retries_then_parks_at_attempt_bound() -> None:
    retry_work = Work(lease(2))
    assert worker(retry_work, Projector(failure=True)).process_one_work()
    assert retry_work.events[-1] == (
        "retry",
        "wfwork_" + "1" * 64,
        "waterfall-projection-transient-failure",
        2,
    )

    park_work = Work(lease(3))
    assert worker(park_work, Projector(failure=True)).process_one_work()
    assert park_work.events[-1] == (
        "park",
        "wfwork_" + "1" * 64,
        "waterfall-projection-attempts-exhausted",
    )


def test_projection_worker_reports_no_work_without_opening_or_projecting() -> None:
    work = Work(None)
    projector = Projector()
    assert not worker(work, projector).process_one_work()
    assert not projector.calls
