from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.recording.starlink_adaptive_qam_codec import (
    MalformedStarlinkAdaptiveQamError,
    decode_starlink_adaptive_qam,
    encode_starlink_adaptive_qam,
)
from leo_flow.analysis.recording.starlink_adaptive_qam_persistence import (
    CatalogedStarlinkAdaptiveQamV0_4,
    DurableRecordingStarlinkAdaptiveQamQueryV0_4,
    DurableStarlinkAdaptiveQamStoreV0_4,
)
from leo_flow.contracts.core import ArtifactRef, Digest, SchemaRef, canonical_digest
from leo_flow.contracts.starlink_acquired_constellation_pipeline import (
    StarlinkAcquiredConstellationQueryV0_3,
    StarlinkAcquiredConstellationViewMode,
)
from leo_flow.contracts.starlink_adaptive_qam import (
    V0_4,
    AdaptiveQamSelectionReason,
    StarlinkAdaptiveQamBundleV0_4,
    StarlinkAdaptiveQamRequestV0_4,
    StarlinkAdaptiveQamStreamRequestV0_4,
    StarlinkAdaptiveQamWindowSelectionV0_4,
)
from leo_flow.storage.filesystem import FileSystemBlobStore

from .test_starlink_acquired_constellation_persistence_v0_3 import _prepared


def _adaptive_prepared():
    inner_request, evidence = _prepared()
    evidence_stream = evidence.streams[0]
    selections = tuple(
        StarlinkAdaptiveQamWindowSelectionV0_4(
            window.window_index,
            window.start_sample,
            window.stop_sample,
            window.start_sample,
            window.stop_sample,
            (AdaptiveQamSelectionReason.QIN_MARGIN,),
            0.4,
            0.02,
            0.38,
        )
        for window in evidence_stream.windows
    )
    stream = StarlinkAdaptiveQamStreamRequestV0_4(
        evidence_stream.radio_id,
        "lnb-current-a",
        evidence_stream.segment_id,
        evidence_stream.receiver_chain_id,
        4,
        evidence_stream.edge,
        evidence_stream.sample_rate_hz,
        evidence_stream.segment_sample_count,
        selections,
    )
    adaptive_ref = ArtifactRef(
        "slar_" + "a" * 32,
        Digest.sha256(b"adaptive"),
        SchemaRef(
            "org.leo-flow.starlink-adaptive-response-bundle", replace(V0_4, minor=1)
        ),
    )
    request = StarlinkAdaptiveQamRequestV0_4(
        SchemaRef(StarlinkAdaptiveQamRequestV0_4.SCHEMA_ID, V0_4),
        inner_request.recording_id,
        inner_request.recording_object_ref,
        adaptive_ref,
        inner_request.source_suite_ref,
        (stream,),
        SchemaRef(StarlinkAdaptiveQamBundleV0_4.SCHEMA_ID, V0_4),
    )
    token = canonical_digest({"request": request.digest, "evidence": evidence.digest})
    bundle = StarlinkAdaptiveQamBundleV0_4(
        SchemaRef(StarlinkAdaptiveQamBundleV0_4.SCHEMA_ID, V0_4),
        f"slqam4_{token.value[:32]}",
        request.recording_id,
        request.recording_object_ref.identity_digest(),
        request.source_adaptive_response_ref,
        request.source_suite_ref,
        request.digest,
        request.streams,
        evidence,
        (
            "candidate-evidence-not-calibrated-detection",
            "adaptive-window-selection-bias-disclosed",
            "target-and-control-selected-windows-retained",
            "whole-time-epoch-cfo-search-calibration-required",
            "published-edge-pilot-not-user-payload",
        ),
        None,
    )
    return request, bundle


class _Catalog:
    def __init__(self) -> None:
        self.item = None

    def publish_starlink_adaptive_qam(
        self, projection, bundle_ref, recording_ref, *, idempotency_key
    ):
        del recording_ref, idempotency_key
        incoming = CatalogedStarlinkAdaptiveQamV0_4(projection, bundle_ref)
        if self.item is None:
            self.item = incoming
        return self.item.ref

    def get_starlink_adaptive_qam(self, ref):
        return self.item if self.item is not None and self.item.ref == ref else None

    def latest_starlink_adaptive_qam(self, recording_id):
        return (
            self.item.ref
            if self.item is not None
            and self.item.projection.recording_id == recording_id
            else None
        )


def test_adaptive_qam_codec_is_canonical_and_fail_closed() -> None:
    _request, bundle = _adaptive_prepared()
    payload = encode_starlink_adaptive_qam(bundle)
    assert decode_starlink_adaptive_qam(payload) == bundle
    with pytest.raises(MalformedStarlinkAdaptiveQamError):
        decode_starlink_adaptive_qam(payload + b" ")


def test_adaptive_qam_store_and_query_preserve_selection_reasons(tmp_path) -> None:
    request, bundle = _adaptive_prepared()
    catalog = _Catalog()
    store = DurableStarlinkAdaptiveQamStoreV0_4(
        FileSystemBlobStore(tmp_path / "cas"), catalog
    )
    ref = store.publish(request, bundle, idempotency_key="adaptive-qam:test")
    assert store.publish(request, bundle, idempotency_key="adaptive-qam:test") == ref
    query = DurableRecordingStarlinkAdaptiveQamQueryV0_4(store, catalog)

    overall = query.recording_starlink_adaptive_qam(
        StarlinkAcquiredConstellationQueryV0_3(
            request.recording_id,
            radio_ids=(bundle.stream_selections[0].radio_id,),
            lnb_ids=("lnb-current-a",),
            maximum_points_per_constellation=100,
        )
    )
    assert len(overall.streams) == 1
    assert len(overall.streams[0].windows) == 1
    assert overall.streams[0].windows[0].selection.reasons == (
        AdaptiveQamSelectionReason.QIN_MARGIN,
    )
    assert len(overall.streams[0].windows[0].qam.display_points) == 100
    windows = query.recording_starlink_adaptive_qam(
        StarlinkAcquiredConstellationQueryV0_3(
            request.recording_id,
            mode=StarlinkAcquiredConstellationViewMode.WINDOWS,
            maximum_windows_per_stream=2,
            maximum_points_per_constellation=10,
        )
    )
    assert len(windows.streams[0].windows) == 2
    assert windows.candidate_only and windows.calibration_required
