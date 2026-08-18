from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.analysis.dataset.starlink_adaptive_calibration_input import (
    adaptive_calibration_pattern_templates_v0_1,
    adaptive_calibration_search_identity_v0_1,
    assemble_adaptive_calibration_input_v0_1,
    conditioned_positive_plumbing_v0_1,
)
from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    starlink_search_grid_v0_1,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    Provenance,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.starlink_adaptive_calibration import (
    AdaptiveCalibrationLabel,
    AdaptiveCalibrationSplit,
)
from leo_flow.contracts.starlink_adaptive_calibration_input import (
    AdaptiveCalibrationAssemblySpecV0_1,
    AdaptiveCalibrationEvidencePurpose,
)
from leo_flow.contracts.starlink_adaptive_refinement import (
    AdaptiveWindowStage,
    StarlinkAdaptiveBaseWindowV0_1,
    StarlinkAdaptiveExactWindowV0_1,
    StarlinkAdaptiveRefinementPlanV0_1,
    StarlinkAdaptiveRefinementSelectionV0_1,
)
from leo_flow.contracts.starlink_adaptive_response import (
    StarlinkAdaptiveResponseBundleV0_1,
    StarlinkAdaptiveResponsePointV0_1,
    StarlinkAdaptiveResponseStreamV0_1,
)
from leo_flow.contracts.starlink_detector_suite import (
    REPORT_METHOD_ORDER,
    StarlinkDetectorMethod,
)
from leo_flow.contracts.starlink_full_dwell_response import (
    StarlinkFullDwellSurrogateV0_1,
    StarlinkFullDwellWinnerV0_1,
)
from leo_flow.contracts.starlink_surrogate_null import StarlinkPatternSearchMode
from tests.recording_analysis.test_starlink_adaptive_qam_persistence import (
    _adaptive_prepared,
)


def _digest(value: str) -> Digest:
    return Digest.sha256(value.encode())


def _winner(score: float, start: int) -> StarlinkFullDwellWinnerV0_1:
    return StarlinkFullDwellWinnerV0_1(
        score,
        3,
        start + 3,
        2_000.0,
        -50.0,
        24,
        StarlinkPatternSearchMode.SEARCHED,
    )


