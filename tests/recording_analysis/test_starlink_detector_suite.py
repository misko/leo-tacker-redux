from __future__ import annotations

import cmath
import json
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.analysis.recording.starlink import (
    KnownCodePilotTemplatePairV0_1,
    template_samples_digest,
)
from leo_flow.analysis.recording.starlink_detector_suite import (
    PSS_SSS_TEMPLATE_SCHEMA_ID,
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
    StarlinkInjectionCaseV0_2,
    StarlinkPssSssTemplateV0_2,
    StarlinkRadioCandidateObservationV0_2,
    build_multi_radio_candidate_evidence_v0_2,
    run_starlink_injection_cases_v0_2,
    synthesize_starlink_injection_v0_2,
)
from leo_flow.analysis.recording.starlink_full_search_control_codec import (
    MalformedFullSearchControlBundleError,
    decode_full_search_control_bundle,
    encode_full_search_control_bundle,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    V0_2,
    StarlinkDetectorMethod,
    StarlinkSamplingStratum,
    StarlinkSearchMode,
)
from leo_flow.contracts.starlink_full_search_control import (
    V0_1 as FULL_SEARCH_CONTROL_V0_1,
)
from leo_flow.contracts.starlink_full_search_control import (
    StarlinkFullSearchControlMode,
    StarlinkFullSearchControlRecordingBundleV0_1,
    StarlinkFullSearchControlRecordingState,
)

from .fakes import execution_context

SAMPLE_RATE_HZ = 2_500_000.0


def _analyzer() -> StarlinkDetectorSuiteV0_2:
    return StarlinkDetectorSuiteV0_2(
        StarlinkDetectorSuiteConfigV0_2(
            (0, 3, 6),
            (0.0, 1_000.0),
            (-100.0, 0.0, 100.0),
        ),
        execution_context(),
    )


def _case(
    case_id: str = "present", *, amplitude: float = 2.0
) -> StarlinkInjectionCaseV0_2:
    return StarlinkInjectionCaseV0_2(
        case_id,
        2,
        14_000,
        amplitude,
        0.1,
        3,
        1_000.0,
        0.0,
        (0, 1, 2, 3) if amplitude else (),
    )


def _bundle():
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    values = synthesize_starlink_injection_v0_2(templates, _case())
    return _analyzer().analyze_receiver(
        values,
        recording_id=RecordingId("rec_detector_suite"),
        recording_identity_digest=Digest.sha256(b"recording"),
        segment_id=SegmentId("seg_detector_suite"),
        receiver_chain_id=ReceiverChainId("rx_detector_suite"),
        templates=templates,
    )


def test_all_report_methods_are_emitted_with_exact_conditioned_identities() -> None:
    bundle = _bundle()

    assert tuple(item.method for item in bundle.methods) == REPORT_METHOD_ORDER
    assert bundle.candidates_only is True
    assert bundle.sampling_stratum is StarlinkSamplingStratum.FULL_PILOT_BAND
    for item in bundle.methods:
        assert item.reported_score == item.conditioned_exact_score
        assert item.exact_minus_control_margin == pytest.approx(
            item.conditioned_exact_score - item.conditioned_control_score,
            abs=1e-12,
        )
        assert item.control_conditioning == (
            "exact-winning-epoch-coarse-and-residual-cfo-fixed"
        )
        assert item.exact_frames.support == item.control_frames.support
        assert item.candidate_only is True


def test_scores_are_invariant_to_receiver_gain_and_global_phase() -> None:
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    values = synthesize_starlink_injection_v0_2(templates, _case())
    rotated = tuple(7.5 * cmath.exp(0.73j) * value for value in values)

    def analyze(samples: tuple[complex, ...], tag: str):
        return _analyzer().analyze_receiver(
            samples,
            recording_id=RecordingId(f"rec_{tag}"),
            recording_identity_digest=Digest.sha256(tag.encode()),
            segment_id=SegmentId(f"seg_{tag}"),
            receiver_chain_id=ReceiverChainId("rx_invariance"),
            templates=templates,
        )

    base = analyze(values, "base")
    transformed = analyze(rotated, "transformed")
    assert [item.reported_score for item in transformed.methods] == pytest.approx(
        [item.reported_score for item in base.methods], abs=2e-12
    )
    assert [
        item.conditioned_control_score for item in transformed.methods
    ] == pytest.approx(
        [item.conditioned_control_score for item in base.methods], abs=2e-12
    )


