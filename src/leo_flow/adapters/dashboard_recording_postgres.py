"""PostgreSQL projection publication and reads for recording capture pages."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb

from leo_flow.application.starlink_projection_work import StarlinkProjectionLeaseV0_1
from leo_flow.application.starlink_suite_projection_work import (
    StarlinkSuiteProjectionLeaseV0_2,
)
from leo_flow.contracts.capture import ActivityKind, GainMode, RecordingManifest
from leo_flow.contracts.core import (
    V0_1,
    ActivityId,
    AnalysisRunId,
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.dashboard_recording import (
    RecordingCaptureDetailViewV0_1,
    RecordingSegmentViewV0_1,
)
from leo_flow.contracts.dashboard_waterfall import (
    RecordingWaterfallViewV0_1,
    WaterfallProjectionState,
    WaterfallTileViewV0_1,
)
from leo_flow.contracts.starlink import (
    RecordingStarlinkDecisionViewV0_1,
    StarlinkEdge,
    StarlinkPilotAnalysisBundleV0_1,
    StarlinkRecordingDecisionState,
)
from leo_flow.contracts.starlink_detector_suite import V0_2, StarlinkDetectorMethod
from leo_flow.contracts.starlink_pipeline import (
    RecordingStarlinkCandidateViewV0_1,
    StarlinkCandidateSummaryV0_1,
    StarlinkPilotAnalysisProductRefV0_1,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    RecordingStarlinkSuiteViewV0_2,
    StarlinkDetectorSuiteProductRefV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
    StarlinkMethodComparisonV0_2,
    StarlinkSuiteRecordingState,
)
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.contracts.waterfall import (
    WaterfallBundleV0_1,
    WaterfallProductRefV0_1,
)
from leo_flow.dashboard import DashboardNotFound

from . import dashboard_recording_postgres_sql as sql

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresRecordingCaptureDetailProjectionWriter:
    """Publish capture-owned manifest facts through the sole write routine."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(self, view: RecordingCaptureDetailViewV0_1) -> int:
        with self._connect() as connection:
            return publish_recording_capture_detail(connection, view)


class PostgresRecordingWaterfallProjectionWriter:
    """Project a verified durable waterfall without retaining its locator."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def project_complete(
        self, bundle: WaterfallBundleV0_1, ref: WaterfallProductRefV0_1
    ) -> int:
        return self.publish(recording_waterfall_view_v0_1(bundle, ref))

    def publish(self, view: RecordingWaterfallViewV0_1) -> int:
        payload = json.loads(canonical_json_bytes(view))
        with self._connect() as connection:
            row = connection.execute(
                sql.PUBLISH_RECORDING_WATERFALL_SQL, {"view": Jsonb(payload)}
            ).fetchone()
        if row is None:
            raise RuntimeError("recording waterfall projection returned no receipt")
        return _integer(row["projection_sequence"], "projection_sequence")


class PostgresRecordingStarlinkProjectionWriter:
    """Project bounded candidates without exposing their CAS object."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def project_candidates(
        self,
        bundle: StarlinkPilotAnalysisBundleV0_1,
        ref: StarlinkPilotAnalysisProductRefV0_1,
        lease: StarlinkProjectionLeaseV0_1,
    ) -> int:
        view = recording_starlink_candidate_view_v0_1(bundle, ref)
        payload = json.loads(canonical_json_bytes(view))
        with self._connect() as connection:
            row = connection.execute(
                sql.PUBLISH_RECORDING_STARLINK_SQL,
                {
                    "view": Jsonb(payload),
                    "work_id": lease.work_id,
                    "lease_token": lease.lease_token,
                    "lease_generation": lease.lease_generation,
                },
            ).fetchone()
        if row is None:
            raise RuntimeError("recording Starlink projection returned no receipt")
        return _integer(row["projection_sequence"], "projection_sequence")


