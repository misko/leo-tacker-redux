from __future__ import annotations

import json
from dataclasses import replace
from typing import cast

import pytest

from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
    StarlinkInjectionCaseV0_2,
    synthesize_starlink_injection_v0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    ReportMethodStarlinkDetectorV0_1,
    StarlinkDetectionParametersV0_1,
    StarlinkDetectorV0_1,
    StarlinkPairedSurrogateAnalyzerV0_1,
    StarlinkRadioSignalV0_1,
    precommitted_surrogate_codebook_v0_1,
    precommitted_surrogate_states_v0_1,
    qin_exact_search_pattern_v0_1,
    radio_signal_v0_1,
)
from leo_flow.analysis.recording.starlink_surrogate_null_codec import (
    MAX_PAIRED_SURROGATE_EVIDENCE_BYTES,
    MalformedPairedSurrogateEvidenceError,
    decode_paired_surrogate_evidence,
    encode_paired_surrogate_evidence,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import (
    Digest,
    ReceiverChainId,
    RecordingId,
    SegmentId,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    StarlinkDetectorMethod,
)
from leo_flow.contracts.starlink_surrogate_null import (
    StarlinkPairedSurrogateEvidenceV0_1,
    StarlinkPatternDetectionV0_1,
    StarlinkPatternSearchMode,
    StarlinkSearchPatternRole,
)

from .fakes import execution_context

SAMPLE_RATE_HZ = 2_500_000.0


def _config() -> StarlinkDetectorSuiteConfigV0_2:
    return StarlinkDetectorSuiteConfigV0_2((0, 3), (0.0, 1_000.0), (0.0, 50.0))


def _signal() -> StarlinkRadioSignalV0_1:
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    case = StarlinkInjectionCaseV0_2(
        "paired-surrogate",
        41,
        7_500,
        1.5,
        0.1,
        3,
        1_000.0,
        0.0,
        (0, 1),
    )
    samples = synthesize_starlink_injection_v0_2(templates, case)
    return radio_signal_v0_1(
        samples,
        recording_id=RecordingId("rec_paired_surrogate"),
        recording_identity_digest=Digest.sha256(b"paired-surrogate-recording"),
        segment_id=SegmentId("seg_ch4_lower"),
        receiver_chain_id=ReceiverChainId("rx_radio_20"),
        edge=StarlinkEdge.LOWER,
        sample_rate_hz=SAMPLE_RATE_HZ,
    )


class _ObservedDetector(StarlinkDetectorV0_1):
    def __init__(self) -> None:
        self.calls: list[
            tuple[StarlinkRadioSignalV0_1, StarlinkDetectionParametersV0_1]
        ] = []
        self.delegate = ReportMethodStarlinkDetectorV0_1(execution_context())

    def detect(
        self,
        radio_signal: StarlinkRadioSignalV0_1,
        parameters: StarlinkDetectionParametersV0_1,
    ) -> StarlinkPatternDetectionV0_1:
        self.calls.append((radio_signal, parameters))
        return self.delegate.detect(radio_signal, parameters)


@pytest.fixture(scope="module")
def observed_evidence() -> tuple[
    _ObservedDetector, StarlinkPairedSurrogateEvidenceV0_1
]:
    detector = _ObservedDetector()
    evidence = StarlinkPairedSurrogateAnalyzerV0_1(detector, _config()).analyze(
        _signal()
    )
    return detector, evidence


def test_exact_and_every_surrogate_use_one_detector_interface_and_config(
    observed_evidence: tuple[_ObservedDetector, StarlinkPairedSurrogateEvidenceV0_1],
) -> None:
    detector, evidence = observed_evidence

    assert len(detector.calls) == 5
    assert all(call[0] == detector.calls[0][0] for call in detector.calls)
    assert all(
        call[1].suite_config is detector.calls[0][1].suite_config
        for call in detector.calls
    )
    assert [call[1].pattern.identity.role for call in detector.calls] == [
        StarlinkSearchPatternRole.QIN_EXACT,
        *([StarlinkSearchPatternRole.PRECOMMITTED_SURROGATE] * 4),
    ]
    assert evidence.exact == detector.delegate.detect(*detector.calls[0])


def test_codebook_is_deterministic_unique_qpsk_and_not_input_seeded() -> None:
    first = precommitted_surrogate_codebook_v0_1(
        SAMPLE_RATE_HZ, StarlinkEdge.LOWER, count=8
    )
    replay = precommitted_surrogate_codebook_v0_1(
        SAMPLE_RATE_HZ, StarlinkEdge.LOWER, count=8
    )
    other_capture_configuration = precommitted_surrogate_codebook_v0_1(
        5_000_000.0, StarlinkEdge.UPPER, count=8
    )

    assert first == replay
    assert len({item.identity.template_ref for item in first}) == 8
    assert len({item.identity.qpsk_state_matrix_digest for item in first}) == 8
    assert [item.identity.generator_seed for item in first] == [
        item.identity.generator_seed for item in other_capture_configuration
    ]
    assert [item.identity.qpsk_state_matrix_digest for item in first] == [
        item.identity.qpsk_state_matrix_digest for item in other_capture_configuration
    ]
    exact_pattern = qin_exact_search_pattern_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    exact_energy = exact_pattern.identity.template_energy
    assert all(
        item.identity.template_ref != exact_pattern.identity.template_ref
        for item in first
    )
    for index, item in enumerate(first):
        states = precommitted_surrogate_states_v0_1(index)
        assert len(states) == 300
        assert all(len(row) == 8 and set(row) <= {0, 1, 2, 3} for row in states)
        assert item.identity.codebook_index == index
        assert item.identity.data_independent is True
        assert item.identity.template_energy == pytest.approx(exact_energy, rel=2e-7)


def test_all_methods_repeat_the_identical_full_search_plan(
    observed_evidence: tuple[_ObservedDetector, StarlinkPairedSurrogateEvidenceV0_1],
) -> None:
    _, evidence = observed_evidence

    assert tuple(item.method for item in evidence.method_nulls) == REPORT_METHOD_ORDER
    assert tuple(item.method for item in evidence.exact.methods) == REPORT_METHOD_ORDER
    assert evidence.exact.search_grid.epoch_hypotheses_samples == (0, 3)
    assert evidence.exact.search_grid.coarse_cfo_hypotheses_hz == (0.0, 1_000.0)
    assert evidence.exact.search_grid.glrt_residual_cfo_hypotheses_hz == (0.0, 50.0)
    assert all(
        item.search_grid == evidence.exact.search_grid for item in evidence.surrogates
    )
    for target in evidence.exact.methods:
        controls = [
            next(item for item in result.methods if item.method is target.method)
            for result in evidence.surrogates
        ]
        assert all(
            item.search_plan_digest == target.search_plan_digest for item in controls
        )
        assert all(
            item.effective_search_cell_count == target.effective_search_cell_count
            for item in controls
        )
        assert all(item.config_ref == target.config_ref for item in controls)
        assert all(item.algorithm_ref == target.algorithm_ref for item in controls)


def test_exact_common_port_reproduces_published_suite_target_search() -> None:
    signal = _signal()
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    published = StarlinkDetectorSuiteV0_2(
        _config(), execution_context()
    ).analyze_receiver(
        signal.samples,
        recording_id=signal.recording_id,
        recording_identity_digest=signal.recording_identity_digest,
        segment_id=signal.segment_id,
        receiver_chain_id=signal.receiver_chain_id,
        templates=templates,
    )
    common = ReportMethodStarlinkDetectorV0_1(execution_context()).detect(
        signal,
        StarlinkDetectionParametersV0_1(
            qin_exact_search_pattern_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER),
            _config(),
        ),
    )

    for expected, actual in zip(published.methods, common.methods, strict=True):
        assert actual.method is expected.method
        assert actual.score == pytest.approx(expected.reported_score, abs=1e-15)
        assert actual.winning_epoch_sample == expected.winning_epoch_sample
        assert actual.winning_coarse_cfo_hz == expected.winning_coarse_cfo_hz
        assert actual.winning_residual_cfo_hz == expected.winning_residual_cfo_hz


