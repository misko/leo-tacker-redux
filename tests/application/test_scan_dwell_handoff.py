from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.application import (
    DwellRequestGate,
    DwellRequestRejected,
    DwellSafetyPolicy,
)
from leo_flow.contracts.capture import ActivityKind, CapturePlan, GainMode, GainSetting
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    FeatureSetId,
    RadioId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    StationId,
    UtcNs,
)
from leo_flow.contracts.dwell import (
    MAX_DWELL_SAMPLE_COUNT,
    DwellRequest,
    ScanResultRef,
)
from leo_flow.contracts.evidence import EvidenceKind, LabelEvidenceRef
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.ports import DwellRequestEmitter, DwellRequestGatePort
from leo_flow.contracts.storage import ObjectRef

STATION = StationId("station_dwell")
RADIO = RadioId("radio_dwell")
CENTER_HZ = 1_825_000_000
RATE_HZ = 1_000_000
BANDWIDTH_HZ = 800_000


def _digest(value: str) -> Digest:
    return Digest.sha256(value.encode())


def _evidence() -> LabelEvidenceRef:
    return LabelEvidenceRef(
        SchemaRef(LabelEvidenceRef.SCHEMA_ID),
        "evidence_scan_candidate",
        EvidenceKind.TLE_WEAK_ASSOCIATION,
        ArtifactRef(
            "artifact_scan_association",
            _digest("scan-association"),
            SchemaRef("org.leo-flow.scan-association-evidence"),
        ),
        "producer_scan_analyzer",
        UtcNs(100),
    )


def _scan_result(
    *, station_id: StationId = STATION, radio_id: RadioId = RADIO
) -> ScanResultRef:
    feature_payload = b"scan-feature-set"
    return ScanResultRef(
        SchemaRef(ScanResultRef.SCHEMA_ID),
        "scanresult_candidate_one",
        RecordingId("rec_scan_candidate"),
        _digest("recording-identity"),
        FeatureSetRef(
            FeatureSetId("fset_scan_candidate"),
            AnalysisRunId("arun_scan_candidate"),
            ObjectRef(
                Digest.sha256(feature_payload),
                len(feature_payload),
                "application/json",
                "feature-set-bundle-v0.1",
                "memory:scan-feature-set",
            ),
        ),
        station_id,
        radio_id,
        UtcNs(100),
        CENTER_HZ,
        RATE_HZ,
        BANDWIDTH_HZ,
        (_evidence(),),
    )


def _request(
    result: ScanResultRef | None = None,
    *,
    sample_count: int = 2_000_000,
    idempotency_key: str = "scan-to-dwell:candidate-one",
) -> DwellRequest:
    source = result or _scan_result()
    return DwellRequest(
        SchemaRef(DwellRequest.SCHEMA_ID),
        "dwell_candidate_one",
        source,
        source.station_id,
        source.radio_id,
        UtcNs(200),
        UtcNs(1_000_000_200),
        source.center_frequency_hz,
        source.sample_rate_hz,
        source.bandwidth_hz,
        sample_count * 1_000_000_000 // source.sample_rate_hz,
        sample_count,
        "candidate_requires_longer_observation",
        source.evidence_refs,
        idempotency_key,
    )


def _policy() -> DwellSafetyPolicy:
    from leo_flow.contracts.core import ReceiverChainId

    return DwellSafetyPolicy(
        STATION,
        RADIO,
        (ReceiverChainId("rx_dwell_a"), ReceiverChainId("rx_dwell_b")),
        GainSetting(GainMode.MANUAL, 30.0),
        1_700_000_000,
        2_000_000_000,
        2_000_000,
        1_500_000,
        5_000_000_000,
        5_000_000,
    )


class FakeAnalysisEmitter:
    def emit(self, result: ScanResultRef) -> DwellRequest | None:
        return _request(result)


class FakeCaptureExecutor:
    def __init__(self, gate: DwellRequestGatePort) -> None:
        self._gate = gate
        self.executed: list[CapturePlan] = []

    def execute(self, request: DwellRequest, now_utc_ns: UtcNs):
        plan = self._gate.accept(request, now_utc_ns)
        if plan not in self.executed:
            self.executed.append(plan)
        return plan


def test_public_scan_result_to_capture_plan_is_bounded_and_idempotent() -> None:
    emitter: DwellRequestEmitter = FakeAnalysisEmitter()
    gate = DwellRequestGate(_policy())
    capture = FakeCaptureExecutor(gate)
    request = emitter.emit(_scan_result())
    assert request is not None

    first = capture.execute(request, UtcNs(300))
    replay = capture.execute(request, UtcNs(400))

    assert first is replay
    assert capture.executed == [first]
    assert first.radio_id == RADIO
    assert first.activities[0].kind is ActivityKind.DWELL
    segment = first.activities[0].segments[0]
    assert segment.center_frequency_hz == CENTER_HZ
    assert segment.sample_count == 2_000_000
    assert dict(segment.tags)["source_feature_set_id"] == "fset_scan_candidate"
    assert dict(segment.tags)["reason_code"] == request.reason_code
    assert "detector" not in repr(first).lower()


def test_gate_rejects_expiry_routing_policy_and_idempotency_conflicts() -> None:
    gate = DwellRequestGate(_policy())
    request = _request()
    gate.accept(request, UtcNs(300))

    with pytest.raises(DwellRequestRejected, match="currently valid"):
        gate.accept(request, request.expires_utc_ns)
    with pytest.raises(DwellRequestRejected, match="idempotency key"):
        gate.accept(
            replace(request, reason_code="changed_reason"),
            UtcNs(400),
        )
    with pytest.raises(DwellRequestRejected, match="capture policy"):
        gate.accept(_request(sample_count=5_000_001), UtcNs(300))
    other = _request(
        _scan_result(
            station_id=StationId("station_other"), radio_id=RadioId("radio_other")
        ),
        idempotency_key="scan-to-dwell:other",
    )
    with pytest.raises(DwellRequestRejected, match="another station"):
        gate.accept(other, UtcNs(300))


def test_contract_rejects_unknown_versions_inconsistent_evidence_and_hard_caps() -> (
    None
):
    result = _scan_result()
    with pytest.raises(ValueError, match="unsupported scan result"):
        replace(
            result,
            schema=SchemaRef(result.SCHEMA_ID, SchemaVersion(1, 0)),
        )
    request = _request(result)
    with pytest.raises(ValueError, match="unsupported dwell request"):
        replace(
            request,
            schema=SchemaRef("org.leo-flow.unknown-dwell-request"),
        )
    with pytest.raises(ValueError, match="evidence differs"):
        replace(
            request,
            evidence_refs=(
                _evidence(),
                replace(_evidence(), evidence_id="evidence_z"),
            ),
        )
    with pytest.raises(ValueError, match="sample count"):
        replace(request, sample_count=MAX_DWELL_SAMPLE_COUNT + 1)


def test_capture_side_gate_has_no_analysis_or_detector_dependency() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "leo_flow"
        / "application"
        / "dwell.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(
        module.startswith(("leo_flow.analysis", "leo_flow.capture."))
        for module in modules
    )
    assert "detector" not in source.read_text(encoding="utf-8").lower()