class PostgresRecordingStarlinkSuiteProjectionWriterV0_2:
    """Project all report-method comparisons or an explicit terminal skip."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def project_suite(
        self,
        bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
        ref: StarlinkDetectorSuiteProductRefV0_2,
        lease: StarlinkSuiteProjectionLeaseV0_2,
    ) -> int:
        view = recording_starlink_suite_view_v0_2(bundle, ref)
        payload = json.loads(canonical_json_bytes(view))
        with self._connect() as connection:
            row = connection.execute(
                sql.PUBLISH_RECORDING_STARLINK_SUITE_SQL,
                {
                    "view": Jsonb(payload),
                    "work_id": lease.work_id,
                    "lease_token": lease.lease_token,
                    "lease_generation": lease.lease_generation,
                },
            ).fetchone()
        if row is None:
            raise RuntimeError("detector-suite projection returned no receipt")
        return _integer(row["projection_sequence"], "projection_sequence")


class PostgresRecordingDashboardRepository:
    """Read only dashboard-owned JSON projections for one exact recording."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def recording_capture_detail(
        self, recording_id: RecordingId
    ) -> RecordingCaptureDetailViewV0_1:
        with self._reader() as connection:
            row = connection.execute(
                sql.EXACT_RECORDING_DETAIL_SQL,
                {"recording_id": str(recording_id)},
            ).fetchone()
        if row is None:
            raise DashboardNotFound(
                f"capture detail for recording {recording_id} was not found"
            )
        document = dict(_mapping(row["semantic_view"], "recording detail"))
        document["analysis_state"] = str(row["analysis_state"])
        document["recording_object_available"] = _boolean(
            row["recording_object_available"], "recording_object_available"
        )
        return _capture_detail(document)

    def recording_waterfall(
        self, recording_id: RecordingId
    ) -> RecordingWaterfallViewV0_1:
        with self._reader() as connection:
            row = connection.execute(
                sql.EXACT_RECORDING_WATERFALL_SQL,
                {"recording_id": str(recording_id)},
            ).fetchone()
        if row is None:
            raise DashboardNotFound(
                f"waterfall for recording {recording_id} was not found"
            )
        return _waterfall(_mapping(row["semantic_view"], "recording waterfall"))

    def recording_starlink_decision(
        self, recording_id: RecordingId
    ) -> RecordingStarlinkCandidateViewV0_1:
        with self._reader() as connection:
            row = connection.execute(
                sql.EXACT_RECORDING_STARLINK_SQL,
                {"recording_id": str(recording_id)},
            ).fetchone()
        if row is None:
            raise DashboardNotFound(
                f"Starlink candidates for recording {recording_id} were not found"
            )
        return _starlink_view(_mapping(row["semantic_view"], "recording Starlink"))

    def recording_starlink_suite(
        self, recording_id: RecordingId
    ) -> RecordingStarlinkSuiteViewV0_2:
        with self._reader() as connection:
            row = connection.execute(
                sql.EXACT_RECORDING_STARLINK_SUITE_SQL,
                {"recording_id": str(recording_id)},
            ).fetchone()
        if row is None:
            raise DashboardNotFound(
                f"Starlink detector suite for recording {recording_id} was not found"
            )
        return _starlink_suite_view(
            _mapping(row["semantic_view"], "recording Starlink detector suite")
        )

    @contextmanager
    def _reader(self) -> Iterator[psycopg.Connection[dict[str, object]]]:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            row = connection.execute("SHOW transaction_read_only").fetchone()
            if row is None or row["transaction_read_only"] != "on":
                raise RuntimeError("dashboard transaction is not read-only")
            yield connection


def publish_recording_capture_detail(
    connection: psycopg.Connection[dict[str, object]],
    view: RecordingCaptureDetailViewV0_1,
) -> int:
    payload = json.loads(canonical_json_bytes(view))
    row = connection.execute(
        sql.PUBLISH_RECORDING_DETAIL_SQL, {"view": Jsonb(payload)}
    ).fetchone()
    if row is None:
        raise RuntimeError("recording capture detail projection returned no receipt")
    return _integer(row["projection_sequence"], "projection_sequence")


