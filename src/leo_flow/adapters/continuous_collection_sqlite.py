"""Full-sync SQLite journal for deferred continuous campaign collection."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from leo_flow.capture.campaign import (
    CampaignAnalysisReceipt,
    CampaignAnalysisSuccess,
    CampaignDefinition,
    build_campaign_unit,
)
from leo_flow.capture.continuous import (
    ContinuousCollectionHaltReason,
    ContinuousCollectionPhase,
    ContinuousCollectionRecord,
    ContinuousCollectionRecordPhase,
    ContinuousCollectionState,
)
from leo_flow.contracts.capture_batch_codec import (
    decode_capture_batch_snapshot,
    encode_capture_batch_snapshot,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    CaptureBatchId,
    Digest,
    DigestAlgorithm,
    FeatureSetId,
    JobId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.storage import ObjectRef

MAX_CONTINUOUS_COLLECTION_STATE_BYTES = 16 * 1024 * 1024


class SQLiteContinuousCollectionJournal:
    """One definition-bound collection with full-sync CAS transitions."""

    def __init__(
        self, database_path: Path, *, now_utc_ns: Callable[[], int] = time.time_ns
    ) -> None:
        if not database_path.is_absolute() or ".." in database_path.parts:
            raise ValueError("continuous journal path must be absolute and normalized")
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._now = now_utc_ns
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS continuous_collection_journal (
                    campaign_id TEXT PRIMARY KEY,
                    definition_digest TEXT NOT NULL,
                    state_payload BLOB NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    updated_utc_ns INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self, definition: CampaignDefinition) -> ContinuousCollectionState:
        initial = ContinuousCollectionState(definition.digest)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM continuous_collection_journal WHERE campaign_id = ?",
                (definition.campaign_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO continuous_collection_journal(
                        campaign_id, definition_digest, state_payload,
                        revision, updated_utc_ns
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        definition.campaign_id,
                        str(definition.digest),
                        _encode_state(initial),
                        initial.revision,
                        self._now(),
                    ),
                )
                return initial
        return self._decode_row(definition, row)

    def load(self, definition: CampaignDefinition) -> ContinuousCollectionState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM continuous_collection_journal WHERE campaign_id = ?",
                (definition.campaign_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("continuous collection journal is not initialized")
        return self._decode_row(definition, row)

    def compare_and_swap(
        self,
        definition: CampaignDefinition,
        expected_revision: int,
        replacement: ContinuousCollectionState,
    ) -> ContinuousCollectionState:
        if (
            replacement.definition_digest != definition.digest
            or replacement.revision != expected_revision + 1
        ):
            raise RuntimeError("continuous collection replacement is invalid")
        payload = _encode_state(replacement)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE continuous_collection_journal
                   SET state_payload = ?, revision = ?, updated_utc_ns = ?
                 WHERE campaign_id = ? AND definition_digest = ? AND revision = ?
                """,
                (
                    payload,
                    replacement.revision,
                    self._now(),
                    definition.campaign_id,
                    str(definition.digest),
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("continuous collection revision changed")
        return replacement

    @staticmethod
    def _decode_row(
        definition: CampaignDefinition, row: sqlite3.Row
    ) -> ContinuousCollectionState:
        try:
            if row["definition_digest"] != str(definition.digest):
                raise ValueError("definition digest differs")
            state = _decode_state(definition, bytes(row["state_payload"]))
            if state.revision != row["revision"]:
                raise ValueError("revision differs")
            return state
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "continuous collection journal failed integrity validation"
            ) from error


def _encode_state(state: ContinuousCollectionState) -> bytes:
    payload = canonical_json_bytes(
        {
            "definition_digest": str(state.definition_digest),
            "phase": state.phase.value,
            "halt_reason": (
                state.halt_reason.value if state.halt_reason is not None else None
            ),
            "revision": state.revision,
            "records": [
                {
                    "success_index": item.unit.success_index,
                    "slot_index": item.unit.slot_index,
                    "retry_index": item.unit.retry_index,
                    "requested_start_utc_ns": int(item.unit.requested_start_utc_ns),
                    "unit_digest": str(item.unit.digest),
                    "phase": item.phase.value,
                    "capture_invocations": item.capture_invocations,
                    "analysis_invocations": item.analysis_invocations,
                    "snapshot": (
                        json.loads(encode_capture_batch_snapshot(item.snapshot))
                        if item.snapshot is not None
                        else None
                    ),
                    "analysis_receipt": (
                        json.loads(canonical_json_bytes(item.analysis_receipt))
                        if item.analysis_receipt is not None
                        else None
                    ),
                }
                for item in state.records
            ],
        }
    )
    if len(payload) > MAX_CONTINUOUS_COLLECTION_STATE_BYTES:
        raise RuntimeError("continuous collection state exceeds the size limit")
    return payload


