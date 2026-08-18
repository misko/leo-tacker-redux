from __future__ import annotations

from leo_flow.analysis.recording.starlink_symbolwise_replay import (
    StarlinkSymbolwiseReplayConfigV0_1,
    starlink_symbolwise_replay_algorithm_ref_v0_1,
    starlink_symbolwise_replay_config_ref_v0_1,
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
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_surrogate_null import (
    StarlinkSearchPatternRole,
    StarlinkSearchPatternV0_1,
)
from leo_flow.contracts.starlink_symbolwise_replay import (
    V0_1 as CORE_V0_1,
)
from leo_flow.contracts.starlink_symbolwise_replay import (
    StarlinkReceiverFrequencyCenterV0_1,
    StarlinkSymbolwisePatternEvidenceV0_1,
    StarlinkSymbolwiseReplayBundleV0_1,
    StarlinkSymbolwiseWindowEvidenceV0_1,
)
from leo_flow.contracts.starlink_symbolwise_replay_product import (
    V0_1,
    StarlinkSymbolwiseRecordingBundleV0_1,
    StarlinkSymbolwiseRecordingPlanV0_1,
    StarlinkSymbolwiseReplayRequestV0_1,
    StarlinkSymbolwiseReplayStreamSelectionV0_1,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef

SAMPLE_RATE_HZ = 2_500_000.0
WINDOW_SAMPLES = 25_000
CADENCE_SAMPLES = 250_000
SEGMENT_SAMPLES = 150_000_000
DEFAULT_RECORDING_ID = RecordingId("rec_symbolwise_product")
DEFAULT_RECEIVER_CHAIN_ID = ReceiverChainId("rx_lnb_c")


def frequency_center(
    center_hz: float = 602_869.4, *, path: bytes = b"physical-path-c"
) -> StarlinkReceiverFrequencyCenterV0_1:
    return StarlinkReceiverFrequencyCenterV0_1(
        SchemaRef(StarlinkReceiverFrequencyCenterV0_1.SCHEMA_ID, CORE_V0_1),
        "calibration_epoch_20260818",
        Digest.sha256(b"hardware-epoch"),
        Digest.sha256(path),
        ArtifactRef(
            "receiver-center-source",
            Digest.sha256(b"immutable-center-source"),
            SchemaRef("org.example.receiver-center-source"),
        ),
        center_hz,
        "absolute-cfo-relative-to-recording-if-center",
        True,
    )


def recording_ref(
    recording_id: RecordingId = DEFAULT_RECORDING_ID,
) -> RecordingObjectRef:
    return RecordingObjectRef(
        recording_id,
        ObjectRef(
            Digest.sha256(b"recording-data"),
            14,
            "application/vnd.sigmf.data",
            "sigmf-ci16-le-v1",
            "cas:sha256:" + Digest.sha256(b"recording-data").value,
        ),
        ObjectRef(
            Digest.sha256(b"recording-metadata"),
            18,
            "application/vnd.sigmf.meta+json",
            "sigmf-meta-v1",
            "cas:sha256:" + Digest.sha256(b"recording-metadata").value,
        ),
        Digest.sha256(b"recording-manifest"),
    )


def request(
    *,
    recording_object: RecordingObjectRef | None = None,
    receiver_chain_id: ReceiverChainId = DEFAULT_RECEIVER_CHAIN_ID,
    center: StarlinkReceiverFrequencyCenterV0_1 | None = None,
) -> StarlinkSymbolwiseReplayRequestV0_1:
    source = recording_object or recording_ref()
    selection = StarlinkSymbolwiseReplayStreamSelectionV0_1(
        RadioId("radio_pluto_5d4d"),
        SegmentId("seg_ch4_lower"),
        receiver_chain_id,
        StarlinkEdge.LOWER,
        SAMPLE_RATE_HZ,
        SEGMENT_SAMPLES,
        center or frequency_center(),
    )
    return StarlinkSymbolwiseReplayRequestV0_1(
        SchemaRef(StarlinkSymbolwiseReplayRequestV0_1.SCHEMA_ID, V0_1),
        source.recording_id,
        source,
        StarlinkSymbolwiseRecordingPlanV0_1(),
        (selection,),
        SchemaRef(StarlinkSymbolwiseRecordingBundleV0_1.SCHEMA_ID, V0_1),
    )


def receiver_bundle(
    replay_request: StarlinkSymbolwiseReplayRequestV0_1,
    selection_index: int = 0,
) -> StarlinkSymbolwiseReplayBundleV0_1:
    selection = replay_request.stream_selections[selection_index]
    patterns = tuple(_pattern(index) for index in range(5))
    controls = tuple(
        ArtifactRef(
            f"control-template-{index}",
            Digest.sha256(f"control-{index}".encode()),
            SchemaRef("org.example.symbolwise-control-template"),
        )
        for index in range(5)
    )
    evidence = tuple(
        _evidence(pattern, controls[index], index)
        for index, pattern in enumerate(patterns)
    )
    windows = tuple(
        StarlinkSymbolwiseWindowEvidenceV0_1(
            index,
            index * CADENCE_SAMPLES,
            index * CADENCE_SAMPLES + WINDOW_SAMPLES,
            Digest.sha256(f"window-{index}".encode()),
            evidence,
        )
        for index in range(600)
    )
    config_ref = starlink_symbolwise_replay_config_ref_v0_1(
        StarlinkSymbolwiseReplayConfigV0_1()
    )
    algorithm_ref = starlink_symbolwise_replay_algorithm_ref_v0_1()
    dependencies = (
        algorithm_ref.digest,
        selection.frequency_center.digest,
        selection.frequency_center.source_ref.digest,
        *(item.template_ref.digest for item in patterns),
        *(item.digest for item in controls),
    )
    return StarlinkSymbolwiseReplayBundleV0_1(
        SchemaRef(StarlinkSymbolwiseReplayBundleV0_1.SCHEMA_ID, CORE_V0_1),
        "slsymreplay_"
        + Digest.sha256(f"receiver-bundle-{selection.identity}".encode()).value[:32],
        replay_request.recording_id,
        replay_request.recording_object_ref.identity_digest(),
        selection.segment_id,
        selection.receiver_chain_id,
        selection.edge,
        selection.sample_rate_hz,
        selection.segment_sample_count,
        selection.frequency_center,
        algorithm_ref,
        config_ref,
        canonical_digest(patterns),
        WINDOW_SAMPLES,
        CADENCE_SAMPLES,
        windows,
        15_000_000,
        0.1,
        3_000,
        3_000,
        6_400_000,
        Provenance(
            "symbolwise-fixture",
            "0.1.0",
            "test-commit",
            Digest.sha256(b"test-environment"),
            config_ref.digest,
            (replay_request.recording_object_ref.identity_digest(),),
            dependencies,
            UtcNs(1_800_000_000_000_000_000),
            UtcNs(1_800_000_001_000_000_000),
            "test-host",
        ),
        True,
        (
            "legacy-parity-evidence-not-runtime-dependency",
            "finite-pattern-controls-not-empirical-null",
            "whole-search-calibration-required",
            "known-pilot-not-user-payload",
            "conditioned-roll17-is-not-pattern-symmetric-null",
            "receiver-center-is-explicit-calibration-input",
        ),
    )


def recording_bundle(
    replay_request: StarlinkSymbolwiseReplayRequestV0_1,
) -> StarlinkSymbolwiseRecordingBundleV0_1:
    streams = tuple(
        receiver_bundle(replay_request, index)
        for index in range(len(replay_request.stream_selections))
    )
    plan = replay_request.plan
    return StarlinkSymbolwiseRecordingBundleV0_1(
        SchemaRef(StarlinkSymbolwiseRecordingBundleV0_1.SCHEMA_ID, V0_1),
        "slsymrec_" + Digest.sha256(b"recording-bundle").value[:32],
        replay_request.recording_id,
        replay_request.recording_object_ref.identity_digest(),
        replay_request.digest,
        plan,
        replay_request.stream_selections,
        streams,
        Provenance(
            "symbolwise-fixture",
            "0.1.0",
            "test-commit",
            Digest.sha256(b"test-environment"),
            plan.digest,
            (
                replay_request.recording_object_ref.identity_digest(),
                replay_request.digest,
            ),
            tuple(stream.digest for stream in streams),
            UtcNs(1_800_000_000_000_000_000),
            UtcNs(1_800_000_001_000_000_000),
            "test-host",
        ),
        True,
        (
            "finite-pattern-controls-not-empirical-null",
            "whole-search-calibration-required",
            "explicit-on-demand-or-backfill-only",
            "candidate-evidence-not-calibrated-detection",
        ),
    )


def _pattern(index: int) -> StarlinkSearchPatternV0_1:
    is_qin = index == 0
    label = "qin" if is_qin else f"surrogate-{index - 1}"
    return StarlinkSearchPatternV0_1(
        SchemaRef(StarlinkSearchPatternV0_1.SCHEMA_ID, CORE_V0_1),
        f"pattern-{label}",
        (
            StarlinkSearchPatternRole.QIN_EXACT
            if is_qin
            else StarlinkSearchPatternRole.PRECOMMITTED_SURROGATE
        ),
        ArtifactRef(
            f"template-{label}",
            Digest.sha256(f"template-{label}".encode()),
            SchemaRef("org.example.symbolwise-template"),
        ),
        StarlinkEdge.LOWER,
        tuple(range(528, 536)),
        2,
        301,
        750.0,
        SAMPLE_RATE_HZ,
        3333,
        1.0,
        Digest.sha256(f"states-{label}".encode()),
        "qin-reference" if is_qin else "precommitted-qpsk-pcg64-v1",
        None if is_qin else 10_000 + index,
        None if is_qin else index - 1,
        True,
    )


def _evidence(
    pattern: StarlinkSearchPatternV0_1,
    control: ArtifactRef,
    index: int,
) -> StarlinkSymbolwisePatternEvidenceV0_1:
    score = 0.75 - index * 0.05
    control_score = score - 0.1
    conditioned = score - 0.05
    conditioned_control = conditioned - 0.1
    return StarlinkSymbolwisePatternEvidenceV0_1(
        pattern,
        control,
        1,
        1,
        1,
        0,
        0,
        600_000.0,
        score,
        0.2,
        3.0,
        8,
        600_000.0,
        2_000.0,
        602_000.0,
        score,
        control_score,
        0.1,
        score,
        control_score,
        conditioned,
        conditioned_control,
        0.1,
        conditioned,
        conditioned_control,
        8,
        300,
        score,
    )
