from __future__ import annotations

from dataclasses import replace

import numpy as np

from leo_flow.analysis.recording.starlink_receiver_agnostic_cfo import (
    ReceiverAgnosticCfoQamAnalyzerV0_6,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    Provenance,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_receiver_agnostic_cfo import (
    V0_6,
    ReceiverAgnosticCfoSearchPlanV0_6,
    ReceiverAgnosticCfoWindowV0_6,
)
from leo_flow.contracts.starlink_receiver_agnostic_cfo_product import (
    ReceiverAgnosticCfoQamRecordingBundleV0_6,
    ReceiverAgnosticCfoQamRecordingPlanV0_6,
    ReceiverAgnosticCfoQamRecordingRequestV0_6,
)
from leo_flow.contracts.storage import ObjectRef

from .fakes import SegmentFixture, execution_context, make_view


def product_pair(
    recording_id: str = "rec_cfo_qam_product",
):  # type: ignore[no-untyped-def]
    raw = (recording_id.encode() * (60_000 // len(recording_id) + 1))[:60_000]
    _, recording = make_view(
        SegmentFixture(raw, 2_500_000),
        recording_id=RecordingId(recording_id),
    )
    metadata = f"metadata:{recording_id}".encode()
    recording = replace(
        recording,
        metadata_object=ObjectRef(
            Digest.sha256(metadata),
            len(metadata),
            "application/json",
            "sigmf-meta-v1",
            f"fixture:metadata:{recording_id}",
        ),
    )
    search = replace(
        ReceiverAgnosticCfoSearchPlanV0_6(),
        coarse_cfo_step_hz=350_000.0,
        local_cfo_radius_hz=350_000.0,
        local_cfo_step_hz=350_000.0,
        basins_per_pattern=1,
        basin_cfo_separation_hz=350_000.0,
    )
    plan = ReceiverAgnosticCfoQamRecordingPlanV0_6(
        search,
        maximum_streams=1,
        maximum_windows_per_stream=1,
        maximum_patterns=2,
        maximum_pattern_evidence=2,
    )
    identity = recording.identity_digest()
    window = ReceiverAgnosticCfoWindowV0_6(
        recording.recording_id,
        identity,
        RadioId("radio_a"),
        SegmentId("seg_00"),
        ReceiverChainId("rx_0"),
        StarlinkEdge.LOWER,
        2_500_000.0,
        0,
        7_500,
        ArtifactRef("recording-object", identity),
        ArtifactRef("window-object", identity),
    )
    product = ReceiverAgnosticCfoQamAnalyzerV0_6(search).analyze(
        np.zeros(7_500, dtype=np.complex128),
        window,
        pattern_count=2,
        execution=execution_context(),
    )
    request = ReceiverAgnosticCfoQamRecordingRequestV0_6(
        SchemaRef(ReceiverAgnosticCfoQamRecordingRequestV0_6.SCHEMA_ID, V0_6),
        recording.recording_id,
        recording,
        plan,
        (window,),
        SchemaRef(ReceiverAgnosticCfoQamRecordingBundleV0_6.SCHEMA_ID, V0_6),
    )
    context = execution_context()
    bundle = ReceiverAgnosticCfoQamRecordingBundleV0_6(
        SchemaRef(ReceiverAgnosticCfoQamRecordingBundleV0_6.SCHEMA_ID, V0_6),
        "slcfoqam6rec_" + identity.value[:32],
        recording.recording_id,
        identity,
        request.digest,
        plan,
        (product,),
        Provenance(
            context.producer_name,
            context.producer_version,
            context.git_commit,
            context.environment_digest,
            plan.digest,
            (identity,),
            (product.digest,),
            context.started_utc_ns,
            context.completed_utc_ns,
            context.host_class,
        ),
        True,
        None,
        tuple(
            sorted(
                (
                    "candidate-evidence-not-calibrated-detection",
                    "explicit-offline-publication-only",
                    "identical-residual-cfo-domain-for-every-radio-rx",
                    "no-lnb-label-center-or-receiver-correction",
                    "pattern-symmetric-known-pattern-qam",
                )
            )
        ),
    )
    return request, bundle
