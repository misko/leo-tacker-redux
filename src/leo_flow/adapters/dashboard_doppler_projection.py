"""Map durable waterfall/Doppler products into the bounded dashboard contract."""

from __future__ import annotations

from leo_flow.analysis.recording.waterfall_v0_2_persistence import (
    DurableWaterfallReaderV0_2,
)
from leo_flow.analysis.tracking.doppler_persistence import DurableDopplerReaderV0_1
from leo_flow.contracts.blind_doppler import (
    BlindDopplerCandidateV0_1,
    DopplerPolynomialOrder,
)
from leo_flow.contracts.core import RecordingId, SchemaRef, UtcNs
from leo_flow.contracts.dashboard_advanced_doppler import (
    PublishedAdvancedDopplerPathPointV0_1,
    PublishedAdvancedDopplerPathV0_1,
)
from leo_flow.contracts.dashboard_doppler import (
    DopplerAdvancedEvidenceViewV0_1,
    DopplerBroadbandEvidenceViewV0_1,
    DopplerCandidateAssociationState,
    DopplerCandidatePathAssociationViewV0_1,
    DopplerCandidateViewV0_1,
    DopplerCombEvidenceViewV0_1,
    DopplerDualReceiverEvidenceViewV0_1,
    DopplerOrbitAssociationViewV0_1,
    DopplerProductProvenanceViewV0_1,
    DopplerStationaryControlViewV0_1,
    DopplerTileProvenanceViewV0_1,
    DopplerTrackModel,
    DopplerTrackPointViewV0_1,
    DopplerVisualizationState,
    DopplerWaterfallCoverageViewV0_1,
    DopplerWaterfallLayer,
    DopplerWaterfallTileViewV0_1,
    DopplerWaterfallTimeBinViewV0_1,
    RecordingDopplerVisualizationViewV0_1,
)
from leo_flow.contracts.doppler_evidence import (
    AdvancedDopplerEvidenceBundleV0_1,
    DopplerAnalysisRefV0_1,
    RecordingDopplerAnalysisQueryPortV0_1,
)
from leo_flow.contracts.waterfall_v0_2 import WaterfallTileV0_2