def test_acquire_selects_and_verify_is_disjoint_and_conditioned() -> None:
    by_method = {item.method: item for item in _bundle().methods}
    acquire = by_method[StarlinkDetectorMethod.FULL_FRAME_ACQUIRE]
    verify = by_method[StarlinkDetectorMethod.FULL_FRAME_VERIFY]
    full = by_method[StarlinkDetectorMethod.FULL_FRAME_FULL]

    assert acquire.search_mode is StarlinkSearchMode.SEARCHED_EXACT
    assert verify.search_mode is StarlinkSearchMode.CONDITIONED_ON_ACQUIRE_WINNER
    assert full.search_mode is StarlinkSearchMode.CONDITIONED_ON_ACQUIRE_WINNER
    assert set(acquire.pilot_symbol_indices).isdisjoint(verify.pilot_symbol_indices)
    assert set(acquire.pilot_symbol_indices) | set(verify.pilot_symbol_indices) == set(
        range(2, 302)
    )
    assert verify.winning_epoch_sample == full.winning_epoch_sample == 3
    assert verify.winning_coarse_cfo_hz == full.winning_coarse_cfo_hz == 1_000.0


def test_verify_sample_mutation_cannot_change_acquire_statistic() -> None:
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    original = synthesize_starlink_injection_v0_2(templates, _case())
    mutated = list(original)
    samples_per_symbol = SAMPLE_RATE_HZ * 4.4e-6
    for frame in range(4):
        frame_start = 3 + round(frame * SAMPLE_RATE_HZ / 750.0)
        for symbol in range(3, 302, 2):
            local_start = round(symbol * samples_per_symbol)
            local_stop = min(
                round((symbol + 1) * samples_per_symbol), len(templates.exact_samples)
            )
            for index in range(frame_start + local_start, frame_start + local_stop):
                mutated[index] = 0j
    analyzer = StarlinkDetectorSuiteV0_2(
        StarlinkDetectorSuiteConfigV0_2((3,), (1_000.0,)), execution_context()
    )

    def analyze(samples: tuple[complex, ...], tag: str):
        return analyzer.analyze_receiver(
            samples,
            recording_id=RecordingId(f"rec_{tag}"),
            recording_identity_digest=Digest.sha256(tag.encode()),
            segment_id=SegmentId(f"seg_{tag}"),
            receiver_chain_id=ReceiverChainId("rx_split"),
            templates=templates,
        )

    base = {item.method: item for item in analyze(original, "split-base").methods}
    changed = {
        item.method: item for item in analyze(tuple(mutated), "split-mutated").methods
    }
    assert changed[
        StarlinkDetectorMethod.FULL_FRAME_ACQUIRE
    ].reported_score == pytest.approx(
        base[StarlinkDetectorMethod.FULL_FRAME_ACQUIRE].reported_score, abs=1e-15
    )
    assert changed[StarlinkDetectorMethod.FULL_FRAME_VERIFY].reported_score < 0.01
    assert base[StarlinkDetectorMethod.FULL_FRAME_VERIFY].reported_score > 0.99


def test_injection_harness_runs_independent_whole_search_trials_without_verdicts() -> (
    None
):
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    present = _case()
    null = _case("null", amplitude=0.0)

    first = run_starlink_injection_cases_v0_2(_analyzer(), templates, (present, null))
    second = run_starlink_injection_cases_v0_2(_analyzer(), templates, (present, null))

    assert first == second
    assert first[0].samples_digest != first[1].samples_digest
    assert all(result.bundle.candidates_only for result in first)
    assert all(
        method.effective_search_cell_count >= 6
        for result in first
        for method in result.bundle.methods
    )
    assert (
        first[0].bundle.methods[0].reported_score
        > first[1].bundle.methods[0].reported_score
    )