def recording_capture_detail_view_v0_1(
    manifest: RecordingManifest,
    published: PublishedRecordingRef,
    *,
    analysis_state: str,
    recording_object_available: bool,
) -> RecordingCaptureDetailViewV0_1:
    """Map an exact manifest; never discover it from a locator or path."""

    recording = published.recording_object
    if manifest.recording_id != recording.recording_id:
        raise ValueError("manifest and published recording IDs differ")
    if recording.manifest_digest != Digest.sha256(canonical_json_bytes(manifest)):
        raise ValueError("manifest does not match the published digest")
    activity_by_segment = {
        segment_id: (activity.activity_id, activity.kind)
        for activity in manifest.activities
        for segment_id in activity.segment_ids
    }
    segments = tuple(
        sorted(
            (
                RecordingSegmentViewV0_1(
                    segment.segment_id,
                    activity_by_segment[segment.segment_id][0],
                    activity_by_segment[segment.segment_id][1],
                    segment.requested.receiver_chain_ids,
                    segment.start_utc_ns,
                    UtcNs(
                        int(segment.start_utc_ns)
                        + round(
                            segment.sample_count
                            * 1_000_000_000
                            / segment.actual_sample_rate_hz
                        )
                    ),
                    segment.actual_center_frequency_hz,
                    segment.actual_sample_rate_hz,
                    segment.actual_bandwidth_hz,
                    segment.actual_gain.mode,
                    segment.actual_gain.gain_db,
                    segment.sample_count,
                )
                for segment in manifest.segments
            ),
            key=lambda item: (int(item.started_utc_ns), str(item.segment_id)),
        )
    )
    return RecordingCaptureDetailViewV0_1(
        SchemaRef(RecordingCaptureDetailViewV0_1.SCHEMA_ID, V0_1),
        manifest.recording_id,
        manifest.plan_id,
        manifest.station_id,
        manifest.radio_id,
        manifest.radio_serial,
        manifest.hardware_metadata_snapshot_id,
        manifest.producer,
        manifest.clock_status,
        manifest.capture_started_utc_ns,
        manifest.capture_finished_utc_ns,
        analysis_state,
        recording_object_available,
        recording.manifest_digest,
        manifest.sample_dtype,
        manifest.sample_layout,
        segments,
    )


def recording_waterfall_view_v0_1(
    bundle: WaterfallBundleV0_1, ref: WaterfallProductRefV0_1
) -> RecordingWaterfallViewV0_1:
    if (
        bundle.product_id != ref.product_id
        or bundle.analysis_run_id != ref.analysis_run_id
        or bundle.recording_id != ref.recording_id
    ):
        raise ValueError("waterfall bundle and durable reference differ")
    tiles: list[WaterfallTileViewV0_1] = []
    for tile in bundle.tiles:
        values = tuple(value for row in tile.time_bins for value in row.power_db)
        floor = min(values)
        ceiling = max(values)
        if floor == ceiling:
            floor -= 1.0
        tiles.append(
            WaterfallTileViewV0_1(
                tile.segment_id,
                tile.receiver_chain_id,
                tile.segment_start_utc_ns,
                tile.segment_sample_count,
                tile.center_frequency_hz,
                tile.sample_rate_hz,
                tile.fft_window_samples,
                tuple(row.start_sample for row in tile.time_bins),
                tuple(row.stop_sample for row in tile.time_bins),
                tuple(row.midpoint_utc_ns for row in tile.time_bins),
                tile.frequency_bin_offsets_hz,
                tuple(row.power_db for row in tile.time_bins),
                tile.power_reference,
                floor,
                ceiling,
            )
        )
    tiles.sort(key=lambda item: (str(item.segment_id), str(item.receiver_chain_id)))
    return RecordingWaterfallViewV0_1(
        SchemaRef(RecordingWaterfallViewV0_1.SCHEMA_ID, V0_1),
        bundle.recording_id,
        bundle.input_recording_identity_digest,
        bundle.analysis_run_id,
        WaterfallProjectionState.COMPLETE,
        None,
        tuple(tiles),
    )


