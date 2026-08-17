from __future__ import annotations

import cmath
import math
import struct
from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink import (
    CONFIG_SCHEMA_ID,
    CONTROL_SYMBOL_ROLL,
    FRAME_RATE_HZ,
    TEMPLATE_SCHEMA_ID,
    KnownCodePilotSearchConfigV0_1,
    KnownCodePilotSearchV0_1,
    KnownCodePilotTemplatePairV0_1,
    evaluate_pilot_candidate_v0_1,
    template_samples_digest,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
)
from leo_flow.contracts.starlink import (
    StarlinkEdge,
    StarlinkEvaluationState,
    StarlinkPilotCalibrationV0_1,
)

from .fakes import execution_context


def _templates() -> KnownCodePilotTemplatePairV0_1:
    exact = tuple(
        _complex64(cmath.exp(2j * math.pi * index / 7) * (1 if index % 3 else -1))
        for index in range(12)
    )
    control = exact[5:] + exact[:5]
    schema = SchemaRef(TEMPLATE_SCHEMA_ID, V0_1)
    return KnownCodePilotTemplatePairV0_1(
        StarlinkEdge.LOWER,
        tuple(range(528, 536)),
        12_000.0,
        ArtifactRef(
            "qin-lower-edge-exact-v0.1", template_samples_digest(exact), schema
        ),
        ArtifactRef(
            "qin-lower-edge-roll17-control-v0.1",
            template_samples_digest(control),
            schema,
        ),
        exact,
        control,
    )


def _complex64(value: complex) -> complex:
    real, imag = struct.unpack("<ff", struct.pack("<ff", value.real, value.imag))
    return complex(real, imag)


def _config(*, cfo: tuple[float, ...] = (-300.0, 0.0, 300.0)):
    return KnownCodePilotSearchConfigV0_1((0, 1, 2, 3), cfo)


def _signal(templates: KnownCodePilotTemplatePairV0_1) -> tuple[complex, ...]:
    values = [0j] * 82
    period = round(templates.sample_rate_hz / FRAME_RATE_HZ)
    for frame in range(5):
        start = 2 + frame * period
        frame_phase = cmath.exp(1j * (0.31 + 0.47 * frame))
        for local_index, reference in enumerate(templates.exact_samples):
            sample_index = start + local_index
            cfo_phase = cmath.exp(
                2j * math.pi * 300.0 * sample_index / templates.sample_rate_hz
            )
            values[sample_index] += 3.0 * frame_phase * cfo_phase * reference
    return tuple(values)


def _candidate(config: KnownCodePilotSearchConfigV0_1 | None = None):
    templates = _templates()
    analyzer = KnownCodePilotSearchV0_1(config or _config(), execution_context())
    bundle = analyzer.analyze_receiver(
        _signal(templates),
        recording_id=RecordingId("rec_starlink_test"),
        recording_identity_digest=Digest.sha256(b"recording"),
        segment_id=SegmentId("seg_starlink_test"),
        receiver_chain_id=ReceiverChainId("rx_starlink_test"),
        templates=templates,
    )
    return bundle, bundle.candidates[0]


def test_search_finds_declared_epoch_and_cfo_without_emitting_detection() -> None:
    first_bundle, candidate = _candidate()
    second_bundle, second_candidate = _candidate()

    assert first_bundle == second_bundle
    assert first_bundle.digest == second_bundle.digest
    assert candidate == second_candidate
    assert candidate.winning_epoch_sample == 2
    assert candidate.winning_cfo_hz == 300.0
    assert candidate.search_cell_count == 12
    assert candidate.probe_sample_count == 82
    assert candidate.frame_support == 5
    assert candidate.searched_exact_score == pytest.approx(1.0)
    assert candidate.conditioned_exact_score == pytest.approx(1.0)
    assert candidate.exact_minus_control_margin > 0.1
    assert candidate.control_conditioning == "winning-epoch-and-cfo-fixed"
    assert candidate.pss_evidence_status == "not_evaluated"
    assert "search-maximum-not-a-detection" in candidate.reason_codes

    evaluation = evaluate_pilot_candidate_v0_1(candidate, None)
    assert evaluation.state is StarlinkEvaluationState.UNCALIBRATED
    assert evaluation.detected is None
    assert evaluation.threshold is None


