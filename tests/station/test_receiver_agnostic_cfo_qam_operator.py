from __future__ import annotations

import json
from io import StringIO

from leo_flow.contracts.core import Digest, RecordingId
from leo_flow.contracts.optional_heavy_work_admission import (
    HeavyWorkAdmissionDecisionV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_receiver_agnostic_cfo_product import (
    ReceiverAgnosticCfoQamRecordingProductRefV0_6,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.services.starlink_receiver_agnostic_cfo_qam import (
    ReceiverAgnosticCfoQamProductionResultV0_6,
)
from leo_station import receiver_agnostic_cfo_qam_operator


class _Permit:
    def release(self) -> None:
        pass


class _Admission:
    def acquire(self):  # type: ignore[no-untyped-def]
        return HeavyWorkAdmissionDecisionV0_1(True, "admitted"), _Permit()


class _Cycle:
    def __init__(self, result=None):  # type: ignore[no-untyped-def]
        self.result = result
        self.calls = []

    def run_once(self, recording_id, selections):  # type: ignore[no-untyped-def]
        self.calls.append((recording_id, selections))
        return ("complete", self.result)


def _argv() -> list[str]:
    return [
        "--credential-directory",
        "/credentials",
        "--capture-guard-status",
        "/run/leo-flow-optional-heavy/guard.json",
        "--recording-id",
        "rec_operator",
        "--window",
        "seg_00:rx_0:lower:10000:20000",
    ]


def test_operator_parses_exact_window_and_reports_publication() -> None:
    ref = ReceiverAgnosticCfoQamRecordingProductRefV0_6(
        "slcfoqam6rec_" + "a" * 32,
        RecordingId("rec_operator"),
        ObjectRef(
            Digest.sha256(b"bundle"),
            6,
            "application/json",
            "receiver-agnostic-cfo-qam-bundle-json-v0.6",
            "cas:bundle",
        ),
    )
    cycle = _Cycle(ReceiverAgnosticCfoQamProductionResultV0_6(ref, False))
    stdout, observed = StringIO(), {}

    result = receiver_agnostic_cfo_qam_operator.main(
        _argv(),
        stdout=stdout,
        cycle_builder=lambda path, admission: (
            observed.update(path=path, admission=admission) or cycle
        ),
        admission_builder=lambda path, **kwargs: (
            observed.update(guard=path, policy=kwargs) or _Admission()
        ),
    )

    assert result == 0
    document = json.loads(stdout.getvalue())
    assert document["event"] == "receiver_agnostic_cfo_qam_complete"
    assert document["analysis_id"] == ref.analysis_id
    assert not document["reused"]
    recording_id, selections = cycle.calls[0]
    assert recording_id == RecordingId("rec_operator")
    assert len(selections) == 1
    assert selections[0].edge is StarlinkEdge.LOWER
    assert (selections[0].start_sample, selections[0].sample_count) == (10_000, 20_000)
    assert observed["policy"]["maximum_optional_concurrency"] == 1


def test_operator_reports_capture_guard_pause_without_claim() -> None:
    cycle = _Cycle()
    cycle.run_once = lambda *_args: ("capture-guard-active", None)  # type: ignore[method-assign]
    stdout = StringIO()
    result = receiver_agnostic_cfo_qam_operator.main(
        _argv(),
        stdout=stdout,
        cycle_builder=lambda *_args: cycle,
        admission_builder=lambda *_args, **_kwargs: _Admission(),
    )
    assert result == 0
    assert json.loads(stdout.getvalue()) == {
        "event": "receiver_agnostic_cfo_qam_paused",
        "reason": "capture-guard-active",
        "recording_id": "rec_operator",
    }


def test_operator_rejects_implicit_or_receiver_adjusted_window_syntax() -> None:
    stderr = StringIO()
    result = receiver_agnostic_cfo_qam_operator.main(
        [*_argv()[:-1], "seg_00:rx_0:lower:+600k:20000"],
        stderr=stderr,
        cycle_builder=lambda *_args: _Cycle(),
        admission_builder=lambda *_args, **_kwargs: _Admission(),
    )
    assert result == 4
    assert json.loads(stderr.getvalue())["event"] == "receiver_agnostic_cfo_qam_failed"