def _response_from_qam(qam, *, scores: tuple[float, ...] = (0.0, 0.8)):
    plan = StarlinkAdaptiveRefinementPlanV0_1(100, 100, 100, 100, 1, 1, 16, 16)
    streams = []
    for stream_index, selected in enumerate(qam.stream_selections):
        starts = tuple(item.source_start_sample for item in selected.windows)
        windows = tuple(
            StarlinkAdaptiveExactWindowV0_1(
                index,
                AdaptiveWindowStage.BASE,
                start,
                start + 100,
                ("fixed-sentinel",),
                (),
            )
            for index, start in enumerate(starts)
        )
        pattern_refs = tuple(
            sorted(
                (
                    ArtifactRef("qin", qam.source_suite_ref.digest),
                    ArtifactRef("surrogate-0", _digest("surrogate-0")),
                    ArtifactRef("surrogate-1", _digest("surrogate-1")),
                ),
                key=lambda item: str(item.digest),
            )
        )
        selection = StarlinkAdaptiveRefinementSelectionV0_1(
            SchemaRef(StarlinkAdaptiveRefinementSelectionV0_1.SCHEMA_ID, V0_1),
            selected.segment_sample_count,
            plan,
            pattern_refs,
            tuple(
                StarlinkAdaptiveBaseWindowV0_1(
                    window.start_sample,
                    window.stop_sample,
                    ("fixed-sentinel",),
                    (),
                )
                for window in windows
            ),
            windows,
            (
                "candidate-evidence-not-calibrated-detection",
                "base-sentinels-span-dwell-but-do-not-cover-every-sample",
                "power-seeds-are-pattern-blind",
                "local-follow-up-uses-equal-quota-for-qin-and-surrogates",
                "all-patterns-search-the-union-of-selected-local-windows",
                "time-look-elsewhere-calibration-required",
            ),
        )
        points = []
        for window_index, start in enumerate(starts):
            score = scores[min(window_index, len(scores) - 1)]
            for method in REPORT_METHOD_ORDER:
                qin = _winner(score, start)
                surrogates = (
                    StarlinkFullDwellSurrogateV0_1(
                        0, _digest("surrogate-0"), _winner(score / 2, start)
                    ),
                    StarlinkFullDwellSurrogateV0_1(
                        1, _digest("surrogate-1"), _winner(score / 4, start)
                    ),
                )
                points.append(
                    StarlinkAdaptiveResponsePointV0_1(
                        method,
                        window_index,
                        start,
                        start + 100,
                        UtcNs(1_000_000_000 + start),
                        UtcNs(1_000_000_100 + start),
                        qin,
                        surrogates,
                        1 + sum(item.winner.score >= score for item in surrogates),
                        score - max(item.winner.score for item in surrogates),
                    )
                )
        streams.append(
            StarlinkAdaptiveResponseStreamV0_1(
                selected.radio_id,
                selected.lnb_id,
                selected.segment_id,
                selected.receiver_chain_id,
                selected.channel_number,
                selected.edge,
                selected.sample_rate_hz,
                selected.segment_sample_count,
                selection,
                tuple(points),
                len(starts) * 100,
                len(starts) * 100 / selected.segment_sample_count,
            )
        )
    return StarlinkAdaptiveResponseBundleV0_1(
        SchemaRef(StarlinkAdaptiveResponseBundleV0_1.SCHEMA_ID, V0_1),
        "slar_fixture",
        qam.recording_id,
        qam.recording_identity_digest,
        ArtifactRef("timeline", _digest("timeline")),
        qam.source_suite_ref,
        _digest("response-request"),
        starlink_search_grid_v0_1(
            StarlinkDetectorSuiteConfigV0_2((0,), (0.0,), (0.0,))
        ),
        plan,
        tuple(sorted(streams, key=lambda item: item.identity)),
        Provenance(
            "fixture",
            "1",
            "commit",
            _digest("env"),
            _digest("config"),
            (_digest("input"),),
            (),
            UtcNs(1),
            UtcNs(2),
            "test",
        ),
        (
            "candidate-evidence-not-calibrated-detection",
            "finite-surrogate-rank-not-p-value",
            "time-look-elsewhere-calibration-required",
            "base-sentinels-span-dwell-but-do-not-cover-every-sample",
            "all-patterns-search-the-union-of-selected-local-windows",
            "exact-window-union-is-sparse-and-dependent",
        ),
        None,
    )


def _spec(response, qam=None, *, label=AdaptiveCalibrationLabel.POSITIVE):
    method = StarlinkDetectorMethod.GLRT_32
    return AdaptiveCalibrationAssemblySpecV0_1(
        SchemaRef(AdaptiveCalibrationAssemblySpecV0_1.SCHEMA_ID, V0_1),
        "dwell-fixture",
        _digest("member"),
        _digest("group"),
        AdaptiveCalibrationSplit.VALIDATION,
        _digest("validation-manifest"),
        label,
        _digest("cell"),
        method,
        response.digest,
        qam.digest if qam is not None else None,
        adaptive_calibration_search_identity_v0_1(response, method),
        tuple(
            sorted(
                (str(item.radio_id), str(item.receiver_chain_id))
                for item in response.streams
            )
        ),
        adaptive_calibration_pattern_templates_v0_1(response, method),
        AdaptiveCalibrationEvidencePurpose.CALIBRATION,
    )


