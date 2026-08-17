from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from leo_flow.adapters.capture_batch_sqlite import SQLiteCaptureBatchStateStore
from leo_flow.application.capture_batches import (
    CaptureBatchCoordinator,
    CaptureBatchIdentityConflict,
)
from leo_flow.capture.batch_serialization import (
    CaptureBatchCodecError,
    decode_batch_definition,
    decode_batch_snapshot,
    encode_batch_definition,
    encode_batch_snapshot,
)
from leo_flow.contracts.capture_batch import (
    CaptureAttemptOutcome,
    CaptureAttemptState,
    CaptureBatchDefinition,
    CaptureBatchMode,
    CaptureBatchSnapshot,
    ExpectedCaptureAttempt,
)
from leo_flow.contracts.capture_batch_codec import (
    CaptureBatchDocumentError,
    decode_capture_batch_definition,
    decode_capture_batch_snapshot,
    encode_capture_batch_definition,
    encode_capture_batch_snapshot,
)
from leo_flow.contracts.core import (
    CaptureAttemptId,
    CaptureBatchId,
    PlanId,
    RadioId,
    SchemaRef,
    UtcNs,
)


def _definition() -> CaptureBatchDefinition:
    return CaptureBatchDefinition(
        SchemaRef(CaptureBatchDefinition.SCHEMA_ID),
        CaptureBatchId("cbatch_sqlite_test"),
        CaptureBatchMode.INDEPENDENT,
        (
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_sqlite_a"),
                RadioId("radio_sqlite_a"),
                PlanId("plan_sqlite_a"),
                UtcNs(1_000),
            ),
            ExpectedCaptureAttempt(
                CaptureAttemptId("cattempt_sqlite_b"),
                RadioId("radio_sqlite_b"),
                PlanId("plan_sqlite_b"),
                UtcNs(2_000),
            ),
        ),
    )


def _failure(definition: CaptureBatchDefinition, index: int) -> CaptureAttemptOutcome:
    expected = definition.expected_attempts[index]
    return CaptureAttemptOutcome(
        SchemaRef(CaptureAttemptOutcome.SCHEMA_ID),
        definition.batch_id,
        expected.attempt_id,
        expected.radio_id,
        expected.plan_id,
        CaptureAttemptState.FAILED,
        UtcNs(3_000 + index),
        failure_reason="test_failure",
    )


def test_strict_codec_round_trips_v0_1_definition_and_snapshot() -> None:
    definition = _definition()
    snapshot = CaptureBatchSnapshot(
        SchemaRef(CaptureBatchSnapshot.SCHEMA_ID), definition
    ).record(_failure(definition, 0))

    assert decode_batch_definition(encode_batch_definition(definition)) == definition
    assert decode_batch_snapshot(encode_batch_snapshot(snapshot)) == snapshot
    assert encode_batch_definition(definition) == encode_capture_batch_definition(
        definition
    )
    assert encode_batch_snapshot(snapshot) == encode_capture_batch_snapshot(snapshot)
    assert decode_capture_batch_definition(encode_batch_definition(definition)) == (
        definition
    )
    assert decode_capture_batch_snapshot(encode_batch_snapshot(snapshot)) == snapshot
    assert CaptureBatchCodecError is CaptureBatchDocumentError


@pytest.mark.parametrize("kind", ("unknown", "noncanonical"))
def test_strict_codec_rejects_unknown_fields_and_noncanonical_bytes(kind: str) -> None:
    canonical = encode_batch_definition(_definition())
    if kind == "unknown":
        document = json.loads(canonical)
        document["private_storage_path"] = "/forbidden"
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    else:
        encoded = json.dumps(json.loads(canonical), indent=2).encode()

    with pytest.raises(CaptureBatchCodecError):
        decode_batch_definition(encoded)


def test_sqlite_state_survives_restart_and_exact_retries_are_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "batch.sqlite3"
    definition = _definition()
    first = CaptureBatchCoordinator(SQLiteCaptureBatchStateStore(database))
    first.register(definition)
    outcome = _failure(definition, 0)
    expected = first.record(outcome)

    restarted = CaptureBatchCoordinator(SQLiteCaptureBatchStateStore(database))
    assert restarted.inspect(definition.batch_id) == expected
    assert restarted.record(outcome) == expected


def test_sqlite_adapter_fails_closed_on_row_tampering(tmp_path: Path) -> None:
    database = tmp_path / "batch.sqlite3"
    definition = _definition()
    store = SQLiteCaptureBatchStateStore(database)
    CaptureBatchCoordinator(store).register(definition)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE capture_batch_state SET definition_digest = ? WHERE batch_id = ?",
            ("sha256:" + "0" * 64, str(definition.batch_id)),
        )

    with pytest.raises(CaptureBatchIdentityConflict, match="integrity"):
        store.get(definition.batch_id)