class DurableDashboardDopplerProjectionV0_1:
    """Read exact durable products and expose DTOs without storage locators."""

    def __init__(
        self,
        query: RecordingDopplerAnalysisQueryPortV0_1,
        waterfalls: DurableWaterfallReaderV0_2,
        doppler: DurableDopplerReaderV0_1,
    ) -> None:
        self._query = query
        self._waterfalls = waterfalls
        self._doppler = doppler

    def recording_doppler_visualization(
        self, recording_id: RecordingId, layer: DopplerWaterfallLayer
    ) -> RecordingDopplerVisualizationViewV0_1:
        refs = self._query.list_recording_doppler(recording_id)
        if not refs:
            return _unavailable(recording_id, layer)
        product_ids = {str(ref.waterfall_product_id) for ref in refs}
        waterfall_digests = {ref.waterfall_bundle_digest for ref in refs}
        if len(product_ids) != 1 or len(waterfall_digests) != 1:
            raise RuntimeError(
                "recording Doppler products identify different waterfalls"
            )
        product_id = next(iter(product_ids))
        with self._waterfalls.open(product_id) as durable_waterfall:
            waterfall = durable_waterfall.bundle
            if (
                waterfall.recording_id != recording_id
                or durable_waterfall.ref.bundle_ref.digest
                != next(iter(waterfall_digests))
            ):
                raise RuntimeError("Doppler and waterfall products disagree")
            tiles = tuple(_tile(tile, layer) for tile in waterfall.tiles)
            candidates: list[DopplerCandidateViewV0_1] = []
            advanced: list[DopplerAdvancedEvidenceViewV0_1] = []
            provenance: list[DopplerTileProvenanceViewV0_1] = []
            for ref in refs:
                with self._doppler.open(recording_id, ref.doppler_id) as durable:
                    basic = durable.basic
                    evidence = durable.advanced
                    candidates.extend(
                        _candidate(ref, item) for item in basic.candidates
                    )
                    if evidence.slope_bank is not None:
                        advanced.append(_advanced(ref, evidence))
                    provenance.append(
                        DopplerTileProvenanceViewV0_1(
                            ref.segment_id,
                            ref.receiver_chain_id,
                            DopplerProductProvenanceViewV0_1(
                                "basic-blind-doppler",
                                str(ref.doppler_id) + ":basic",
                                basic.schema,
                                basic.input_identity_digest,
                                basic.algorithm_version,
                                basic.config_digest,
                            ),
                            DopplerProductProvenanceViewV0_1(
                                "advanced-blind-doppler",
                                str(ref.doppler_id) + ":advanced",
                                evidence.schema,
                                evidence.input_identity_digest,
                                evidence.algorithm_version,
                                evidence.config_digest,
                            ),
                        )
                    )
            return RecordingDopplerVisualizationViewV0_1(
                schema=SchemaRef(RecordingDopplerVisualizationViewV0_1.SCHEMA_ID),
                recording_id=recording_id,
                state=DopplerVisualizationState.COMPLETE,
                selected_layer=layer,
                candidate_only=True,
                calibrated_detection_count=None,
                waterfall_provenance=DopplerProductProvenanceViewV0_1(
                    "full-coverage-waterfall",
                    str(waterfall.product_id),
                    waterfall.schema,
                    waterfall.input_recording_identity_digest,
                    waterfall.provenance.producer_version,
                    waterfall.provenance.normalized_config_digest,
                    str(waterfall.analysis_run_id),
                    waterfall.provenance.producer_name,
                    waterfall.provenance.producer_version,
                    waterfall.provenance.git_commit,
                    waterfall.provenance.started_utc_ns,
                    waterfall.provenance.completed_utc_ns,
                ),
                doppler_provenance=tuple(provenance),
                tiles=tiles,
                candidates=tuple(candidates),
                advanced_evidence=tuple(advanced),
                warnings=tuple(
                    sorted(
                        {
                            RecordingDopplerVisualizationViewV0_1.CANDIDATE_WARNING,
                            *waterfall.warnings,
                        }
                    )
                ),
                reason_codes=tuple(sorted(set(waterfall.reason_codes))),
            )

    def recording_advanced_doppler_paths(
        self, recording_id: RecordingId
    ) -> tuple[PublishedAdvancedDopplerPathV0_1, ...]:
        """Expose exact immutable slope-bank points without fabricating candidates."""

        refs = self._query.list_recording_doppler(recording_id)
        if not refs:
            return ()
        product_ids = {str(ref.waterfall_product_id) for ref in refs}
        waterfall_digests = {ref.waterfall_bundle_digest for ref in refs}
        if len(product_ids) != 1 or len(waterfall_digests) != 1:
            raise RuntimeError(
                "recording Doppler products identify different waterfalls"
            )
        with self._waterfalls.open(next(iter(product_ids))) as durable_waterfall:
            waterfall = durable_waterfall.bundle
            if (
                waterfall.recording_id != recording_id
                or durable_waterfall.ref.bundle_ref.digest
                != next(iter(waterfall_digests))
            ):
                raise RuntimeError("Doppler and waterfall products disagree")
            tiles = {
                (tile.segment_id, tile.receiver_chain_id): tile
                for tile in waterfall.tiles
            }
            paths: list[PublishedAdvancedDopplerPathV0_1] = []
            for ref in refs:
                tile = tiles.get((ref.segment_id, ref.receiver_chain_id))
                if tile is None:
                    raise RuntimeError("advanced Doppler path has no waterfall tile")
                with self._doppler.open(recording_id, ref.doppler_id) as durable:
                    advanced = durable.advanced
                    bank = advanced.slope_bank
                    association = advanced.association
                    if (
                        bank is None
                        or association is None
                        or association.state != "advanced-path-only"
                    ):
                        continue
                    if len(bank.track.bins) != len(tile.time_bins):
                        raise RuntimeError(
                            "advanced Doppler path and waterfall rows disagree"
                        )
                    points = tuple(
                        PublishedAdvancedDopplerPathPointV0_1(
                            row_index,
                            row.start_sample,
                            row.stop_sample,
                            _sample_utc_ns(
                                tile.segment_start_utc_ns,
                                row.start_sample,
                                tile.sample_rate_hz,
                            ),
                            _sample_utc_ns(
                                tile.segment_start_utc_ns,
                                row.stop_sample,
                                tile.sample_rate_hz,
                            ),
                            row.midpoint_utc_ns,
                            tile.center_frequency_hz
                            + tile.frequency_bin_offsets_hz[frequency_bin],
                        )
                        for row_index, (row, frequency_bin) in enumerate(
                            zip(tile.time_bins, bank.track.bins, strict=True)
                        )
                    )
                    paths.append(
                        PublishedAdvancedDopplerPathV0_1(
                            recording_id,
                            ref.segment_id,
                            ref.receiver_chain_id,
                            bank.candidate_path_digest,
                            str(ref.doppler_id) + ":advanced",
                            association.state,
                            bank.track.drift_rate_hz_s,
                            points,
                        )
                    )
        return tuple(
            sorted(
                paths,
                key=lambda item: (str(item.segment_id), str(item.receiver_chain_id)),
            )
        )