def test_assembly_takes_complete_declared_time_cfo_epoch_maxima_and_retains_zeroes():
    _, qam = _adaptive_prepared()
    response = _response_from_qam(qam, scores=(0.0, 0.8))
    assembled = assemble_adaptive_calibration_input_v0_1(_spec(response), response)

    spec = _spec(response)
    assert assembled.assembly_spec_digest == spec.digest
    assert assembled.split_manifest_digest == spec.split_manifest_digest
    assert assembled.dwell.member_digest == spec.member_digest
    assert assembled.dwell.group_digest == spec.group_digest
    assert assembled.dwell.cell_identity_digest == spec.cell_identity_digest
    assert all(
        replace(spec, **{field: _digest(f"changed-{field}")}).digest != spec.digest
        for field in (
            "split_manifest_digest",
            "member_digest",
            "group_digest",
            "cell_identity_digest",
        )
    )
    assert assembled.complete_search_axes == (
        "declared-time-windows",
        "coarse-cfo",
        "residual-cfo",
        "epoch",
    )
    assert all(
        receiver.whole_search_maximum == pytest.approx(expected)
        and receiver.candidate_count == 1
        for pattern, expected in zip(assembled.dwell.patterns, (0.8, 0.4, 0.2))
        for receiver in pattern.receiver_evidence
    )

    zero_response = _response_from_qam(qam, scores=(0.0, 0.0))
    zero = assemble_adaptive_calibration_input_v0_1(
        _spec(zero_response, label=AdaptiveCalibrationLabel.NULL), zero_response
    )
    assert all(
        receiver.candidate_count == 0 and receiver.whole_search_maximum == 0
        for pattern in zero.dwell.patterns
        for receiver in pattern.receiver_evidence
    )


def test_qam_closes_over_response_and_attaches_only_to_qin_pattern():
    _, original_qam = _adaptive_prepared()
    response = _response_from_qam(original_qam)
    qam = replace(
        original_qam,
        source_adaptive_response_ref=ArtifactRef(
            response.analysis_id,
            response.digest,
            SchemaRef(StarlinkAdaptiveResponseBundleV0_1.SCHEMA_ID, V0_1),
        ),
    )
    assembled = assemble_adaptive_calibration_input_v0_1(
        _spec(response, qam), response, qam
    )
    assert all(
        item.qam_complete_frame_count > 0 and item.qam_goodness > 0
        for item in assembled.dwell.patterns[0].receiver_evidence
    )
    assert all(
        item.qam_complete_frame_count == 0 and item.qam_goodness == 0
        for pattern in assembled.dwell.patterns[1:]
        for item in pattern.receiver_evidence
    )
    with pytest.raises(ValueError, match="pattern-symmetric QAM"):
        assemble_adaptive_calibration_input_v0_1(
            _spec(response, qam, label=AdaptiveCalibrationLabel.NULL), response, qam
        )


def test_membership_search_and_label_correction_fail_closed():
    _, qam = _adaptive_prepared()
    response = _response_from_qam(qam)
    spec = _spec(response)
    with pytest.raises(ValueError, match="pattern bank"):
        assemble_adaptive_calibration_input_v0_1(
            replace(spec, pattern_template_digests=tuple(reversed(spec.pattern_template_digests))),
            response,
        )
    with pytest.raises(ValueError, match="label-derived"):
        replace(spec, score_correction="lnb-c-offset")

    relabelled = replace(
        response,
        streams=tuple(replace(item, lnb_id="arbitrary-label") for item in response.streams),
    )
    assert adaptive_calibration_search_identity_v0_1(
        relabelled, spec.method
    ) == adaptive_calibration_search_identity_v0_1(response, spec.method)


def test_retro_fixture_is_conditioned_positive_plumbing_not_calibration():
    fixture = json.loads(
        Path("tests/recording_analysis/fixtures/retro_qam_2026_08_17_v1.json").read_text()
    )
    result = conditioned_positive_plumbing_v0_1(
        fixture_id=fixture["fixture_id"],
        fixture_digest=Digest.sha256(
            Path("tests/recording_analysis/fixtures/retro_qam_2026_08_17_v1.json").read_bytes()
        ),
        receiver_accuracy_and_evm=tuple(
            (item["hard_symbol_accuracy"], item["rms_evm"])
            for item in fixture["historical_conditioned_expectations"]
        ),
    )
    assert min(result.receiver_qam_goodness) > 0.7
    assert not result.eligible_for_calibration
    assert result.purpose is AdaptiveCalibrationEvidencePurpose.CONDITIONED_POSITIVE