def _decode_state(
    definition: CampaignDefinition, payload: bytes
) -> ContinuousCollectionState:
    if len(payload) > MAX_CONTINUOUS_COLLECTION_STATE_BYTES:
        raise ValueError("continuous collection state exceeds the size limit")
    root = _mapping(json.loads(payload), "continuous collection state")
    if set(root) != {
        "definition_digest",
        "phase",
        "halt_reason",
        "revision",
        "records",
    }:
        raise ValueError("continuous collection state fields differ")
    digest = _digest(root["definition_digest"])
    revision = _integer(root["revision"])
    values = root["records"]
    if digest != definition.digest or revision < 0 or not isinstance(values, list):
        raise ValueError("continuous collection state values are invalid")
    records: list[ContinuousCollectionRecord] = []
    expected_record_fields = {
        "success_index",
        "slot_index",
        "retry_index",
        "requested_start_utc_ns",
        "unit_digest",
        "phase",
        "capture_invocations",
        "analysis_invocations",
        "snapshot",
        "analysis_receipt",
    }
    for value in values:
        item = _mapping(value, "continuous collection record")
        if set(item) != expected_record_fields:
            raise ValueError("continuous collection record fields differ")
        unit = build_campaign_unit(
            definition,
            success_index=_integer(item["success_index"]),
            slot_index=_integer(item["slot_index"]),
            retry_index=_integer(item["retry_index"]),
            requested_start_utc_ns=UtcNs(_integer(item["requested_start_utc_ns"])),
        )
        if str(unit.digest) != item["unit_digest"]:
            raise ValueError("continuous collection unit digest differs")
        snapshot_value = item["snapshot"]
        snapshot = (
            None
            if snapshot_value is None
            else decode_capture_batch_snapshot(canonical_json_bytes(snapshot_value))
        )
        if snapshot is not None and snapshot.definition != unit.batch:
            raise ValueError("continuous collection snapshot identifies another unit")
        receipt_value = item["analysis_receipt"]
        receipt = (
            None
            if receipt_value is None
            else _analysis_receipt(_mapping(receipt_value, "analysis receipt"))
        )
        if receipt is not None and receipt.batch_id != unit.batch.batch_id:
            raise ValueError("continuous analysis identifies another unit")
        capture_invocations = _integer(item["capture_invocations"])
        analysis_invocations = _integer(item["analysis_invocations"])
        if capture_invocations < 0 or analysis_invocations < 0:
            raise ValueError("continuous invocation count is invalid")
        records.append(
            ContinuousCollectionRecord(
                unit,
                ContinuousCollectionRecordPhase(_string(item["phase"])),
                capture_invocations,
                analysis_invocations,
                snapshot,
                receipt,
            )
        )
    reason = root["halt_reason"]
    state = ContinuousCollectionState(
        digest,
        ContinuousCollectionPhase(_string(root["phase"])),
        tuple(records),
        (None if reason is None else ContinuousCollectionHaltReason(_string(reason))),
        revision,
    )
    if _encode_state(state) != payload:
        raise ValueError("continuous collection state is not canonical")
    return state


def _analysis_receipt(value: Mapping[str, Any]) -> CampaignAnalysisReceipt:
    if set(value) != {"batch_id", "successes", "completed_utc_ns"}:
        raise ValueError("analysis receipt fields differ")
    successes_value = value["successes"]
    if not isinstance(successes_value, list) or len(successes_value) != 2:
        raise ValueError("analysis receipt requires two successes")
    successes = tuple(
        _analysis_success(_mapping(item, "analysis success"))
        for item in successes_value
    )
    return CampaignAnalysisReceipt(
        CaptureBatchId(_string(value["batch_id"])),
        (successes[0], successes[1]),
        UtcNs(_integer(value["completed_utc_ns"])),
    )


def _analysis_success(value: Mapping[str, Any]) -> CampaignAnalysisSuccess:
    if set(value) != {
        "recording_id",
        "analysis_job_id",
        "result_ref",
        "projection_work_id",
        "projected_feature_set_ref",
        "projected_utc_ns",
    }:
        raise ValueError("analysis success fields differ")
    result = _mapping(value["result_ref"], "result ref")
    feature = _mapping(value["projected_feature_set_ref"], "feature ref")
    if set(result) != {"artifact_id", "digest", "schema"} or set(feature) != {
        "feature_set_id",
        "analysis_run_id",
        "bundle_ref",
    }:
        raise ValueError("analysis success reference fields differ")
    schema_value = result["schema"]
    return CampaignAnalysisSuccess(
        RecordingId(_string(value["recording_id"])),
        JobId(_string(value["analysis_job_id"])),
        ArtifactRef(
            _string(result["artifact_id"]),
            _structured_digest(result["digest"]),
            None if schema_value is None else _schema(_mapping(schema_value, "schema")),
        ),
        _string(value["projection_work_id"]),
        FeatureSetRef(
            FeatureSetId(_string(feature["feature_set_id"])),
            AnalysisRunId(_string(feature["analysis_run_id"])),
            _object_ref(_mapping(feature["bundle_ref"], "bundle ref")),
        ),
        UtcNs(_integer(value["projected_utc_ns"])),
    )


def _object_ref(value: Mapping[str, Any]) -> ObjectRef:
    if set(value) != {"digest", "byte_count", "media_type", "format_id", "locator"}:
        raise ValueError("object ref fields differ")
    return ObjectRef(
        _structured_digest(value["digest"]),
        _integer(value["byte_count"]),
        _string(value["media_type"]),
        _string(value["format_id"]),
        _string(value["locator"]),
    )


def _structured_digest(value: object) -> Digest:
    item = _mapping(value, "digest")
    if set(item) != {"algorithm", "value"}:
        raise ValueError("digest fields differ")
    return Digest(DigestAlgorithm(_string(item["algorithm"])), _string(item["value"]))


def _schema(value: Mapping[str, Any]) -> SchemaRef:
    if set(value) != {"schema_id", "version"}:
        raise ValueError("schema fields differ")
    version = _mapping(value["version"], "schema version")
    if set(version) != {"major", "minor"}:
        raise ValueError("schema version fields differ")
    return SchemaRef(
        _string(value["schema_id"]),
        SchemaVersion(_integer(version["major"]), _integer(version["minor"])),
    )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return value


def _digest(value: object) -> Digest:
    text = _string(value)
    algorithm, separator, encoded = text.partition(":")
    if not separator:
        raise ValueError("digest has no algorithm")
    return Digest(DigestAlgorithm(algorithm), encoded)


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("string is invalid")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("integer is invalid")
    return value