def test_pss_sss_lag_doppler_is_supporting_evidence_only() -> None:
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    values = synthesize_starlink_injection_v0_2(templates, _case())
    # Production supplies Qin's independently generated PSS+SSS waveform.
    replica = templates.exact_samples[:128]
    pss_sss = StarlinkPssSssTemplateV0_2(
        ArtifactRef(
            "qin-pss-sss-test-replica-v0.2",
            template_samples_digest(replica),
            SchemaRef(PSS_SSS_TEMPLATE_SCHEMA_ID, V0_2),
        ),
        SAMPLE_RATE_HZ,
        replica,
        0.01,
    )
    bundle = _analyzer().analyze_receiver(
        values,
        recording_id=RecordingId("rec_pss"),
        recording_identity_digest=Digest.sha256(b"pss"),
        segment_id=SegmentId("seg_pss"),
        receiver_chain_id=ReceiverChainId("rx_pss"),
        templates=templates,
        pss_sss_template=pss_sss,
    )

    acquisition = bundle.pss_sss_acquisition
    assert acquisition is not None
    assert acquisition.supporting_only is True
    assert acquisition.search_cell_count == 6
    assert acquisition.searched_score == acquisition.conditioned_score
    assert "not-edge-pilot-detection" in acquisition.reason_codes
    assert "pss-sss-captured-energy-too-low-for-primary-detection" in bundle.warnings


def test_input_bounds_and_clipped_sampling_stratum_are_explicit() -> None:
    config = StarlinkDetectorSuiteConfigV0_2((0,), (0.0,), maximum_probe_samples=10)
    templates = qin_edge_pilot_template_pair_v0_1(1_250_000.0, StarlinkEdge.LOWER)
    analyzer = StarlinkDetectorSuiteV0_2(config, execution_context())

    with pytest.raises(ValueError, match="maximum_probe_samples"):
        analyzer.analyze_receiver(
            (0j,) * 11,
            recording_id=RecordingId("rec_bound"),
            recording_identity_digest=Digest.sha256(b"bound"),
            segment_id=SegmentId("seg_bound"),
            receiver_chain_id=ReceiverChainId("rx_bound"),
            templates=templates,
        )

    with pytest.raises(ValueError, match="sampling stratum"):
        replace(_bundle(), sample_rate_hz=1_250_000.0)

    full_templates = qin_edge_pilot_template_pair_v0_1(
        SAMPLE_RATE_HZ, StarlinkEdge.LOWER
    )
    with pytest.raises(ValueError, match="maximum_cases"):
        run_starlink_injection_cases_v0_2(
            _analyzer(), full_templates, (_case("one"), _case("two")), maximum_cases=1
        )


def test_multi_radio_evidence_is_noncoherent_and_never_claims_hardware_sync() -> None:
    bundle = _bundle()
    observations = tuple(
        StarlinkRadioCandidateObservationV0_2(
            RadioId(radio),
            ReceiverChainId(receiver),
            1,
            StarlinkEdge.LOWER,
            UtcNs(1_800_000_000_000_000_000 + offset),
            UtcNs(1_800_000_001_000_000_000 + offset),
            skew,
            replace(bundle, receiver_chain_id=ReceiverChainId(receiver)),
        )
        for radio, receiver, offset, skew in (
            ("radio_20", "rx_20", 0, 4_000_000),
            ("radio_21", "rx_21", 2_000_000, 6_000_000),
        )
    )

    evidence = build_multi_radio_candidate_evidence_v0_2(
        observations, maximum_time_gap_ns=10_000_000, maximum_cfo_span_hz=1.0
    )

    assert evidence.radio_ids == (RadioId("radio_20"), RadioId("radio_21"))
    assert evidence.phase_combination == "none-noncoherent-evidence-only"
    assert evidence.coincidence_basis == "software-coordinated-multi-radio"
    assert evidence.candidate_only is True
    assert "no-hardware-synchronization-claim" in evidence.reason_codes

    with pytest.raises(ValueError, match="non-negative"):
        build_multi_radio_candidate_evidence_v0_2(
            (
                replace(observations[0], observed_first_sample_skew_ns=-1),
                observations[1],
            ),
            maximum_time_gap_ns=10_000_000,
            maximum_cfo_span_hz=1.0,
        )


def test_contract_rejects_false_verdict_and_search_conditioning_drift() -> None:
    evidence = _bundle().methods[0]

    with pytest.raises(ValueError, match="cannot emit a verdict"):
        replace(evidence, candidate_only=False)
    with pytest.raises(ValueError, match="reproduce"):
        replace(evidence, reported_score=evidence.reported_score / 2)
    with pytest.raises(ValueError, match="exact winner"):
        replace(evidence, control_conditioning="independently-searched-roll")


