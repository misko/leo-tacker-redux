"""Bounded adaptive-QAM producer layered after symmetric detector responses."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from leo_flow.analysis.recording.quality import decode_ci16
from leo_flow.analysis.recording.starlink_adaptive_qam import (
    adaptive_qam_window_selections_v0_4,
    shared_adaptive_qam_window_selections_v0_4,
)
from leo_flow.analysis.recording.starlink_adaptive_qam_persistence import (
    DurableStarlinkAdaptiveQamStoreV0_4,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    StarlinkAcquiredConstellationOverallV0_3,
    StarlinkAcquiredConstellationRecordingBundleV0_3,
    StarlinkAcquiredConstellationRequestV0_3,
    StarlinkAcquiredConstellationStreamV0_3,
    StarlinkAcquiredConstellationWindowV0_3,
)
from leo_flow.contracts.starlink_acquisition import V0_3
from leo_flow.contracts.starlink_adaptive_qam import (
    V0_4,
    StarlinkAdaptiveQamBundleV0_4,
    StarlinkAdaptiveQamRequestV0_4,
    StarlinkAdaptiveQamStreamRequestV0_4,
    StarlinkAdaptiveQamWindowSelectionV0_4,
)
from leo_flow.contracts.starlink_adaptive_response import (
    StarlinkAdaptiveResponseBundleV0_1,
    StarlinkAdaptiveResponseProductRefV0_1,
    StarlinkAdaptiveResponseStreamV0_1,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
)
from leo_flow.contracts.storage import RecordingObjectRef
from leo_flow.services.starlink_acquired_constellation_analysis import (
    StarlinkAcquiredDwellCompositionProfileV0_3,
)
from leo_flow.storage.ports import RecordingObjectReader


@dataclass(frozen=True)
class PublishedAdaptiveQamV0_4:
    request: StarlinkAdaptiveQamRequestV0_4
    bundle: StarlinkAdaptiveQamBundleV0_4
    artifact_ref: ArtifactRef


class DurableAdaptiveQamProducerV0_4:
    def __init__(
        self,
        reader: RecordingObjectReader,
        products: DurableStarlinkAdaptiveQamStoreV0_4,
        profiles: tuple[StarlinkAcquiredDwellCompositionProfileV0_3, ...],
        *,
        qam_window_sample_count: int = 50_000,
        maximum_windows_per_stream: int = 12,
    ) -> None:
        keys = tuple(
            (item.suite_config_ref, item.receiver_chain_id) for item in profiles
        )
        if (
            not profiles
            or len(keys) != len(set(keys))
            or qam_window_sample_count <= 0
            or not 3 <= maximum_windows_per_stream <= 24
        ):
            raise ValueError("adaptive QAM producer profile is invalid")
        self._reader, self._products, self._profiles = reader, products, profiles
        self._qam_window_sample_count = qam_window_sample_count
        self._maximum_windows = maximum_windows_per_stream

    def publish(
        self,
        recording_ref: RecordingObjectRef,
        source_suite_ref: StarlinkDetectorSuiteProductRefV0_2,
        source_suite_request_digest: Digest,
        source_suite: StarlinkDetectorSuiteRecordingBundleV0_2,
        source_response_ref: StarlinkAdaptiveResponseProductRefV0_1,
        source_response: StarlinkAdaptiveResponseBundleV0_1,
    ) -> PublishedAdaptiveQamV0_4:
        if (
            recording_ref.recording_id != source_response.recording_id
            or recording_ref.identity_digest()
            != source_response.recording_identity_digest
            or source_suite.recording_id != source_response.recording_id
            or source_suite.analysis_id != source_suite_ref.analysis_id
            or source_response.source_suite_ref.artifact_id
            != source_suite_ref.analysis_id
            or source_response_ref.analysis_id != source_response.analysis_id
        ):
            raise ValueError("adaptive QAM source closure differs")
        profile_map = {
            (item.suite_config_ref, item.receiver_chain_id): item
            for item in self._profiles
        }
        config_refs = {
            method.config_ref
            for suite in source_suite.suites
            for method in suite.methods
        }
        grouped_response_streams: dict[
            tuple[object, ...], list[StarlinkAdaptiveResponseStreamV0_1]
        ] = defaultdict(list)
        for stream in source_response.streams:
            grouped_response_streams[
                (
                    stream.radio_id,
                    stream.segment_id,
                    stream.channel_number,
                    stream.edge,
                    stream.sample_rate_hz,
                    stream.segment_sample_count,
                )
            ].append(stream)
        selections_by_stream: dict[
            tuple[object, object, object],
            tuple[StarlinkAdaptiveQamWindowSelectionV0_4, ...],
        ] = {}
        for grouped in grouped_response_streams.values():
            ordered = tuple(
                sorted(grouped, key=lambda item: str(item.receiver_chain_id))
            )
            shared = shared_adaptive_qam_window_selections_v0_4(
                ordered,
                qam_window_sample_count=self._qam_window_sample_count,
                maximum_windows=self._maximum_windows,
            )
            for stream, group_selections in zip(ordered, shared, strict=True):
                selections_by_stream[
                    (stream.segment_id, stream.receiver_chain_id, stream.edge)
                ] = group_selections
        with self._reader.open(recording_ref) as recording:
            segments = {item.segment_id: item for item in recording.manifest.segments}
            request_streams = []
            evidence_streams = []
            for response_stream in source_response.streams:
                source = next(
                    (
                        item
                        for item in source_suite.suites
                        if item.segment_id == response_stream.segment_id
                        and item.receiver_chain_id == response_stream.receiver_chain_id
                        and item.edge == response_stream.edge
                    ),
                    None,
                )
                segment = segments.get(response_stream.segment_id)
                matches = tuple(
                    profile
                    for (config_ref, receiver), profile in profile_map.items()
                    if config_ref in config_refs
                    and receiver == response_stream.receiver_chain_id
                )
                if source is None or segment is None or len(matches) != 1:
                    raise ValueError("adaptive QAM stream profile is not authoritative")
                profile = matches[0]
                try:
                    receiver_index = segment.requested.receiver_chain_ids.index(
                        response_stream.receiver_chain_id
                    )
                except ValueError as error:
                    raise ValueError("adaptive QAM receiver is absent") from error
                selections = selections_by_stream.get(
                    (
                        response_stream.segment_id,
                        response_stream.receiver_chain_id,
                        response_stream.edge,
                    )
                )
                if selections is None:
                    selections = adaptive_qam_window_selections_v0_4(
                        response_stream,
                        qam_window_sample_count=self._qam_window_sample_count,
                        maximum_windows=self._maximum_windows,
                    )
                templates = qin_edge_pilot_template_pair_v0_1(
                    segment.actual_sample_rate_hz, response_stream.edge
                )
                windows = []
                for index, selection in enumerate(selections):
                    raw = recording.read_iq_bytes(
                        response_stream.segment_id,
                        selection.qam_start_sample,
                        selection.qam_stop_sample,
                    )
                    values, count = decode_ci16(
                        raw, len(segment.requested.receiver_chain_ids)
                    )
                    if count != self._qam_window_sample_count:
                        raise ValueError(
                            "adaptive QAM reader returned another interval"
                        )
                    stride = len(segment.requested.receiver_chain_ids) * 2
                    offset = receiver_index * 2
                    samples = tuple(
                        complex(values[position], values[position + 1])
                        for position in range(offset, count * stride, stride)
                    )
                    suite = profile.suite_analyzer.analyze_receiver(
                        samples,
                        recording_id=recording_ref.recording_id,
                        recording_identity_digest=recording_ref.identity_digest(),
                        segment_id=response_stream.segment_id,
                        receiver_chain_id=response_stream.receiver_chain_id,
                        templates=templates,
                    )
                    acquisition = profile.acquisition_analyzer.analyze_receiver(
                        samples,
                        recording_id=recording_ref.recording_id,
                        recording_identity_digest=recording_ref.identity_digest(),
                        segment_id=response_stream.segment_id,
                        receiver_chain_id=response_stream.receiver_chain_id,
                        templates=templates,
                    )
                    evidence = profile.constellation_analyzer.analyze(
                        samples, suite, acquisition, time_window_count=len(selections)
                    )
                    start_utc = int(segment.start_utc_ns) + round(
                        selection.qam_start_sample
                        / segment.actual_sample_rate_hz
                        * 1_000_000_000
                    )
                    stop_utc = int(segment.start_utc_ns) + round(
                        selection.qam_stop_sample
                        / segment.actual_sample_rate_hz
                        * 1_000_000_000
                    )
                    windows.append(
                        StarlinkAcquiredConstellationWindowV0_3(
                            index,
                            selection.qam_start_sample,
                            selection.qam_stop_sample,
                            UtcNs(start_utc),
                            UtcNs(stop_utc),
                            acquisition,
                            evidence,
                        )
                    )
                request_streams.append(
                    StarlinkAdaptiveQamStreamRequestV0_4(
                        response_stream.radio_id,
                        response_stream.lnb_id,
                        response_stream.segment_id,
                        response_stream.receiver_chain_id,
                        response_stream.channel_number,
                        response_stream.edge,
                        response_stream.sample_rate_hz,
                        response_stream.segment_sample_count,
                        selections,
                    )
                )
                evidence_streams.append(
                    StarlinkAcquiredConstellationStreamV0_3(
                        response_stream.radio_id,
                        response_stream.segment_id,
                        response_stream.receiver_chain_id,
                        response_stream.edge,
                        response_stream.sample_rate_hz,
                        response_stream.segment_sample_count,
                        tuple(windows),
                        adaptive_qam_overall_v0_4(tuple(windows)),
                    )
                )
        request_streams.sort(key=lambda item: item.identity)
        evidence_streams.sort(
            key=lambda item: tuple(
                map(
                    str,
                    (
                        item.radio_id,
                        item.segment_id,
                        item.receiver_chain_id,
                        item.edge,
                    ),
                )
            )
        )
        source_suite_artifact = ArtifactRef(
            source_suite_ref.analysis_id,
            source_suite_ref.bundle_ref.digest,
            source_suite.schema,
        )
        request = StarlinkAdaptiveQamRequestV0_4(
            SchemaRef(StarlinkAdaptiveQamRequestV0_4.SCHEMA_ID, V0_4),
            recording_ref.recording_id,
            recording_ref,
            source_response_ref.artifact_ref,
            source_suite_artifact,
            tuple(request_streams),
            SchemaRef(StarlinkAdaptiveQamBundleV0_4.SCHEMA_ID, V0_4),
        )
        inner_request = StarlinkAcquiredConstellationRequestV0_3(
            SchemaRef(StarlinkAcquiredConstellationRequestV0_3.SCHEMA_ID, V0_3),
            recording_ref.recording_id,
            recording_ref,
            source_suite_artifact,
            source_suite_request_digest,
            tuple(
                (item.radio_id, item.segment_id, item.receiver_chain_id, item.edge)
                for item in evidence_streams
            ),
            max(len(item.windows) for item in evidence_streams),
            SchemaRef(StarlinkAcquiredConstellationRecordingBundleV0_3.SCHEMA_ID, V0_3),
        )
        inner_token = canonical_digest(
            {
                "adaptive_qam_request": request.digest,
                "streams": tuple(canonical_digest(item) for item in evidence_streams),
            }
        ).value
        evidence_bundle = StarlinkAcquiredConstellationRecordingBundleV0_3(
            SchemaRef(StarlinkAcquiredConstellationRecordingBundleV0_3.SCHEMA_ID, V0_3),
            f"slqam3rec_{inner_token[:32]}",
            recording_ref.recording_id,
            recording_ref.identity_digest(),
            source_suite_artifact,
            source_suite_request_digest,
            inner_request.digest,
            tuple(evidence_streams),
            (
                "candidate-evidence-not-calibrated-detection",
                "whole-revised-search-calibration-required",
                "published-edge-pilot-not-user-payload",
                "bounded-window-sampling-across-dwell",
            ),
            None,
        )
        token = canonical_digest(
            {"request": request.digest, "evidence": evidence_bundle.digest}
        ).value
        bundle = StarlinkAdaptiveQamBundleV0_4(
            SchemaRef(StarlinkAdaptiveQamBundleV0_4.SCHEMA_ID, V0_4),
            f"slqam4_{token[:32]}",
            request.recording_id,
            recording_ref.identity_digest(),
            request.source_adaptive_response_ref,
            request.source_suite_ref,
            request.digest,
            request.streams,
            evidence_bundle,
            (
                "candidate-evidence-not-calibrated-detection",
                "adaptive-window-selection-bias-disclosed",
                "target-and-control-selected-windows-retained",
                "whole-time-epoch-cfo-search-calibration-required",
                "published-edge-pilot-not-user-payload",
            ),
            None,
        )
        ref = self._products.publish(
            request,
            bundle,
            idempotency_key=f"adaptive-qam:{source_response.analysis_id}:{request.digest.value}",
        )
        return PublishedAdaptiveQamV0_4(request, bundle, ref.artifact_ref)


def adaptive_qam_overall_v0_4(
    windows: tuple[StarlinkAcquiredConstellationWindowV0_3, ...],
) -> StarlinkAcquiredConstellationOverallV0_3:
    if not windows:
        raise ValueError("adaptive QAM overall requires windows")
    support = sum(item.evidence.complete_frame_count for item in windows)

    def weighted(name: str) -> float:
        return (
            sum(
                float(getattr(item.evidence, name)) * item.evidence.complete_frame_count
                for item in windows
            )
            / support
        )

    selected = max(
        range(len(windows)),
        key=lambda index: (
            windows[index].evidence.verify_minus_control_margin,
            windows[index].evidence.held_out_verify_score,
            -index,
        ),
    )
    return StarlinkAcquiredConstellationOverallV0_3(
        len(windows),
        support,
        weighted("hard_symbol_accuracy"),
        weighted("rms_evm"),
        weighted("model_snr_db"),
        max(item.evidence.held_out_verify_score for item in windows),
        max(item.evidence.verify_minus_control_margin for item in windows),
        selected,
    )
