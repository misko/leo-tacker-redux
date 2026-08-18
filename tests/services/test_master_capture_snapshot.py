from __future__ import annotations

import pytest

from leo_flow.contracts.core import UtcNs
from leo_flow.contracts.dashboard_master_capture import (
    MasterCaptureObservationV0_1,
    MasterCaptureRetroQamCanaryV0_1,
    MasterCaptureSnapshotQueryV0_1,
    MasterCaptureSnapshotV0_1,
    MasterCaptureSummaryState,
)
from leo_flow.contracts.dashboard_observation import ObservationAggregateViewV0_1
from leo_flow.services.master_capture_snapshot import (
    MasterCaptureSnapshotQueryServiceV0_1,
)


class _Repository:
    calls = 0

    def __init__(self, *, wrong_interval: bool = False) -> None:
        self.wrong_interval = wrong_interval

    def master_capture_snapshot(self, query, cursor=None):
        self.calls += 1
        assert cursor == "opaque"
        start = UtcNs(2) if self.wrong_interval else query.start_utc_ns
        return _empty_snapshot(start, query.stop_utc_ns)


def test_service_uses_exactly_one_snapshot_repository_call() -> None:
    repository = _Repository()
    service = MasterCaptureSnapshotQueryServiceV0_1(repository)
    view = service.master_capture_snapshot(
        MasterCaptureSnapshotQueryV0_1(UtcNs(1), UtcNs(10), 100), "opaque"
    )
    assert repository.calls == 1
    assert view.items == ()


def test_service_rejects_a_repository_snapshot_for_another_interval() -> None:
    service = MasterCaptureSnapshotQueryServiceV0_1(_Repository(wrong_interval=True))
    with pytest.raises(RuntimeError, match="another interval"):
        service.master_capture_snapshot(
            MasterCaptureSnapshotQueryV0_1(UtcNs(1), UtcNs(10), 100), "opaque"
        )


def _empty_snapshot(start: UtcNs, stop: UtcNs) -> MasterCaptureSnapshotV0_1:
    observation = ObservationAggregateViewV0_1(
        1,
        start,
        stop,
        0,
        0,
        0,
        0,
        "required",
        "whole-search-calibration-required",
        (),
        (),
        (),
        False,
    )
    return MasterCaptureSnapshotV0_1(
        1,
        start,
        stop,
        (),
        None,
        MasterCaptureObservationV0_1(
            MasterCaptureSummaryState.COMPLETE, observation, ()
        ),
        MasterCaptureRetroQamCanaryV0_1(
            MasterCaptureSummaryState.UNAVAILABLE,
            None,
            ("retro-qam-canary-unavailable",),
        ),
        (
            "candidate-only-qam-goodness-not-starlink-detection",
            "radio-lnb-receiver-series-are-never-pooled",
        ),
    )