def test_full_frame_verify_and_full_use_each_patterns_own_acquire_winner(
    observed_evidence: tuple[_ObservedDetector, StarlinkPairedSurrogateEvidenceV0_1],
) -> None:
    _, evidence = observed_evidence

    for result in (evidence.exact, *evidence.surrogates):
        methods = {item.method: item for item in result.methods}
        acquire = methods[StarlinkDetectorMethod.FULL_FRAME_ACQUIRE]
        verify = methods[StarlinkDetectorMethod.FULL_FRAME_VERIFY]
        full = methods[StarlinkDetectorMethod.FULL_FRAME_FULL]
        assert acquire.search_mode is StarlinkPatternSearchMode.SEARCHED
        assert verify.search_mode is (
            StarlinkPatternSearchMode.CONDITIONED_ON_PATTERN_ACQUIRE_WINNER
        )
        assert full.search_mode is (
            StarlinkPatternSearchMode.CONDITIONED_ON_PATTERN_ACQUIRE_WINNER
        )
        assert (
            verify.selection_method
            is full.selection_method
            is (StarlinkDetectorMethod.FULL_FRAME_ACQUIRE)
        )
        assert (
            verify.winning_epoch_sample
            == full.winning_epoch_sample
            == acquire.winning_epoch_sample
        )
        assert (
            verify.winning_coarse_cfo_hz
            == full.winning_coarse_cfo_hz
            == acquire.winning_coarse_cfo_hz
        )


