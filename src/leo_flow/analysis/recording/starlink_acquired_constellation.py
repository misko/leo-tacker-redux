"""Additive QAM composition for the v0.3 multi-basin winner."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from leo_flow.contracts.core import ArtifactRef, Provenance, SchemaRef, canonical_digest
from leo_flow.contracts.starlink_acquired_constellation import (
    StarlinkAcquiredPilotConstellationEvidenceV0_3,
    StarlinkCalibrationState,
    StarlinkRevisedSearchCalibrationIdentityV0_3,
)
from leo_flow.contracts.starlink_acquisition import V0_3, StarlinkAcquisitionBundleV0_3
from leo_flow.contracts.starlink_detector_suite import (
    StarlinkDetectorMethod,
    StarlinkDetectorSuiteBundleV0_2,
    StarlinkFrameScoreSummaryV0_2,
)

from .api import AnalysisExecutionContext
from .starlink import FRAME_RATE_HZ
from .starlink_acquisition import StarlinkAcquisitionConfigV0_3
from .starlink_pilot_constellation import (
    StarlinkPilotConstellationAnalyzerV0_1,
    StarlinkPilotConstellationConfigV0_1,
    starlink_pilot_constellation_algorithm_ref_v0_1,
    starlink_pilot_constellation_config_ref_v0_1,
)


def revised_search_calibration_identity_v0_3(
    acquisition: StarlinkAcquisitionBundleV0_3,
    config: StarlinkAcquisitionConfigV0_3,
    *,
    time_window_count: int = 1,
) -> StarlinkRevisedSearchCalibrationIdentityV0_3:
    """Name the complete maximum; deliberately provide no threshold."""
    epoch_count = round(acquisition.sample_rate_hz / FRAME_RATE_HZ)
    cfo_count = len(config.coarse_cfo_hypotheses_hz)
    return StarlinkRevisedSearchCalibrationIdentityV0_3(
        SchemaRef(StarlinkRevisedSearchCalibrationIdentityV0_3.SCHEMA_ID, V0_3),
        acquisition.algorithm_ref,
        acquisition.config_ref,
        acquisition.exact_template_ref,
        acquisition.conditioned_control_template_ref,
        time_window_count,
        epoch_count,
        cfo_count,
        time_window_count * epoch_count * cfo_count,
        time_window_count * acquisition.refinement_search_cell_count,
        acquisition.acquire_symbol_indices,
        acquisition.verify_symbol_indices,
        StarlinkCalibrationState.BLOCKED_PENDING_WHOLE_REVISED_SEARCH,
        None,
    )


def starlink_acquired_constellation_algorithm_ref_v0_3() -> ArtifactRef:
    return ArtifactRef(
        "starlink-acquired-pilot-constellation-v0.3",
        canonical_digest(
            {
                "selection": "v0.3-multibasin-held-out-winner",
                "demodulator": str(
                    starlink_pilot_constellation_algorithm_ref_v0_1().digest
                ),
                "decision": "none-before-whole-revised-search-calibration",
            }
        ),
        SchemaRef("org.leo-flow.recording-algorithm", V0_3),
    )


class StarlinkAcquiredPilotConstellationAnalyzerV0_3:
    """Demodulate at v0.3's winner without altering v0.1 or suite v0.2."""

    def __init__(
        self,
        acquisition_config: StarlinkAcquisitionConfigV0_3,
        constellation_config: StarlinkPilotConstellationConfigV0_1,
        execution: AnalysisExecutionContext,
    ) -> None:
        self._acquisition_config = acquisition_config
        self._constellation_config = constellation_config
        self._execution = execution
        self._legacy_demodulator = StarlinkPilotConstellationAnalyzerV0_1(
            constellation_config, execution
        )

    def analyze(
        self,
        samples: Sequence[complex],
        suite: StarlinkDetectorSuiteBundleV0_2,
        acquisition: StarlinkAcquisitionBundleV0_3,
        *,
        time_window_count: int = 1,
    ) -> StarlinkAcquiredPilotConstellationEvidenceV0_3:
        self._validate_sources(samples, suite, acquisition)
        winner = acquisition.winner
        acquire = next(
            item
            for item in suite.methods
            if item.method is StarlinkDetectorMethod.FULL_FRAME_ACQUIRE
        )
        support = winner.frame_support
        exact_frames = StarlinkFrameScoreSummaryV0_2(
            winner.acquire_score, winner.acquire_score, support
        )
        control_frames = StarlinkFrameScoreSummaryV0_2(
            winner.conditioned_control_score,
            winner.conditioned_control_score,
            support,
        )
        selected = replace(
            acquire,
            algorithm_ref=acquisition.algorithm_ref,
            config_ref=acquisition.config_ref,
            search_identity_digest=acquisition.search_identity_digest,
            effective_search_cell_count=(
                acquisition.coarse_search_cell_count
                + acquisition.refinement_search_cell_count
            ),
            winning_epoch_sample=winner.refined_epoch_sample,
            winning_coarse_cfo_hz=winner.refined_cfo_hz,
            winning_residual_cfo_hz=0.0,
            reported_score=winner.acquire_score,
            conditioned_exact_score=winner.acquire_score,
            conditioned_control_score=winner.conditioned_control_score,
            exact_minus_control_margin=(
                winner.acquire_score - winner.conditioned_control_score
            ),
            exact_frames=exact_frames,
            control_frames=control_frames,
            reason_codes=tuple(
                sorted(
                    set(acquire.reason_codes)
                    | {"whole-revised-search-calibration-required"}
                )
            ),
        )
        methods = tuple(selected if item is acquire else item for item in suite.methods)
        numerical_suite = replace(suite, methods=methods)
        numerical = self._legacy_demodulator.analyze(samples, numerical_suite)
        calibration = revised_search_calibration_identity_v0_3(
            acquisition, self._acquisition_config, time_window_count=time_window_count
        )
        algorithm_ref = starlink_acquired_constellation_algorithm_ref_v0_3()
        constellation_config_ref = starlink_pilot_constellation_config_ref_v0_1(
            self._constellation_config
        )
        provenance = Provenance(
            self._execution.producer_name,
            self._execution.producer_version,
            self._execution.git_commit,
            self._execution.environment_digest,
            constellation_config_ref.digest,
            (suite.recording_identity_digest, suite.digest, acquisition.digest),
            (
                algorithm_ref.digest,
                acquisition.algorithm_ref.digest,
                acquisition.config_ref.digest,
                acquisition.exact_template_ref.digest,
                acquisition.conditioned_control_template_ref.digest,
                calibration.digest,
            ),
            self._execution.started_utc_ns,
            self._execution.completed_utc_ns,
            self._execution.host_class,
        )
        identity = canonical_digest(
            {
                "source_suite_digest": str(suite.digest),
                "source_acquisition_digest": str(acquisition.digest),
                "calibration_identity_digest": str(calibration.digest),
                "algorithm_digest": str(algorithm_ref.digest),
                "config_digest": str(constellation_config_ref.digest),
            }
        )
        return StarlinkAcquiredPilotConstellationEvidenceV0_3(
            SchemaRef(StarlinkAcquiredPilotConstellationEvidenceV0_3.SCHEMA_ID, V0_3),
            f"slqam3_{identity.value[:32]}",
            suite.recording_id,
            suite.recording_identity_digest,
            suite.segment_id,
            suite.receiver_chain_id,
            suite.edge,
            suite.sample_rate_hz,
            suite.probe_sample_count,
            suite.ref,
            acquisition.ref,
            acquisition.search_identity_digest,
            calibration.digest,
            acquisition.winning_candidate_rank,
            winner.refined_epoch_sample,
            winner.refined_cfo_hz,
            winner.verify_score,
            winner.conditioned_control_score,
            winner.verify_minus_control_margin,
            algorithm_ref,
            constellation_config_ref,
            numerical.residual_cfo_refinement_hz,
            numerical.complete_frame_count,
            numerical.effective_frame_count,
            numerical.hard_symbol_accuracy,
            numerical.rms_evm,
            numerical.model_snr_db,
            numerical.subcarriers,
            numerical.points,
            provenance,
            True,
            None,
            (
                "candidate-evidence-not-calibrated-detection",
                "whole-revised-search-calibration-required",
                "conditioned-on-v0.3-multibasin-winner",
                "published-edge-pilot-not-user-payload",
            ),
        )

    @staticmethod
    def _validate_sources(
        samples: Sequence[complex],
        suite: StarlinkDetectorSuiteBundleV0_2,
        acquisition: StarlinkAcquisitionBundleV0_3,
    ) -> None:
        if (
            len(samples) != suite.probe_sample_count
            or len(samples) != acquisition.probe_sample_count
        ):
            raise ValueError("suite, acquisition, and sample interval differ")
        suite_key = (
            suite.recording_id,
            suite.recording_identity_digest,
            suite.segment_id,
            suite.receiver_chain_id,
            suite.edge,
            suite.sample_rate_hz,
        )
        acquisition_key = (
            acquisition.recording_id,
            acquisition.recording_identity_digest,
            acquisition.segment_id,
            acquisition.receiver_chain_id,
            acquisition.edge,
            acquisition.sample_rate_hz,
        )
        if suite_key != acquisition_key:
            raise ValueError("suite and acquisition identify different streams")
        acquire = next(
            (
                item
                for item in suite.methods
                if item.method is StarlinkDetectorMethod.FULL_FRAME_ACQUIRE
            ),
            None,
        )
        if (
            acquire is None
            or acquire.exact_template_ref != acquisition.exact_template_ref
        ):
            raise ValueError("suite and acquisition bind different exact templates")
        if (
            acquire.conditioned_control_template_ref
            != acquisition.conditioned_control_template_ref
        ):
            raise ValueError("suite and acquisition bind different controls")