def recording_starlink_candidate_view_v0_1(
    bundle: StarlinkPilotAnalysisBundleV0_1,
    ref: StarlinkPilotAnalysisProductRefV0_1,
) -> RecordingStarlinkCandidateViewV0_1:
    if (
        bundle.analysis_id != ref.analysis_id
        or bundle.recording_id != ref.recording_id
        or not bundle.candidates
    ):
        raise ValueError("Starlink bundle and durable reference differ")
    candidates = tuple(
        StarlinkCandidateSummaryV0_1(
            item.candidate_id,
            item.segment_id,
            item.receiver_chain_id,
            item.edge,
            item.search_identity_digest,
            item.winning_epoch_sample,
            item.winning_cfo_hz,
            item.search_cell_count,
            item.frame_support,
            item.conditioned_exact_score,
            item.conditioned_control_score,
            item.exact_minus_control_margin,
            item.pss_evidence_status,
        )
        for item in sorted(
            bundle.candidates,
            key=lambda value: (str(value.segment_id), str(value.receiver_chain_id)),
        )
    )
    decision = RecordingStarlinkDecisionViewV0_1(
        SchemaRef(RecordingStarlinkDecisionViewV0_1.SCHEMA_ID, V0_1),
        bundle.recording_id,
        StarlinkRecordingDecisionState.CANDIDATES,
        len({(item.segment_id, item.receiver_chain_id) for item in bundle.candidates}),
        len(bundle.candidates),
        None,
        ArtifactRef(bundle.analysis_id, ref.bundle_ref.digest, bundle.schema),
        tuple(sorted(set(bundle.warnings + ("whole-search-calibration-required",)))),
    )
    return RecordingStarlinkCandidateViewV0_1(
        SchemaRef(RecordingStarlinkCandidateViewV0_1.SCHEMA_ID, V0_1),
        decision,
        candidates,
    )


def recording_starlink_suite_view_v0_2(
    bundle: StarlinkDetectorSuiteRecordingBundleV0_2,
    ref: StarlinkDetectorSuiteProductRefV0_2,
) -> RecordingStarlinkSuiteViewV0_2:
    if bundle.analysis_id != ref.analysis_id or bundle.recording_id != ref.recording_id:
        raise ValueError("detector-suite bundle and durable reference differ")
    methods = tuple(
        StarlinkMethodComparisonV0_2(
            suite.segment_id,
            suite.receiver_chain_id,
            suite.edge,
            evidence.method,
            evidence.reported_score,
            evidence.conditioned_control_score,
            evidence.exact_minus_control_margin,
            evidence.winning_epoch_sample,
            evidence.winning_coarse_cfo_hz,
            evidence.winning_residual_cfo_hz,
            evidence.effective_search_cell_count,
            evidence.exact_frames.support,
        )
        for suite in bundle.suites
        for evidence in suite.methods
    )
    return RecordingStarlinkSuiteViewV0_2(
        SchemaRef(RecordingStarlinkSuiteViewV0_2.SCHEMA_ID, V0_2),
        bundle.recording_id,
        bundle.state,
        ArtifactRef(bundle.analysis_id, ref.bundle_ref.digest, bundle.schema),
        len(bundle.suites),
        len(methods),
        None,
        bundle.reason_codes,
        methods,
    )