def test_search_identity_pins_probe_length_and_complete_search_bank() -> None:
    _, base = _candidate()
    templates = _templates()
    altered_bank = (
        KnownCodePilotSearchV0_1(
            _config(cfo=(-300.0, 0.0, 200.0, 300.0)), execution_context()
        )
        .analyze_receiver(
            _signal(templates),
            recording_id=RecordingId("rec_starlink_test"),
            recording_identity_digest=Digest.sha256(b"recording"),
            segment_id=SegmentId("seg_starlink_test"),
            receiver_chain_id=ReceiverChainId("rx_starlink_test"),
            templates=templates,
        )
        .candidates[0]
    )
    longer_probe = (
        KnownCodePilotSearchV0_1(_config(), execution_context())
        .analyze_receiver(
            _signal(templates) + (0j,),
            recording_id=RecordingId("rec_starlink_test"),
            recording_identity_digest=Digest.sha256(b"recording"),
            segment_id=SegmentId("seg_starlink_test"),
            receiver_chain_id=ReceiverChainId("rx_starlink_test"),
            templates=templates,
        )
        .candidates[0]
    )

    assert base.search_identity_digest != altered_bank.search_identity_digest
    assert base.search_identity_digest != longer_probe.search_identity_digest


def test_only_exact_matching_calibration_can_emit_detection() -> None:
    _, candidate = _candidate()
    hardware = Digest.sha256(b"pluto-20-rx0-profile")
    calibration = StarlinkPilotCalibrationV0_1(
        SchemaRef(StarlinkPilotCalibrationV0_1.SCHEMA_ID, V0_1),
        "slcalibration_test",
        candidate.algorithm_ref.digest,
        candidate.config_ref.digest,
        candidate.exact_template_ref.digest,
        candidate.conditioned_control_template_ref.digest,
        candidate.search_identity_digest,
        hardware,
        Digest.sha256(b"locked-null-corpus"),
        Digest.sha256(b"held-out-null-split"),
        "searched-exact-minus-conditioned-control-margin",
        "whole-search",
        0.25,
        0.01,
        10_000,
        100,
    )

    evaluation = evaluate_pilot_candidate_v0_1(
        candidate, calibration, hardware_profile_digest=hardware
    )
    assert evaluation.state is StarlinkEvaluationState.CALIBRATED
    assert evaluation.detected is True
    assert evaluation.threshold == 0.25

    with pytest.raises(ValueError, match="hardware profile"):
        evaluate_pilot_candidate_v0_1(candidate, calibration)
    with pytest.raises(ValueError, match="config"):
        evaluate_pilot_candidate_v0_1(
            candidate,
            replace(calibration, config_digest=Digest.sha256(b"other-config")),
            hardware_profile_digest=hardware,
        )


def test_template_identity_and_execution_bounds_fail_closed() -> None:
    templates = _templates()
    with pytest.raises(ValueError, match="identify its samples"):
        replace(
            templates,
            exact_ref=ArtifactRef(
                "wrong-exact",
                Digest.sha256(b"not-the-template"),
                SchemaRef(TEMPLATE_SCHEMA_ID, V0_1),
            ),
        )
    with pytest.raises(ValueError, match="template schema"):
        replace(
            templates,
            exact_ref=ArtifactRef(
                "wrong-schema",
                templates.exact_ref.digest,
                SchemaRef(CONFIG_SCHEMA_ID, V0_1),
            ),
        )
    with pytest.raises(ValueError, match="maximum_search_cells"):
        KnownCodePilotSearchConfigV0_1((0, 1), (0.0, 1.0), maximum_search_cells=3)
    with pytest.raises(ValueError, match="maximum_probe_samples"):
        KnownCodePilotSearchV0_1(
            replace(_config(), maximum_probe_samples=10), execution_context()
        ).analyze_receiver(
            _signal(templates),
            recording_id=RecordingId("rec_starlink_test"),
            recording_identity_digest=Digest.sha256(b"recording"),
            segment_id=SegmentId("seg_starlink_test"),
            receiver_chain_id=ReceiverChainId("rx_starlink_test"),
            templates=templates,
        )


def test_frozen_science_constants_are_explicit() -> None:
    assert FRAME_RATE_HZ == 750.0
    assert CONTROL_SYMBOL_ROLL == 17
