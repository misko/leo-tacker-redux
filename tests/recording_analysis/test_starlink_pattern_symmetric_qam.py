from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from leo_flow.analysis.dataset.starlink_adaptive_calibration_input import (
    assemble_pattern_symmetric_adaptive_calibration_input_v0_1,
)
from leo_flow.analysis.recording.starlink_pattern_symmetric_qam import (
    PatternSymmetricAdaptiveQamAnalyzerV0_5,
    known_pattern_qam_quality_v0_5,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    conditioned_pattern_control_v0_1,
    precommitted_surrogate_states_v0_1,
    qin_exact_search_pattern_v0_1,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import Digest
from leo_flow.contracts.starlink_adaptive_calibration import AdaptiveCalibrationLabel
from leo_flow.contracts.starlink_pattern_symmetric_qam import (
    PatternSymmetricQamPolicyV0_5,
)
from tests.dataset_analysis.test_starlink_adaptive_calibration_input import _spec
from tests.recording_analysis.test_starlink_adaptive_response import (
    adaptive_response_result,
)


class _FakeAcquisition:
    def __init__(self) -> None:
        self.calls = []

    def analyze_receiver(self, samples, **kwargs):
        templates = kwargs["templates"]
        self.calls.append(
            (
                templates.exact_ref.digest,
                templates.conditioned_control_ref.digest,
                len(samples),
            )
        )
        return SimpleNamespace(
            search_identity_digest=Digest.sha256(
                f"{templates.exact_ref.digest}:{templates.conditioned_control_ref.digest}".encode()
            ),
            algorithm_ref=SimpleNamespace(digest=Digest.sha256(b"algorithm")),
            config_ref=SimpleNamespace(digest=Digest.sha256(b"config")),
            coarse_search_cell_count=24,
            refinement_search_cell_count=16,
            winner=SimpleNamespace(refined_epoch_sample=0, refined_cfo_hz=0.0),
        )


class _ZeroReader:
    def read_window(self, stream, start_sample, stop_sample):
        del stream
        return (0j,) * (stop_sample - start_sample)


def _response():
    return adaptive_response_result.__wrapped__()[2]


def test_every_pattern_runs_identical_data_independent_windows_and_search_shape():
    response = _response()
    acquisition = _FakeAcquisition()
    policy = PatternSymmetricQamPolicyV0_5(
        qam_window_sample_count=7_500,
        maximum_windows_per_stream=2,
        maximum_patterns=5,
        maximum_receivers=1,
        maximum_acquisition_runs=10,
    )
    bundle = PatternSymmetricAdaptiveQamAnalyzerV0_5(  # type: ignore[arg-type]
        acquisition, policy
    ).analyze(response, _ZeroReader())

    assert bundle.acquisition_run_count == 10
    assert len(acquisition.calls) == 10
    stream = bundle.streams[0]
    assert tuple(item.pattern_index for item in stream.patterns) == tuple(range(5))
    assert all(
        item.control_template_digest != item.template_digest for item in stream.patterns
    )
    memberships = {
        tuple((item.start_sample, item.stop_sample) for item in pattern.windows)
        for pattern in stream.patterns
    }
    assert len(memberships) == 1
    assert all(
        item.complete_frame_count > 0
        for pattern in stream.patterns
        for item in pattern.windows
    )
    assert all(exact != control for exact, control, _ in acquisition.calls)
    assert bundle.calibrated_detection_count is None


def test_window_selection_is_invariant_to_qin_and_surrogate_scores():
    response = _response()
    changed_streams = []
    for stream in response.streams:
        changed_points = tuple(
            replace(
                point,
                qin=replace(point.qin, score=1 - point.qin.score),
                finite_upper_tail_rank=(
                    1
                    + sum(
                        item.winner.score >= 1 - point.qin.score
                        for item in point.surrogates
                    )
                ),
                qin_minus_max_surrogate=(
                    1
                    - point.qin.score
                    - max(item.winner.score for item in point.surrogates)
                ),
            )
            for point in stream.points
        )
        changed_streams.append(replace(stream, points=changed_points))
    changed = replace(response, streams=tuple(changed_streams))
    policy = PatternSymmetricQamPolicyV0_5(7_500, 2, 5, 1, 10)
    left = PatternSymmetricAdaptiveQamAnalyzerV0_5(  # type: ignore[arg-type]
        _FakeAcquisition(), policy
    ).analyze(response, _ZeroReader())
    right = PatternSymmetricAdaptiveQamAnalyzerV0_5(  # type: ignore[arg-type]
        _FakeAcquisition(), policy
    ).analyze(changed, _ZeroReader())
    assert tuple(
        (item.start_sample, item.stop_sample)
        for item in left.streams[0].patterns[0].windows
    ) == tuple(
        (item.start_sample, item.stop_sample)
        for item in right.streams[0].patterns[0].windows
    )


def test_bounded_policy_rejects_unbudgeted_full_pattern_bank():
    response = _response()
    with pytest.raises(ValueError, match="pattern count"):
        PatternSymmetricAdaptiveQamAnalyzerV0_5(  # type: ignore[arg-type]
            _FakeAcquisition(),
            PatternSymmetricQamPolicyV0_5(7_500, 1, 4, 1, 4),
        ).analyze(response, _ZeroReader())


def test_known_surrogate_quality_is_finite_and_has_complete_support():
    accuracy, evm, support = known_pattern_qam_quality_v0_5(
        (0j,) * 7_500,
        2_500_000.0,
        _response().streams[0].edge,
        precommitted_surrogate_states_v0_1(0),
        0,
        0.0,
    )
    assert 0 <= accuracy <= 1
    assert evm >= 0
    assert support == 2


def test_generic_qin_roll_control_matches_frozen_qin_control_numerics():
    response = _response()
    stream = response.streams[0]
    pattern = qin_exact_search_pattern_v0_1(stream.sample_rate_hz, stream.edge)
    generic = conditioned_pattern_control_v0_1(pattern)
    canonical = qin_edge_pilot_template_pair_v0_1(stream.sample_rate_hz, stream.edge)
    assert generic.template_ref.digest == canonical.conditioned_control_ref.digest
    assert generic.samples == canonical.conditioned_control_samples


def test_retro_and_j1_are_disclosed_as_canaries_not_calibration_members():
    response = _response()
    policy = PatternSymmetricQamPolicyV0_5(7_500, 1, 5, 1, 5)
    bundle = PatternSymmetricAdaptiveQamAnalyzerV0_5(  # type: ignore[arg-type]
        _FakeAcquisition(), policy
    ).analyze(response, _ZeroReader())
    assert "retro-and-j1-are-conditioned-numerical-canaries-only" in bundle.warnings
    assert bundle.candidate_only


def test_null_calibration_receives_qam_for_qin_and_every_surrogate():
    response = _response()
    policy = PatternSymmetricQamPolicyV0_5(7_500, 1, 5, 1, 5)
    qam = PatternSymmetricAdaptiveQamAnalyzerV0_5(  # type: ignore[arg-type]
        _FakeAcquisition(), policy
    ).analyze(response, _ZeroReader())
    spec = _spec(response, qam, label=AdaptiveCalibrationLabel.NULL)
    assembled = assemble_pattern_symmetric_adaptive_calibration_input_v0_1(
        spec, response, qam
    )
    assert all(
        receiver.qam_complete_frame_count > 0
        for pattern in assembled.dwell.patterns
        for receiver in pattern.receiver_evidence
    )