def _capture_detail(value: Mapping[str, object]) -> RecordingCaptureDetailViewV0_1:
    schema = _schema(value.get("schema"), RecordingCaptureDetailViewV0_1.SCHEMA_ID)
    segments = _array(value.get("segments"), "segments")
    return RecordingCaptureDetailViewV0_1(
        schema,
        RecordingId(_string(value.get("recording_id"), "recording_id")),
        PlanId(_string(value.get("plan_id"), "plan_id")),
        StationId(_string(value.get("station_id"), "station_id")),
        RadioId(_string(value.get("radio_id"), "radio_id")),
        _string(value.get("radio_serial"), "radio_serial"),
        HardwareSnapshotId(
            _string(value.get("hardware_snapshot_id"), "hardware_snapshot_id")
        ),
        _string(value.get("producer"), "producer"),
        _string(value.get("clock_status"), "clock_status"),
        UtcNs(_integer(value.get("capture_started_utc_ns"), "capture_started_utc_ns")),
        UtcNs(
            _integer(value.get("capture_finished_utc_ns"), "capture_finished_utc_ns")
        ),
        _string(value.get("analysis_state"), "analysis_state"),
        _boolean(value.get("recording_object_available"), "recording_object_available"),
        _digest(value.get("manifest_digest"), "manifest_digest"),
        _string(value.get("sample_dtype"), "sample_dtype"),
        _sample_layout(value.get("sample_layout")),
        tuple(_segment(_mapping(item, "segment")) for item in segments),
    )


def _segment(value: Mapping[str, object]) -> RecordingSegmentViewV0_1:
    gain_db = value.get("gain_db")
    return RecordingSegmentViewV0_1(
        SegmentId(_string(value.get("segment_id"), "segment_id")),
        ActivityId(_string(value.get("activity_id"), "activity_id")),
        ActivityKind(_string(value.get("activity_kind"), "activity_kind")),
        tuple(
            ReceiverChainId(_string(item, "receiver_chain_id"))
            for item in _array(value.get("receiver_chain_ids"), "receiver_chain_ids")
        ),
        UtcNs(_integer(value.get("started_utc_ns"), "started_utc_ns")),
        UtcNs(_integer(value.get("finished_utc_ns"), "finished_utc_ns")),
        _number(value.get("center_frequency_hz"), "center_frequency_hz"),
        _number(value.get("sample_rate_hz"), "sample_rate_hz"),
        _number(value.get("bandwidth_hz"), "bandwidth_hz"),
        GainMode(_string(value.get("gain_mode"), "gain_mode")),
        None if gain_db is None else _number(gain_db, "gain_db"),
        _integer(value.get("sample_count"), "sample_count"),
    )


def _waterfall(value: Mapping[str, object]) -> RecordingWaterfallViewV0_1:
    run_id = value.get("analysis_run_id")
    reason = value.get("reason_code")
    return RecordingWaterfallViewV0_1(
        _schema(value.get("schema"), RecordingWaterfallViewV0_1.SCHEMA_ID),
        RecordingId(_string(value.get("recording_id"), "recording_id")),
        _digest(value.get("recording_identity_digest"), "recording_identity_digest"),
        None if run_id is None else AnalysisRunId(_string(run_id, "analysis_run_id")),
        WaterfallProjectionState(_string(value.get("state"), "state")),
        None if reason is None else _string(reason, "reason_code"),
        tuple(
            _waterfall_tile(_mapping(item, "waterfall tile"))
            for item in _array(value.get("tiles"), "tiles")
        ),
    )


