from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.analysis.recording.starlink_acquired_constellation import (
    StarlinkAcquiredPilotConstellationAnalyzerV0_3,
    revised_search_calibration_identity_v0_3,
)
from leo_flow.analysis.recording.starlink_acquisition import (
    StarlinkAcquisitionConfigV0_3,
    StarlinkAcquisitionV0_3,
    starlink_acquisition_algorithm_ref_v0_3,
    starlink_acquisition_config_ref_v0_3,
)
from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
    StarlinkInjectionCaseV0_2,
    synthesize_starlink_injection_v0_2,
)
from leo_flow.analysis.recording.starlink_pilot_constellation import (
    StarlinkPilotConstellationAnalyzerV0_1,
    StarlinkPilotConstellationConfigV0_1,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import Digest, ReceiverChainId, RecordingId, SegmentId
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_acquired_constellation import (
    StarlinkCalibrationState,
)
from leo_flow.services.starlink_acquired_constellation_analysis import (
    CombinedStarlinkSuiteAnalysisJobPreparerV0_3,
    StarlinkAcquisitionCompositionProfileV0_3,
)

from .fakes import execution_context

RATE = 2_500_000.0
COUNT = 14_000


def _products():
    templates = qin_edge_pilot_template_pair_v0_1(RATE, StarlinkEdge.LOWER)
    samples = synthesize_starlink_injection_v0_2(
        templates,
        StarlinkInjectionCaseV0_2(
            "acquired-qam",
            17,
            COUNT,
            2.0,
            0.1,
            37,
            240_000.0,
            0.0,
            (0, 1, 2, 3),
        ),
    )
    common = {
        "recording_id": RecordingId("rec_acquired_qam"),
        "recording_identity_digest": Digest.sha256(b"acquired-qam"),
        "segment_id": SegmentId("seg_acquired_qam"),
        "receiver_chain_id": ReceiverChainId("rx_acquired_qam"),
        "templates": templates,
    }
    acquisition_config = StarlinkAcquisitionConfigV0_3(
        "pluto-acquired-qam", retained_candidate_count=4
    )
    acquisition = StarlinkAcquisitionV0_3(
        acquisition_config, execution_context()
    ).analyze_receiver(samples, **common)
    suite = StarlinkDetectorSuiteV0_2(
        StarlinkDetectorSuiteConfigV0_2(
            (37,), (0.0,), (0.0,), maximum_probe_samples=COUNT
        ),
        execution_context(),
    ).analyze_receiver(samples, **common)
    return samples, suite, acquisition, acquisition_config


def test_v0_3_qam_binds_acquisition_without_mutating_v0_2_or_v0_1() -> None:
    samples, suite, acquisition, acquisition_config = _products()
    suite_digest_before = suite.digest
    legacy_config = StarlinkPilotConstellationConfigV0_1(maximum_probe_samples=COUNT)
    legacy_before = StarlinkPilotConstellationAnalyzerV0_1(
        legacy_config, execution_context()
    ).analyze(samples, suite)

    result = StarlinkAcquiredPilotConstellationAnalyzerV0_3(
        acquisition_config, legacy_config, execution_context()
    ).analyze(samples, suite, acquisition)

    legacy_after = StarlinkPilotConstellationAnalyzerV0_1(
        legacy_config, execution_context()
    ).analyze(samples, suite)
    assert suite.digest == suite_digest_before
    assert legacy_after == legacy_before
    assert result.source_suite_ref == suite.ref
    assert result.source_acquisition_ref == acquisition.ref
    assert result.winning_epoch_sample == 37
    assert result.winning_cfo_hz == pytest.approx(240_000.0, abs=75.0)
    assert result.calibrated_detection is None
    assert "whole-revised-search-calibration-required" in result.reason_codes
    assert result.provenance.input_digests[-1] == acquisition.digest


def test_calibration_identity_names_time_epoch_cfo_maximum_and_blocks_threshold() -> (
    None
):
    _samples, _suite, acquisition, config = _products()
    identity = revised_search_calibration_identity_v0_3(
        acquisition, config, time_window_count=8
    )
    assert identity.maximum_coarse_search_cells == (
        identity.time_window_count
        * identity.epoch_hypothesis_count
        * identity.coarse_cfo_hypothesis_count
    )
    assert (
        identity.threshold_state
        is StarlinkCalibrationState.BLOCKED_PENDING_WHOLE_REVISED_SEARCH
    )
    assert identity.calibrated_threshold is None
    with pytest.raises(ValueError, match="cannot publish a threshold"):
        replace(identity, calibrated_threshold=0.5)


def test_v0_3_qam_fails_closed_on_cross_stream_acquisition() -> None:
    samples, suite, acquisition, config = _products()
    other = replace(acquisition, receiver_chain_id=ReceiverChainId("rx_other"))
    with pytest.raises(ValueError, match="different streams"):
        StarlinkAcquiredPilotConstellationAnalyzerV0_3(
            config,
            StarlinkPilotConstellationConfigV0_1(maximum_probe_samples=COUNT),
            execution_context(),
        ).analyze(samples, suite, other)


def test_service_composition_rejects_ambiguous_receiver_profiles() -> None:
    _samples, suite, _acquisition, config = _products()
    qam_config = StarlinkPilotConstellationConfigV0_1(maximum_probe_samples=COUNT)
    profile = StarlinkAcquisitionCompositionProfileV0_3(
        suite.methods[0].config_ref,
        suite.receiver_chain_id,
        StarlinkAcquisitionV0_3(config, execution_context()),
        StarlinkAcquiredPilotConstellationAnalyzerV0_3(
            config, qam_config, execution_context()
        ),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        CombinedStarlinkSuiteAnalysisJobPreparerV0_3(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            (profile, profile),
        )


def test_science_inventory_pins_exact_profile_and_algorithm_digests() -> None:
    path = (
        Path(__file__).parents[2]
        / "benchmark"
        / "specs"
        / "starlink-acquisition-v0.3.json"
    )
    inventory = json.loads(path.read_text())
    assert inventory["algorithm_ref"]["digest"] == str(
        starlink_acquisition_algorithm_ref_v0_3().digest
    )
    profiles = {
        item["receiver_cfo_profile_id"]: item["config_digest"]
        for item in inventory["profiles"]
    }
    for profile_id, expected in profiles.items():
        assert (
            str(
                starlink_acquisition_config_ref_v0_3(
                    StarlinkAcquisitionConfigV0_3(profile_id)
                ).digest
            )
            == expected
        )
