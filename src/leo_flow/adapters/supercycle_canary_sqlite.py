"""Full-sync SQLite journal dedicated to one finite canary namespace."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from leo_flow.capture.campaign import CampaignAnalysisReceipt, CampaignAnalysisSuccess
from leo_flow.capture.supercycle_canary import (
    CANARY_SLOTS,
    CanaryHaltReason,
    CanaryPhase,
    CanaryRecord,
    CanaryRecordPhase,
    CanaryState,
    SupercycleCanaryDefinition,
    build_canary_unit,
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

MAX_CANARY_STATE_BYTES = 1024 * 1024


class SQLiteSupercycleCanaryJournal:
    """Definition-bound durable CAS journal with its own table and keyspace."""

    def __init__(
        self, database_path: Path, *, now_utc_ns: Callable[[], int] = time.time_ns
    ) -> None:
        if (
            not database_path.is_absolute()
            or ".." in database_path.parts
            or "continuous" in database_path.parts
            or "qualification" in database_path.parts
        ):
            raise ValueError("canary journal path is not isolated")
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._now = now_utc_ns
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS supercycle_canary_journal (
                    canary_id TEXT PRIMARY KEY,
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

    def initialize(self, definition: SupercycleCanaryDefinition) -> CanaryState:
        initial = CanaryState(definition.digest)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM supercycle_canary_journal WHERE canary_id = ?",
                (definition.canary_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO supercycle_canary_journal
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        definition.canary_id,
                        str(definition.digest),
                        _encode_state(initial),
                        0,
                        self._now(),
                    ),
                )
                return initial
        return self._decode_row(definition, row)

    def load(self, definition: SupercycleCanaryDefinition) -> CanaryState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM supercycle_canary_journal WHERE canary_id = ?",
                (definition.canary_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("canary journal is not initialized")
        return self._decode_row(definition, row)

    def compare_and_swap(
        self,
        definition: SupercycleCanaryDefinition,
        expected_revision: int,
        replacement: CanaryState,
    ) -> CanaryState:
        if (
            replacement.definition_digest != definition.digest
            or replacement.revision != expected_revision + 1
        ):
            raise RuntimeError("canary journal replacement is invalid")
        payload = _encode_state(replacement)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE supercycle_canary_journal
                      SET state_payload = ?, revision = ?, updated_utc_ns = ?
                    WHERE canary_id = ? AND definition_digest = ? AND revision = ?""",
                (
                    payload,
                    replacement.revision,
                    self._now(),
                    definition.canary_id,
                    str(definition.digest),
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("canary journal revision changed")
        return replacement

    @staticmethod
    def _decode_row(
        definition: SupercycleCanaryDefinition, row: sqlite3.Row
    ) -> CanaryState:
        try:
            if row["definition_digest"] != str(definition.digest):
                raise ValueError("canary definition digest differs")
            state = _decode_state(definition, bytes(row["state_payload"]))
            if state.revision != row["revision"]:
                raise ValueError("canary revision differs")
            return state
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("canary journal failed integrity validation") from error


def _encode_state(state: CanaryState) -> bytes:
    payload = canonical_json_bytes(
        {
            "definition_digest": str(state.definition_digest),
            "phase": state.phase.value,
            "halt_reason": state.halt_reason.value if state.halt_reason else None,
            "revision": state.revision,
            "records": [
                {
                    "slot_index": record.unit.slot_index,
                    "unit_digest": str(record.unit.digest),
                    "phase": record.phase.value,
                    "snapshot": (
                        json.loads(encode_capture_batch_snapshot(record.snapshot))
                        if record.snapshot
                        else None
                    ),
                    "analysis_receipt": (
                        json.loads(canonical_json_bytes(record.analysis_receipt))
                        if record.analysis_receipt
                        else None
                    ),
                }
                for record in state.records
            ],
        }
    )
    if len(payload) > MAX_CANARY_STATE_BYTES:
        raise RuntimeError("canary state exceeds size limit")
    return payload


def _decode_state(
    definition: SupercycleCanaryDefinition, payload: bytes
) -> CanaryState:
    if len(payload) > MAX_CANARY_STATE_BYTES:
        raise ValueError("canary state exceeds size limit")
    root = _mapping(json.loads(payload), "canary state")
    if set(root) != {
        "definition_digest",
        "phase",
        "halt_reason",
        "revision",
        "records",
    }:
        raise ValueError("canary state fields differ")
    records_value = root["records"]
    if not isinstance(records_value, list) or len(records_value) > CANARY_SLOTS:
        raise ValueError("canary records differ")
    records: list[CanaryRecord] = []
    for index, value in enumerate(records_value):
        item = _mapping(value, "canary record")
        if set(item) != {
            "slot_index",
            "unit_digest",
            "phase",
            "snapshot",
            "analysis_receipt",
        }:
            raise ValueError("canary record fields differ")
        slot = _integer(item["slot_index"])
        if slot != index:
            raise ValueError("canary record order differs")
        unit = build_canary_unit(definition, slot_index=slot)
        if item["unit_digest"] != str(unit.digest):
            raise ValueError("canary unit digest differs")
        snapshot_value = item["snapshot"]
        snapshot = (
            None
            if snapshot_value is None
            else decode_capture_batch_snapshot(canonical_json_bytes(snapshot_value))
        )
        receipt_value = item["analysis_receipt"]
        receipt = (
            None
            if receipt_value is None
            else _analysis_receipt(_mapping(receipt_value, "analysis receipt"))
        )
        records.append(
            CanaryRecord(
                unit, CanaryRecordPhase(_string(item["phase"])), snapshot, receipt
            )
        )
    reason = root["halt_reason"]
    state = CanaryState(
        _digest(root["definition_digest"]),
        CanaryPhase(_string(root["phase"])),
        tuple(records),
        None if reason is None else CanaryHaltReason(_string(reason)),
        _integer(root["revision"]),
    )
    if state.definition_digest != definition.digest or _encode_state(state) != payload:
        raise ValueError("canary state is noncanonical")
    return state


def _analysis_receipt(value: Mapping[str, Any]) -> CampaignAnalysisReceipt:
    if set(value) != {"batch_id", "successes", "completed_utc_ns"}:
        raise ValueError("analysis receipt fields differ")
    values = value["successes"]
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError("analysis receipt successes differ")
    successes = tuple(_analysis_success(_mapping(item, "success")) for item in values)
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
    artifact = _mapping(value["result_ref"], "artifact")
    feature = _mapping(value["projected_feature_set_ref"], "feature")
    schema_value = artifact["schema"]
    return CampaignAnalysisSuccess(
        RecordingId(_string(value["recording_id"])),
        JobId(_string(value["analysis_job_id"])),
        ArtifactRef(
            _string(artifact["artifact_id"]),
            _structured_digest(artifact["digest"]),
            None if schema_value is None else _schema(_mapping(schema_value, "schema")),
        ),
        _string(value["projection_work_id"]),
        FeatureSetRef(
            FeatureSetId(_string(feature["feature_set_id"])),
            AnalysisRunId(_string(feature["analysis_run_id"])),
            _object_ref(_mapping(feature["bundle_ref"], "bundle")),
        ),
        UtcNs(_integer(value["projected_utc_ns"])),
    )


def _object_ref(value: Mapping[str, Any]) -> ObjectRef:
    return ObjectRef(
        _structured_digest(value["digest"]),
        _integer(value["byte_count"]),
        _string(value["media_type"]),
        _string(value["format_id"]),
        _string(value["locator"]),
    )


def _schema(value: Mapping[str, Any]) -> SchemaRef:
    version = _mapping(value["version"], "schema version")
    return SchemaRef(
        _string(value["schema_id"]),
        SchemaVersion(_integer(version["major"]), _integer(version["minor"])),
    )


def _structured_digest(value: object) -> Digest:
    item = _mapping(value, "digest")
    return Digest(DigestAlgorithm(_string(item["algorithm"])), _string(item["value"]))


def _digest(value: object) -> Digest:
    algorithm, separator, encoded = _string(value).partition(":")
    if not separator:
        raise ValueError("digest lacks algorithm")
    return Digest(DigestAlgorithm(algorithm), encoded)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{name} must be an object")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("expected non-empty string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer")
    return value