def _waterfall_tile(value: Mapping[str, object]) -> WaterfallTileViewV0_1:
    return WaterfallTileViewV0_1(
        SegmentId(_string(value.get("segment_id"), "segment_id")),
        ReceiverChainId(_string(value.get("receiver_chain_id"), "receiver_chain_id")),
        UtcNs(_integer(value.get("segment_start_utc_ns"), "segment_start_utc_ns")),
        _integer(value.get("segment_sample_count"), "segment_sample_count"),
        _number(value.get("center_frequency_hz"), "center_frequency_hz"),
        _number(value.get("sample_rate_hz"), "sample_rate_hz"),
        _integer(value.get("fft_window_samples"), "fft_window_samples"),
        tuple(
            _integer(item, "time_bin_start_samples")
            for item in _array(
                value.get("time_bin_start_samples"), "time_bin_start_samples"
            )
        ),
        tuple(
            _integer(item, "time_bin_stop_samples")
            for item in _array(
                value.get("time_bin_stop_samples"), "time_bin_stop_samples"
            )
        ),
        tuple(
            UtcNs(_integer(item, "time_bin_midpoint_utc_ns"))
            for item in _array(
                value.get("time_bin_midpoint_utc_ns"), "time_bin_midpoint_utc_ns"
            )
        ),
        tuple(
            _number(item, "frequency_bin_offsets_hz")
            for item in _array(
                value.get("frequency_bin_offsets_hz"), "frequency_bin_offsets_hz"
            )
        ),
        tuple(
            tuple(_number(item, "power_db") for item in _array(row, "power row"))
            for row in _array(value.get("power_db"), "power_db")
        ),
        _string(value.get("power_reference"), "power_reference"),
        _number(value.get("floor_db"), "floor_db"),
        _number(value.get("ceiling_db"), "ceiling_db"),
    )


def _starlink_view(value: Mapping[str, object]) -> RecordingStarlinkCandidateViewV0_1:
    decision_doc = _mapping(value.get("decision"), "Starlink decision")
    analysis_doc = _mapping(decision_doc.get("analysis_ref"), "analysis_ref")
    analysis_schema = _mapping(analysis_doc.get("schema"), "analysis_ref.schema")
    detection_count = decision_doc.get("calibrated_detection_count")
    decision = RecordingStarlinkDecisionViewV0_1(
        _schema(
            decision_doc.get("schema"), RecordingStarlinkDecisionViewV0_1.SCHEMA_ID
        ),
        RecordingId(_string(decision_doc.get("recording_id"), "recording_id")),
        StarlinkRecordingDecisionState(
            _string(decision_doc.get("state"), "Starlink state")
        ),
        _integer(decision_doc.get("analyzed_stream_count"), "stream count"),
        _integer(decision_doc.get("search_candidate_count"), "candidate count"),
        None
        if detection_count is None
        else _integer(detection_count, "detection count"),
        ArtifactRef(
            _string(analysis_doc.get("artifact_id"), "analysis artifact_id"),
            _digest(analysis_doc.get("digest"), "analysis digest"),
            _schema(
                analysis_schema,
                StarlinkPilotAnalysisBundleV0_1.SCHEMA_ID,
            ),
        ),
        tuple(
            _string(item, "reason code")
            for item in _array(decision_doc.get("reason_codes"), "reason_codes")
        ),
    )
    candidates = tuple(
        StarlinkCandidateSummaryV0_1(
            _string(item.get("candidate_id"), "candidate_id"),
            SegmentId(_string(item.get("segment_id"), "segment_id")),
            ReceiverChainId(
                _string(item.get("receiver_chain_id"), "receiver_chain_id")
            ),
            StarlinkEdge(_string(item.get("edge"), "edge")),
            _digest(item.get("search_identity_digest"), "search_identity_digest"),
            _integer(item.get("winning_epoch_sample"), "winning_epoch_sample"),
            _number(item.get("winning_cfo_hz"), "winning_cfo_hz"),
            _integer(item.get("search_cell_count"), "search_cell_count"),
            _integer(item.get("frame_support"), "frame_support"),
            _number(item.get("exact_score"), "exact_score"),
            _number(item.get("conditioned_control_score"), "conditioned_control_score"),
            _number(
                item.get("exact_minus_control_margin"),
                "exact_minus_control_margin",
            ),
            _string(item.get("pss_evidence_status"), "pss_evidence_status"),
        )
        for item in (
            _mapping(value, "Starlink candidate")
            for value in _array(value.get("candidates"), "Starlink candidates")
        )
    )
    return RecordingStarlinkCandidateViewV0_1(
        _schema(value.get("schema"), RecordingStarlinkCandidateViewV0_1.SCHEMA_ID),
        decision,
        candidates,
    )