def test_evidence_records_scores_winners_pattern_and_provenance(
    observed_evidence: tuple[_ObservedDetector, StarlinkPairedSurrogateEvidenceV0_1],
) -> None:
    _, evidence = observed_evidence

    assert len(evidence.surrogates) == 4
    assert evidence.candidate_only is True
    assert set(evidence.warnings) >= {
        "finite-paired-surrogate-controls",
        "not-verified-signal-absent",
        "not-calibrated-detection",
    }
    for result in (evidence.exact, *evidence.surrogates):
        assert result.input_digest == _signal().input_digest
        assert (
            result.provenance.normalized_config_digest
            == result.methods[0].config_ref.digest
        )
        assert (
            result.pattern.template_ref.digest in result.provenance.dependency_digests
        )
        for method in result.methods:
            assert method.input_digest == result.input_digest
            assert method.pattern == result.pattern
            assert method.effective_search_cell_count > 0
            assert method.search_identity_digest != method.search_plan_digest


def test_codec_round_trips_exact_canonical_evidence(
    observed_evidence: tuple[_ObservedDetector, StarlinkPairedSurrogateEvidenceV0_1],
) -> None:
    _, evidence = observed_evidence
    payload = encode_paired_surrogate_evidence(evidence)

    assert decode_paired_surrogate_evidence(payload) == evidence
    assert (
        encode_paired_surrogate_evidence(decode_paired_surrogate_evidence(payload))
        == payload
    )
    noncanonical = json.dumps(json.loads(payload), indent=2).encode()
    with pytest.raises(MalformedPairedSurrogateEvidenceError, match="not canonical"):
        decode_paired_surrogate_evidence(noncanonical)
    with pytest.raises(MalformedPairedSurrogateEvidenceError, match="size limit"):
        decode_paired_surrogate_evidence(
            b"x" * (MAX_PAIRED_SURROGATE_EVIDENCE_BYTES + 1)
        )


@pytest.mark.parametrize("count", [0, 33, True, 1.5])
def test_surrogate_count_bounds_fail(count: object) -> None:
    analyzer = StarlinkPairedSurrogateAnalyzerV0_1(
        ReportMethodStarlinkDetectorV0_1(execution_context()), _config()
    )
    error = TypeError if count in (True, 1.5) else ValueError
    with pytest.raises(error):
        analyzer.analyze(_signal(), surrogate_count=cast(int, count))


def test_contract_rejects_a_surrogate_with_a_different_search_plan(
    observed_evidence: tuple[_ObservedDetector, StarlinkPairedSurrogateEvidenceV0_1],
) -> None:
    _, evidence = observed_evidence
    first = evidence.surrogates[0]
    changed_method = replace(
        first.methods[0], search_plan_digest=Digest.sha256(b"different-grid")
    )
    changed = replace(first, methods=(changed_method, *first.methods[1:]))

    with pytest.raises(ValueError, match="identical search plan"):
        replace(evidence, surrogates=(changed, *evidence.surrogates[1:]))