def _unavailable(
    recording_id: RecordingId, layer: DopplerWaterfallLayer
) -> RecordingDopplerVisualizationViewV0_1:
    return RecordingDopplerVisualizationViewV0_1(
        SchemaRef(RecordingDopplerVisualizationViewV0_1.SCHEMA_ID),
        recording_id,
        DopplerVisualizationState.UNAVAILABLE,
        layer,
        True,
        None,
        None,
        (),
        (),
        (),
        (),
        (RecordingDopplerVisualizationViewV0_1.CANDIDATE_WARNING,),
        ("doppler-analysis-unavailable",),
    )


def _sample_utc_ns(start: UtcNs, sample: int, sample_rate_hz: float) -> UtcNs:
    return UtcNs(int(start) + round(sample * 1_000_000_000 / sample_rate_hz))


def _tile(
    value: WaterfallTileV0_2, layer: DopplerWaterfallLayer
) -> DopplerWaterfallTileViewV0_1:
    rows = tuple(
        DopplerWaterfallTimeBinViewV0_1(
            row.midpoint_utc_ns,
            {
                DopplerWaterfallLayer.AVERAGE: row.average_power_db,
                DopplerWaterfallLayer.RESIDUAL: row.temporal_median_residual_db,
                DopplerWaterfallLayer.HIGH_PERCENTILE: row.high_percentile_power_db,
            }[layer],
        )
        for row in value.time_bins
    )
    coverage = value.coverage
    return DopplerWaterfallTileViewV0_1(
        value.segment_id,
        value.receiver_chain_id,
        value.center_frequency_hz,
        value.sample_rate_hz,
        value.fft_window_samples,
        value.power_reference,
        value.high_percentile,
        value.frequency_bin_offsets_hz,
        DopplerWaterfallCoverageViewV0_1(
            coverage.contiguous_rf_span_count,
            coverage.contiguous_rf_sample_count,
            coverage.analyzed_sample_count,
            coverage.discarded_tail_sample_count,
            coverage.fft_frame_count,
            coverage.coverage_fraction,
        ),
        rows,
    )