def _starlink_suite_view(value: Mapping[str, object]) -> RecordingStarlinkSuiteViewV0_2:
    analysis = _mapping(value.get("analysis_ref"), "analysis_ref")
    methods = tuple(
        StarlinkMethodComparisonV0_2(
            SegmentId(_string(item.get("segment_id"), "segment_id")),
            ReceiverChainId(
                _string(item.get("receiver_chain_id"), "receiver_chain_id")
            ),
            StarlinkEdge(_string(item.get("edge"), "edge")),
            StarlinkDetectorMethod(_string(item.get("method"), "method")),
            _number(item.get("score"), "score"),
            _number(item.get("control_score"), "control_score"),
            _number(item.get("margin"), "margin"),
            _integer(item.get("epoch_sample"), "epoch_sample"),
            _number(item.get("coarse_cfo_hz"), "coarse_cfo_hz"),
            _number(item.get("residual_cfo_hz"), "residual_cfo_hz"),
            _integer(item.get("effective_search_cell_count"), "search cells"),
            _integer(item.get("frame_support"), "frame support"),
        )
        for item in (
            _mapping(entry, "detector-suite method")
            for entry in _array(value.get("methods"), "methods")
        )
    )
    return RecordingStarlinkSuiteViewV0_2(
        _schema_v0_2(value.get("schema"), RecordingStarlinkSuiteViewV0_2.SCHEMA_ID),
        RecordingId(_string(value.get("recording_id"), "recording_id")),
        StarlinkSuiteRecordingState(_string(value.get("state"), "state")),
        ArtifactRef(
            _string(analysis.get("artifact_id"), "analysis_id"),
            _digest(analysis.get("digest"), "analysis digest"),
            _schema_v0_2(
                analysis.get("schema"),
                StarlinkDetectorSuiteRecordingBundleV0_2.SCHEMA_ID,
            ),
        ),
        _integer(value.get("analyzed_stream_count"), "stream count"),
        _integer(value.get("method_count"), "method count"),
        None,
        tuple(
            _string(item, "reason code")
            for item in _array(value.get("reason_codes"), "reason codes")
        ),
        methods,
    )


def _schema(value: object, expected: str) -> SchemaRef:
    document = _mapping(value, "schema")
    version = _mapping(document.get("version"), "schema version")
    if (
        _integer(version.get("major"), "schema major") != 0
        or _integer(version.get("minor"), "schema minor") != 1
    ):
        raise TypeError("database schema version is unsupported")
    schema_id = _string(document.get("schema_id"), "schema_id")
    if schema_id != expected:
        raise TypeError("database schema identity is unsupported")
    return SchemaRef(schema_id, V0_1)


def _schema_v0_2(value: object, expected: str) -> SchemaRef:
    document = _mapping(value, "schema")
    version = _mapping(document.get("version"), "schema version")
    if (
        _integer(version.get("major"), "schema major"),
        _integer(version.get("minor"), "schema minor"),
    ) != (0, 2):
        raise TypeError("database schema version is unsupported")
    schema_id = _string(document.get("schema_id"), "schema_id")
    if schema_id != expected:
        raise TypeError("database schema identity is unsupported")
    return SchemaRef(schema_id, V0_2)


def _sample_layout(value: object) -> tuple[str, str, str]:
    items = tuple(
        _string(item, "sample_layout") for item in _array(value, "sample_layout")
    )
    if len(items) != 3:
        raise TypeError("database sample_layout is invalid")
    return items[0], items[1], items[2]


def _digest(value: object, field: str) -> Digest:
    document = _mapping(value, field)
    return Digest(
        DigestAlgorithm(_string(document.get("algorithm"), f"{field}.algorithm")),
        _string(document.get("value"), f"{field}.value"),
    )


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"database {field} is invalid")
    return value


def _array(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"database {field} is invalid")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"database {field} is invalid")
    return value


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"database {field} is invalid")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"database {field} is invalid")
    result = float(value)
    if not math.isfinite(result):
        raise TypeError(f"database {field} is invalid")
    return result


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"database {field} is invalid")
    return value
