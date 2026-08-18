from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

from leo_flow.adapters.dashboard_doppler_projection import (
    DurableDashboardDopplerProjectionV0_1,
)
from leo_flow.analysis.recording.waterfall_doppler_pipeline import (
    AdvancedBlindDopplerAnalyzerV0_1,
)
from leo_flow.analysis.recording.waterfall_v0_2_persistence import (
    DurableWaterfallViewV0_2,
)
from leo_flow.analysis.tracking.doppler_persistence import DurableDopplerViewV0_1
from leo_flow.contracts.core import Digest, ReceiverChainId, RecordingId, SegmentId
from leo_flow.contracts.dashboard_doppler import (
    DopplerCandidateAssociationState,
    DopplerVisualizationState,
    DopplerWaterfallLayer,
    RecordingDopplerVisualizationViewV0_1,
)
from leo_flow.contracts.doppler_evidence import (
    CandidatePathAssociationV0_1,
    DopplerAnalysisId,
    DopplerAnalysisRefV0_1,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.contracts.waterfall import WaterfallProductId
from leo_flow.contracts.waterfall_v0_2 import WaterfallProductRefV0_2
from tests.recording_analysis.test_waterfall_doppler_pipeline import _basic, _bundle


def _object(name: str) -> ObjectRef:
    digest = Digest.sha256(name.encode())
    return ObjectRef(
        digest,
        1,
        "application/json",
        name,
        f"cas:sha256:{digest.value}",
    )


class _Query:
    def __init__(self, refs=()):
        self.refs = refs

    def list_recording_doppler(self, recording_id):
        del recording_id
        return self.refs


class _Waterfalls:
    def __init__(self, view):
        self.view = view

    @contextmanager
    def open(self, product_id):
        assert product_id == str(self.view.ref.product_id)
        yield self.view


class _Dopplers:
    def __init__(self, view):
        self.view = view

    @contextmanager
    def open(self, recording_id, doppler_id):
        assert recording_id == self.view.ref.recording_id
        assert doppler_id == self.view.ref.doppler_id
        yield self.view


def test_missing_doppler_product_is_explicitly_unavailable() -> None:
    projection = DurableDashboardDopplerProjectionV0_1(
        _Query(),
        None,
        None,  # type: ignore[arg-type]
    )

    result = projection.recording_doppler_visualization(
        RecordingId("rec_missing"), DopplerWaterfallLayer.RESIDUAL
    )

    assert result.state is DopplerVisualizationState.UNAVAILABLE
    assert result.reason_codes == ("doppler-analysis-unavailable",)
    assert result.warnings == (RecordingDopplerVisualizationViewV0_1.CANDIDATE_WARNING,)


def test_durable_products_map_to_one_bounded_candidate_only_view() -> None:
    waterfall = _bundle()
    spectrogram, basic = _basic(waterfall)
    advanced = AdvancedBlindDopplerAnalyzerV0_1().analyze(spectrogram, basic)
    waterfall_object = _object("waterfall-v0.2")
    basic_object = _object("basic-doppler")
    advanced_object = _object("advanced-doppler")
    ref = DopplerAnalysisRefV0_1(
        DopplerAnalysisId("doppler_" + "1" * 32),
        waterfall.recording_id,
        waterfall.product_id,
        waterfall_object.digest,
        SegmentId("seg_ch4_lower"),
        ReceiverChainId("rx_first"),
        spectrogram.input_identity_digest,
        basic.config_digest,
        advanced.config_digest,
        basic_object,
        advanced_object,
        len(basic.candidates),
        len(basic.candidates),
        basic.candidates[0].ranking_score,
    )
    waterfall_ref = WaterfallProductRefV0_2(
        WaterfallProductId(str(waterfall.product_id)),
        waterfall.analysis_run_id,
        waterfall.recording_id,
        waterfall_object,
    )
    projection = DurableDashboardDopplerProjectionV0_1(
        _Query((ref,)),
        _Waterfalls(DurableWaterfallViewV0_2(waterfall_ref, waterfall)),
        _Dopplers(DurableDopplerViewV0_1(ref, basic, advanced)),
    )

    result = projection.recording_doppler_visualization(
        waterfall.recording_id, DopplerWaterfallLayer.RESIDUAL
    )

    assert result.state is DopplerVisualizationState.COMPLETE
    assert result.candidate_only is True
    assert result.calibrated_detection_count is None
    assert len(result.tiles) == 1
    assert result.tiles[0].time_bins[0].power_db == (
        waterfall.tiles[0].time_bins[0].temporal_median_residual_db
    )
    assert len(result.candidates) == len(basic.candidates)
    assert result.candidates[0].mean_spectral_peak_excess_db == (
        basic.candidates[0].mean_spectral_peak_excess_db
    )
    assert result.doppler_provenance[0].segment_id == "seg_ch4_lower"
    assert result.advanced_evidence[0].candidate_rank == 1
    assert result.advanced_evidence[0].drift_rate_hz_s == (
        advanced.slope_bank.track.drift_rate_hz_s  # type: ignore[union-attr]
    )
    assert result.advanced_evidence[0].spectral_peak_excess_reference == (
        advanced.spectral_peak_excess_reference
    )
    assert result.advanced_evidence[0].source_input_digest == (
        advanced.slope_bank.source_input_digest  # type: ignore[union-attr]
    )
    assert result.advanced_evidence[0].candidate_path_digest == (
        advanced.slope_bank.candidate_path_digest  # type: ignore[union-attr]
    )
    assert result.advanced_evidence[0].association is not None
    assert (
        result.advanced_evidence[0].association.state
        is DopplerCandidateAssociationState.MATCHED_BASIC_CANDIDATE
    )


def test_advanced_only_path_and_zero_candidate_provenance_are_not_suppressed() -> None:
    waterfall = _bundle()
    spectrogram, populated_basic = _basic(waterfall)
    populated_advanced = AdvancedBlindDopplerAnalyzerV0_1().analyze(
        spectrogram, populated_basic
    )
    assert populated_advanced.slope_bank is not None
    advanced_path_digest = populated_advanced.slope_bank.candidate_path_digest
    basic = replace(populated_basic, candidates=())
    advanced = replace(
        populated_advanced,
        slope_bank=replace(
            populated_advanced.slope_bank,
            basic_candidate_rank=None,
        ),
        association=CandidatePathAssociationV0_1(
            "advanced-path-only",
            advanced_path_digest,
            None,
            0,
            0.0,
            None,
            None,
        ),
    )
    waterfall_object = _object("waterfall-v0.2-advanced-only")
    ref = DopplerAnalysisRefV0_1(
        DopplerAnalysisId("doppler_" + "2" * 32),
        waterfall.recording_id,
        waterfall.product_id,
        waterfall_object.digest,
        SegmentId("seg_ch4_lower"),
        ReceiverChainId("rx_first"),
        spectrogram.input_identity_digest,
        basic.config_digest,
        advanced.config_digest,
        _object("basic-doppler-advanced-only"),
        _object("advanced-doppler-advanced-only"),
        0,
        0,
        None,
    )
    waterfall_ref = WaterfallProductRefV0_2(
        WaterfallProductId(str(waterfall.product_id)),
        waterfall.analysis_run_id,
        waterfall.recording_id,
        waterfall_object,
    )
    projection = DurableDashboardDopplerProjectionV0_1(
        _Query((ref,)),
        _Waterfalls(DurableWaterfallViewV0_2(waterfall_ref, waterfall)),
        _Dopplers(DurableDopplerViewV0_1(ref, basic, advanced)),
    )

    result = projection.recording_doppler_visualization(
        waterfall.recording_id, DopplerWaterfallLayer.HIGH_PERCENTILE
    )

    assert result.candidates == ()
    assert len(result.doppler_provenance) == 1
    assert len(result.advanced_evidence) == 1
    evidence = result.advanced_evidence[0]
    assert evidence.candidate_rank is None
    assert evidence.association is not None
    assert (
        evidence.association.state
        is DopplerCandidateAssociationState.ADVANCED_PATH_ONLY
    )
    assert evidence.candidate_path_digest == advanced_path_digest

    paths = projection.recording_advanced_doppler_paths(waterfall.recording_id)
    assert len(paths) == 1
    path = paths[0]
    assert path.association_state == "advanced-path-only"
    assert path.path_digest == advanced_path_digest
    assert path.provenance_artifact_id == str(ref.doppler_id) + ":advanced"
    assert len(path.points) == len(waterfall.tiles[0].time_bins)
    first = path.points[0]
    first_row = waterfall.tiles[0].time_bins[0]
    first_bin = advanced.slope_bank.track.bins[0]  # type: ignore[union-attr]
    assert (first.start_sample, first.stop_sample) == (
        first_row.start_sample,
        first_row.stop_sample,
    )
    assert first.midpoint_utc_ns == first_row.midpoint_utc_ns
    assert first.frequency_hz == (
        waterfall.tiles[0].center_frequency_hz
        + waterfall.tiles[0].frequency_bin_offsets_hz[first_bin]
    )
