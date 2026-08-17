from __future__ import annotations

import pytest

from leo_flow.analysis.recording.waterfall_doppler_pipeline import (
    AdvancedBlindDopplerAnalyzerV0_1,
    AdvancedDopplerConfigV0_1,
    DualReceiverPathInputV0_1,
    _peer_path,
    blind_candidate_path_digest,
    spectrogram_from_residual_v0_1,
)
from leo_flow.analysis.tracking.blind_doppler import (
    BasicBlindDopplerAnalyzer,
    BlindDopplerConfig,
    blind_doppler_config_digest,
)
from leo_flow.contracts.blind_doppler import (
    BlindDopplerAnalysisRequestV0_1,
    BlindDopplerBundleV0_1,
    SpectrogramRowV0_1,
    SpectrogramSliceV0_1,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    JobId,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.contracts.waterfall import (
    WaterfallAnalysisRequestV0_1,
    WaterfallBundleV0_1,
    WaterfallProductId,
)
from leo_flow.contracts.waterfall_v0_2 import (
    V0_2,
    WaterfallBundleV0_2,
    WaterfallCoverageV0_2,
    WaterfallTileV0_2,
    WaterfallTimeBinV0_2,
)
from leo_flow.jobs.contracts import JobLease, JobType
from leo_flow.services.waterfall_analysis import waterfall_analysis_payload
from leo_flow.services.waterfall_doppler_analysis import (
    CombinedWaterfallAnalysisJobPreparerV0_1,
)


def _bundle(receiver: str = "rx_first") -> WaterfallBundleV0_2:
    rows = []
    for row_index in range(12):
        residual = [0.0] * 16
        residual[2 + row_index] = 10.0
        start = row_index * 32
        rows.append(
            WaterfallTimeBinV0_2(
                start_sample=start,
                stop_sample=start + 32,
                midpoint_utc_ns=UtcNs(
                    1_000 + (row_index * 1_000_000_000) + 500_000_000
                ),
                analyzed_sample_count=32,
                fft_frame_count=1,
                fft_frame_start_samples=(start,),
                average_power_db=tuple(residual),
                temporal_median_residual_db=tuple(residual),
                high_percentile_power_db=tuple(residual),
            )
        )
    tile = WaterfallTileV0_2(
        segment_id=SegmentId("seg_ch4_lower"),
        receiver_chain_id=ReceiverChainId(receiver),
        segment_start_utc_ns=UtcNs(1_000),
        segment_sample_count=384,
        center_frequency_hz=11_325_000_000.0,
        sample_rate_hz=32.0,
        fft_window_samples=32,
        fft_hop_samples=32,
        display_frequency_bins=16,
        power_reference="uncalibrated-counts-squared-per-native-fft-bin",
        high_percentile=95.0,
        frequency_bin_offsets_hz=tuple(float(value) for value in range(-15, 17, 2)),
        coverage=WaterfallCoverageV0_2(1, 384, 384, 0, 12, 1.0),
        time_bins=tuple(rows),
    )
    digest = Digest.sha256(b"recording")
    return WaterfallBundleV0_2(
        schema=SchemaRef(WaterfallBundleV0_2.SCHEMA_ID, V0_2),
        product_id=WaterfallProductId("waterfall_" + "1" * 32),
        analysis_run_id=AnalysisRunId("arun_" + "2" * 32),
        recording_id=RecordingId("rec_pipeline"),
        input_recording_identity_digest=digest,
        provenance=Provenance(
            "test",
            "0.2",
            "commit",
            Digest.sha256(b"environment"),
            Digest.sha256(b"config"),
            (digest,),
            (Digest.sha256(b"algorithm"),),
            UtcNs(1),
            UtcNs(2),
            "test-host",
        ),
        tiles=(tile,),
    )


def _basic(bundle: WaterfallBundleV0_2):
    spectrogram = spectrogram_from_residual_v0_1(bundle, bundle.tiles[0])
    config = BlindDopplerConfig(
        minimum_spectral_peak_excess_db=3.0,
        maximum_frequency_step_hz=3.0,
        maximum_abs_drift_rate_hz_s=0.0,
        minimum_track_points=4,
    )
    request = BlindDopplerAnalysisRequestV0_1(
        SchemaRef(BlindDopplerAnalysisRequestV0_1.SCHEMA_ID),
        spectrogram.input_identity_digest,
        blind_doppler_config_digest(config),
        4,
    )
    return spectrogram, BasicBlindDopplerAnalyzer(config).analyze_blind_doppler(
        spectrogram, request
    )


def test_residual_adapter_and_advanced_controls_preserve_exact_identity() -> None:
    spectrogram, basic = _basic(_bundle())

    advanced = AdvancedBlindDopplerAnalyzerV0_1(
        AdvancedDopplerConfigV0_1(
            doppler_rates_hz_s=(-2.0, 0.0, 2.0),
            maximum_viterbi_step_bins=1,
            viterbi_track_count=2,
        )
    ).analyze(spectrogram, basic)

    assert basic.candidates
    assert advanced.input_identity_digest == spectrogram.input_identity_digest
    assert advanced.slope_bank is not None
    assert advanced.slope_bank.track.slope_bins_per_row == 1.0
    assert advanced.slope_bank.track.drift_rate_hz_s == 2.0
    assert advanced.slope_bank.heldout_score > advanced.slope_bank.stationary_score
    assert advanced.slope_bank.heldout_score > advanced.slope_bank.opposite_slope_score
    assert all(
        advanced.slope_bank.heldout_score > value
        for value in advanced.slope_bank.time_shuffle_scores
    )
    assert advanced.comb is None
    assert advanced.broadband is None
    assert advanced.dual_receiver is None
    assert advanced.tle_association is None


def test_optional_dual_receiver_evidence_is_only_emitted_with_input() -> None:
    spectrogram, basic = _basic(_bundle())
    points = tuple(point.interpolated_bin for point in basic.candidates[0].points)
    peer = tuple(value + 2.5 for value in points)
    first = AdvancedBlindDopplerAnalyzerV0_1().analyze(spectrogram, basic)
    assert first.association is not None
    assert first.association.basic_candidate_rank == 1
    path_digest = blind_candidate_path_digest(spectrogram, basic.candidates[0])

    advanced = AdvancedBlindDopplerAnalyzerV0_1().analyze(
        spectrogram,
        basic,
        dual_receiver=DualReceiverPathInputV0_1(
            path_digest,
            Digest.sha256(b"peer-path"),
            ReceiverChainId("rx_second"),
            points,
            peer,
        ),
    )

    assert advanced.dual_receiver is not None
    assert advanced.dual_receiver.peer_receiver_chain_id == "rx_second"
    assert advanced.dual_receiver.receiver_offsets_bins == (0.0, 2.5)
    assert advanced.dual_receiver.candidate_path_digest == path_digest
    assert len(advanced.auxiliary_input_digests) == 1


def test_physical_rate_grid_resolves_report_scale_drift() -> None:
    bin_width_hz = 4_882.8125
    cadence_s = 0.1
    expected_rate_hz_s = -3_750.0
    expected_slope = expected_rate_hz_s * cadence_s / bin_width_hz
    power_rows = []
    for row_index in range(120):
        values = [0.0] * 128
        values[round(80 + expected_slope * row_index)] = 9.0
        power_rows.append(
            SpectrogramRowV0_1(UtcNs(1_000 + row_index * 100_000_000), tuple(values))
        )
    spectrogram = SpectrogramSliceV0_1(
        SchemaRef(SpectrogramSliceV0_1.SCHEMA_ID),
        Digest.sha256(b"report-scale-spectrogram"),
        SegmentId("seg_report_scale"),
        ReceiverChainId("rx_report_scale"),
        11_325_000_000.0,
        tuple((index - 64) * bin_width_hz for index in range(128)),
        "temporal-median-residual-db",
        tuple(power_rows),
    )
    basic = BlindDopplerBundleV0_1(
        SchemaRef(BlindDopplerBundleV0_1.SCHEMA_ID),
        spectrogram.input_identity_digest,
        Digest.sha256(b"basic-config"),
        "blind-doppler-v0.1",
        True,
        120,
        0,
        (),
        (),
        ("no_candidate_met_track_bounds",),
    )

    advanced = AdvancedBlindDopplerAnalyzerV0_1(
        AdvancedDopplerConfigV0_1(
            doppler_rates_hz_s=(-3_750.0, 0.0, 3_750.0),
            viterbi_track_count=1,
        )
    ).analyze(spectrogram, basic)

    assert advanced.slope_bank is not None
    assert advanced.slope_bank.track.drift_rate_hz_s == pytest.approx(
        expected_rate_hz_s
    )
    assert advanced.slope_bank.test_rows


def test_mismatched_peer_path_is_not_bundled() -> None:
    spectrogram, basic = _basic(_bundle())
    points = tuple(point.interpolated_bin for point in basic.candidates[0].points)

    advanced = AdvancedBlindDopplerAnalyzerV0_1().analyze(
        spectrogram,
        basic,
        dual_receiver=DualReceiverPathInputV0_1(
            Digest.sha256(b"another-ridge"),
            Digest.sha256(b"peer-ridge"),
            ReceiverChainId("rx_second"),
            points,
            tuple(reversed(points)),
        ),
    )

    assert advanced.dual_receiver is None
    assert advanced.warnings == ("dual-receiver-input-path-mismatch",)


def test_crossing_peer_selection_does_not_follow_unrelated_rank_one() -> None:
    def crossing(receiver: str, positive: float, negative: float):
        axis = tuple(float(value) for value in range(-20_000, 20_001, 1_000))
        rows = []
        for row_index in range(24):
            time_s = row_index * 0.01
            values = [0.0] * len(axis)
            for frequency, strength in (
                (-8_000 + 70_000 * time_s, positive),
                (8_000 - 70_000 * time_s, negative),
            ):
                index = min(
                    range(len(axis)), key=lambda item: abs(axis[item] - frequency)
                )
                values[index] += strength
            rows.append(
                SpectrogramRowV0_1(UtcNs(1_000 + row_index * 10_000_000), tuple(values))
            )
        return SpectrogramSliceV0_1(
            SchemaRef(SpectrogramSliceV0_1.SCHEMA_ID),
            Digest.sha256(receiver.encode()),
            SegmentId("seg_crossing"),
            ReceiverChainId(receiver),
            11_325_000_000.0,
            axis,
            "temporal-median-residual-db",
            tuple(rows),
        )

    config = BlindDopplerConfig(
        minimum_spectral_peak_excess_db=5.0,
        maximum_frequency_step_hz=1_250.0,
        maximum_abs_drift_rate_hz_s=120_000.0,
    )

    def analyze_basic(spectrogram):
        request = BlindDopplerAnalysisRequestV0_1(
            SchemaRef(BlindDopplerAnalysisRequestV0_1.SCHEMA_ID),
            spectrogram.input_identity_digest,
            blind_doppler_config_digest(config),
            8,
        )
        return BasicBlindDopplerAnalyzer(config).analyze_blind_doppler(
            spectrogram, request
        )

    own = crossing("rx_crossing_own", 18.0, 12.0)
    peer = crossing("rx_crossing_peer", 10.0, 20.0)
    own_basic = analyze_basic(own)
    peer_basic = analyze_basic(peer)
    advanced = AdvancedBlindDopplerAnalyzerV0_1(
        AdvancedDopplerConfigV0_1(
            doppler_rates_hz_s=(-70_000.0, 0.0, 70_000.0),
            maximum_mean_candidate_distance_hz=2_000.0,
            maximum_candidate_point_distance_hz=3_000.0,
        )
    ).analyze(own, own_basic)

    assert advanced.association is not None
    selected = next(
        candidate
        for candidate in own_basic.candidates
        if candidate.rank == advanced.association.basic_candidate_rank
    )
    selected_rate = next(
        fit.drift_rate_hz_s
        for fit in selected.fits
        if fit.order == selected.selected_order
    )
    peer_rank_one_rate = next(
        fit.drift_rate_hz_s
        for fit in peer_basic.candidates[0].fits
        if fit.order == peer_basic.candidates[0].selected_order
    )
    assert selected_rate * peer_rank_one_rate < 0

    match = _peer_path(
        own,
        own_basic,
        advanced.association,
        (own, peer),
        (own_basic, peer_basic),
    )

    assert match is not None
    assert match.candidate_path_digest == advanced.association.candidate_path_digest
    assert match.peer_candidate_path_digest != blind_candidate_path_digest(
        peer, peer_basic.candidates[0]
    )


def test_combined_preparer_preserves_legacy_payload_and_opens_recording_once() -> None:
    recording_id = RecordingId("rec_combined")
    recording_ref = RecordingObjectRef(
        recording_id,
        ObjectRef(
            Digest.sha256(b"data"), 4, "application/octet-stream", "ci16", "cas:data"
        ),
        ObjectRef(
            Digest.sha256(b"metadata"), 4, "application/json", "json", "cas:metadata"
        ),
        Digest.sha256(b"manifest"),
    )
    request = WaterfallAnalysisRequestV0_1(
        SchemaRef(WaterfallAnalysisRequestV0_1.SCHEMA_ID),
        recording_id,
        recording_ref,
        ArtifactRef("legacy-algorithm", Digest.sha256(b"algorithm")),
        ArtifactRef("legacy-config", Digest.sha256(b"config")),
        (),
        SchemaRef(WaterfallBundleV0_1.SCHEMA_ID),
    )
    lease = JobLease(
        JobId("job_combined"),
        JobType.WATERFALL_ANALYSIS,
        waterfall_analysis_payload(request),
        1,
        "lease",
        1,
        UtcNs(10_000),
    )

    class _Context:
        def __enter__(self):
            return "recording-view"

        def __exit__(self, *_args):
            return None

    class _Reader:
        calls = 0

        def open(self, ref):
            assert ref == recording_ref
            self.calls += 1
            return _Context()

    class _Legacy:
        def analyze_waterfall(self, recording, selected):
            assert recording == "recording-view"
            assert selected == request
            return "legacy-bundle"

    class _Enhanced:
        def analyze(self, recording, selected):
            assert recording == "recording-view"
            assert selected == request
            return "enhanced-products"

    reader = _Reader()
    prepared = CombinedWaterfallAnalysisJobPreparerV0_1(
        reader, _Legacy(), _Enhanced()
    ).prepare(lease)

    assert reader.calls == 1
    assert prepared.request == request
    assert prepared.bundle == "legacy-bundle"
    assert prepared.enhanced == "enhanced-products"