def _candidate(
    ref: DopplerAnalysisRefV0_1, value: BlindDopplerCandidateV0_1
) -> DopplerCandidateViewV0_1:
    fit = next(item for item in value.fits if item.order == value.selected_order)
    model = {
        DopplerPolynomialOrder.CONSTANT: DopplerTrackModel.CONSTANT,
        DopplerPolynomialOrder.LINEAR: DopplerTrackModel.LINEAR,
        DopplerPolynomialOrder.QUADRATIC: DopplerTrackModel.QUADRATIC,
    }[value.selected_order]
    control = value.stationary_control
    return DopplerCandidateViewV0_1(
        value.rank,
        value.component_id,
        ref.segment_id,
        ref.receiver_chain_id,
        model,
        fit.reference_utc_ns,
        fit.frequency_hz,
        fit.drift_rate_hz_s,
        fit.drift_acceleration_hz_s2,
        fit.residual_rms_hz,
        fit.robust_scale_hz,
        fit.inlier_count,
        value.mean_spectral_peak_excess_db,
        value.peak_layer_value_db,
        value.duration_s,
        value.missing_row_fraction,
        value.edge_truncated_point_count,
        value.ranking_score,
        DopplerStationaryControlViewV0_1(
            control.constant_residual_rms_hz,
            control.selected_residual_rms_hz,
            control.residual_improvement_fraction,
            control.bic_margin_over_constant,
            control.moving_model_preferred,
        ),
        tuple(
            DopplerTrackPointViewV0_1(
                point.midpoint_utc_ns,
                point.frequency_hz,
                point.layer_value_db,
                point.local_peak_excess_db,
                point.edge_truncated,
            )
            for point in value.points
        ),
    )


def _advanced(
    ref: DopplerAnalysisRefV0_1, value: AdvancedDopplerEvidenceBundleV0_1
) -> DopplerAdvancedEvidenceViewV0_1:
    bank = value.slope_bank
    association = value.association
    assert bank is not None and association is not None
    return DopplerAdvancedEvidenceViewV0_1(
        candidate_rank=bank.basic_candidate_rank,
        segment_id=ref.segment_id,
        receiver_chain_id=ref.receiver_chain_id,
        slope_bins_per_row=bank.track.slope_bins_per_row,
        heldout_score=bank.heldout_score,
        stationary_score=bank.stationary_score,
        opposite_slope_score=bank.opposite_slope_score,
        shuffled_scores=bank.time_shuffle_scores,
        comb=None
        if value.comb is None
        else DopplerCombEvidenceViewV0_1(
            value.comb.fit_score,
            value.comb.heldout_score,
            value.comb.wrong_spacing_score,
        ),
        broadband=None
        if value.broadband is None
        else DopplerBroadbandEvidenceViewV0_1(
            value.broadband.lower_slope_bins_per_row,
            value.broadband.upper_slope_bins_per_row,
            value.broadband.edge_slope_difference,
            value.broadband.width_mad_fraction,
            value.broadband.texture_shift_bins,
            value.broadband.texture_correlation,
        ),
        dual_receiver=None
        if value.dual_receiver is None
        else DopplerDualReceiverEvidenceViewV0_1(
            value.dual_receiver.common_slope_bins_per_row,
            value.dual_receiver.slope_difference,
            value.dual_receiver.receiver_offsets_bins,
            value.dual_receiver.offset_removed_rms_bins,
            value.dual_receiver.path_correlation,
        ),
        orbit_association=None
        if value.tle_association is None
        else DopplerOrbitAssociationViewV0_1(
            value.tle_association.name,
            value.tle_association.offset_bins,
            value.tle_association.heldout_rms_bins,
            value.tle_association.runner_up_margin_bins,
            value.tle_association.stationary_control_rms_bins,
            value.tle_association.opposite_slope_control_rms_bins,
            value.tle_association.qualified,
        ),
        drift_rate_hz_s=bank.track.drift_rate_hz_s,
        spectral_peak_excess_reference=value.spectral_peak_excess_reference,
        source_input_digest=bank.source_input_digest,
        candidate_path_digest=bank.candidate_path_digest,
        association=DopplerCandidatePathAssociationViewV0_1(
            DopplerCandidateAssociationState(association.state),
            association.candidate_path_digest,
            association.basic_candidate_rank,
            association.overlap_point_count,
            association.overlap_fraction,
            association.mean_frequency_distance_hz,
            association.maximum_frequency_distance_hz,
        ),
    )