def test_searched_roll_reacquires_shift_but_same_cell_control_remains_suppressed() -> (
    None
):
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    case = replace(
        _case(),
        case_id="shifted-roll",
        sample_count=15_000,
        epoch_sample=200,
        cfo_hz=0.0,
        noise_standard_deviation=0.02,
    )
    values = synthesize_starlink_injection_v0_2(templates, case)
    analyzer = StarlinkDetectorSuiteV0_2(
        StarlinkDetectorSuiteConfigV0_2((13, 200), (0.0,), (0.0,)),
        execution_context(),
    )
    exact = analyzer.analyze_receiver(
        values,
        recording_id=RecordingId("rec_exact_roll_test"),
        recording_identity_digest=Digest.sha256(b"exact-roll-test"),
        segment_id=SegmentId("seg_exact_roll_test"),
        receiver_chain_id=ReceiverChainId("rx_exact_roll_test"),
        templates=templates,
    )
    roll_as_search_target = KnownCodePilotTemplatePairV0_1(
        templates.edge,
        templates.pilot_indices,
        templates.sample_rate_hz,
        templates.conditioned_control_ref,
        templates.exact_ref,
        templates.conditioned_control_samples,
        templates.exact_samples,
    )
    searched_roll = analyzer.analyze_receiver(
        values,
        recording_id=RecordingId("rec_searched_roll_test"),
        recording_identity_digest=Digest.sha256(b"searched-roll-test"),
        segment_id=SegmentId("seg_searched_roll_test"),
        receiver_chain_id=ReceiverChainId("rx_searched_roll_test"),
        templates=roll_as_search_target,
    )
    full_search_control = analyzer.analyze_full_search_control(
        values,
        recording_id=RecordingId("rec_full_search_control_test"),
        recording_identity_digest=Digest.sha256(b"full-search-control-test"),
        segment_id=SegmentId("seg_full_search_control_test"),
        receiver_chain_id=ReceiverChainId("rx_full_search_control_test"),
        templates=templates,
    )

    exact_anchor = exact.methods[0]
    searched_roll_anchor = searched_roll.methods[0]
    symmetric_anchor = full_search_control.methods[0]
    assert exact_anchor.winning_epoch_sample == 200
    assert exact_anchor.conditioned_control_score < 0.1
    assert searched_roll_anchor.winning_epoch_sample == 13
    assert searched_roll_anchor.reported_score > 0.9
    assert symmetric_anchor.winning_epoch_sample == 13
    assert symmetric_anchor.full_search_control_score == pytest.approx(
        searched_roll_anchor.reported_score, abs=2e-12
    )
    assert symmetric_anchor.effective_search_cell_count == (
        exact_anchor.effective_search_cell_count
    )
    assert symmetric_anchor.control_search == (
        "rolled-template-independent-full-search"
    )
    assert full_search_control.surrogate_only is True
    assert "not-an-empirical-null-distribution" in full_search_control.warnings
    for symmetric, independently_searched in zip(
        full_search_control.methods, searched_roll.methods, strict=True
    ):
        assert symmetric.method is independently_searched.method
        assert symmetric.full_search_control_score == pytest.approx(
            independently_searched.reported_score, abs=2e-12
        )
        assert symmetric.winning_epoch_sample == (
            independently_searched.winning_epoch_sample
        )
        assert symmetric.winning_coarse_cfo_hz == pytest.approx(
            independently_searched.winning_coarse_cfo_hz, abs=1e-12
        )
        assert symmetric.winning_residual_cfo_hz == pytest.approx(
            independently_searched.winning_residual_cfo_hz, abs=1e-12
        )


def test_full_search_control_mirrors_all_target_search_modes_without_verdict() -> None:
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.UPPER)
    values = synthesize_starlink_injection_v0_2(templates, _case())
    control = _analyzer().analyze_full_search_control(
        values,
        recording_id=RecordingId("rec_symmetric_control"),
        recording_identity_digest=Digest.sha256(b"symmetric-control"),
        segment_id=SegmentId("seg_symmetric_control"),
        receiver_chain_id=ReceiverChainId("rx_symmetric_control"),
        templates=templates,
    )

    assert tuple(item.method for item in control.methods) == REPORT_METHOD_ORDER
    by_method = {item.method: item for item in control.methods}
    acquire = by_method[StarlinkDetectorMethod.FULL_FRAME_ACQUIRE]
    verify = by_method[StarlinkDetectorMethod.FULL_FRAME_VERIFY]
    full = by_method[StarlinkDetectorMethod.FULL_FRAME_FULL]
    assert acquire.search_mode is StarlinkFullSearchControlMode.SEARCHED_ROLLED_TEMPLATE
    assert (
        verify.search_mode
        is StarlinkFullSearchControlMode.CONDITIONED_ON_ROLLED_ACQUIRE_WINNER
    )
    assert (
        full.search_mode
        is StarlinkFullSearchControlMode.CONDITIONED_ON_ROLLED_ACQUIRE_WINNER
    )
    assert (
        verify.winning_epoch_sample
        == full.winning_epoch_sample
        == (acquire.winning_epoch_sample)
    )
    assert (
        verify.winning_coarse_cfo_hz
        == full.winning_coarse_cfo_hz
        == (acquire.winning_coarse_cfo_hz)
    )
    assert all(item.surrogate_only for item in control.methods)

    with pytest.raises(ValueError, match="cannot be a detection verdict"):
        replace(control.methods[0], surrogate_only=False)


def test_full_search_control_recording_codec_is_canonical_and_strict() -> None:
    templates = qin_edge_pilot_template_pair_v0_1(SAMPLE_RATE_HZ, StarlinkEdge.LOWER)
    values = synthesize_starlink_injection_v0_2(templates, _case())
    recording_digest = Digest.sha256(b"control-codec-recording")
    suite = _analyzer().analyze_full_search_control(
        values,
        recording_id=RecordingId("rec_control_codec"),
        recording_identity_digest=recording_digest,
        segment_id=SegmentId("seg_control_codec"),
        receiver_chain_id=ReceiverChainId("rx_control_codec"),
        templates=templates,
    )
    bundle = StarlinkFullSearchControlRecordingBundleV0_1(
        SchemaRef(
            StarlinkFullSearchControlRecordingBundleV0_1.SCHEMA_ID,
            FULL_SEARCH_CONTROL_V0_1,
        ),
        "slsctrlrec_0123456789abcdef0123456789abcdef",
        suite.recording_id,
        recording_digest,
        Digest.sha256(b"source-request"),
        StarlinkFullSearchControlRecordingState.CANDIDATES,
        (suite,),
        (
            "surrogate-control-only",
            "not-an-empirical-null-distribution",
        ),
    )

    payload = encode_full_search_control_bundle(bundle)
    assert decode_full_search_control_bundle(payload) == bundle
    with pytest.raises(MalformedFullSearchControlBundleError):
        decode_full_search_control_bundle(payload + b"\n")


@pytest.mark.parametrize("edge", tuple(StarlinkEdge))
def test_scores_match_frozen_leo_tracker_numerical_oracle(edge: StarlinkEdge) -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures"
            / "starlink_detector_suite_oracle_v0_2.json"
        ).read_text()
    )
    templates = qin_edge_pilot_template_pair_v0_1(fixture["sample_rate_hz"], edge)
    values = synthesize_starlink_injection_v0_2(templates, _case())
    analyzer = StarlinkDetectorSuiteV0_2(
        StarlinkDetectorSuiteConfigV0_2((3,), (1_000.0,)), execution_context()
    )
    bundle = analyzer.analyze_receiver(
        values,
        recording_id=RecordingId("rec_oracle"),
        recording_identity_digest=Digest.sha256(b"oracle"),
        segment_id=SegmentId("seg_oracle"),
        receiver_chain_id=ReceiverChainId("rx_oracle"),
        templates=templates,
    )

    for method in bundle.methods:
        exact, control, support = fixture["edges"][edge.value][method.method.value]
        assert method.reported_score == pytest.approx(exact, abs=2e-12)
        assert method.conditioned_control_score == pytest.approx(control, abs=2e-12)
        assert method.exact_frames.support == support


def test_runtime_module_never_imports_the_leo_tracker_oracle() -> None:
    source = (
        Path(__file__).parents[2]
        / "src"
        / "leo_flow"
        / "analysis"
        / "recording"
        / "starlink_detector_suite.py"
    ).read_text(encoding="utf-8")
    assert "from leo_tracker" not in source
    assert "import leo_tracker" not in source
