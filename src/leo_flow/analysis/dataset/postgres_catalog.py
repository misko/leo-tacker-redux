"""Psycopg catalog for atomic immutable dataset snapshot publication."""

from __future__ import annotations

import json
from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.contracts.core import (
    DatasetSnapshotId,
    Digest,
    DigestAlgorithm,
    canonical_json_bytes,
)
from leo_flow.contracts.storage import ObjectRef

from . import postgres_sql
from .persistence import (
    CatalogedDatasetSnapshot,
    DatasetMemberProjection,
    DatasetSnapshotProjection,
    dataset_snapshot_projection,
)
from .snapshot import DatasetSnapshotBundle, DatasetSnapshotRef

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresDatasetCatalogError(RuntimeError):
    """Base error for invalid or conflicting persistent dataset state."""


class DatasetObjectCollisionError(PostgresDatasetCatalogError):
    """One content digest identifies different object metadata."""


class DatasetSnapshotConflictError(PostgresDatasetCatalogError):
    """A snapshot identity or idempotency key was reused inconsistently."""


class PostgresDatasetSnapshotCatalog:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(
        self,
        snapshot: DatasetSnapshotBundle,
        bundle_ref: ObjectRef,
        *,
        idempotency_key: str,
    ) -> DatasetSnapshotRef:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        projection = dataset_snapshot_projection(snapshot)
        parameters = _snapshot_parameters(projection, bundle_ref, idempotency_key)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            _register_object(cursor, bundle_ref)
            cursor.execute(postgres_sql.PUBLISH_SNAPSHOT_SQL, parameters)
            if cursor.fetchone() is not None:
                for member in projection.members:
                    cursor.execute(
                        postgres_sql.PUBLISH_MEMBER_SQL,
                        _member_parameters(snapshot.ref, member),
                    )
                return snapshot.ref

            cursor.execute(postgres_sql.GET_CONFLICTS_SQL, parameters)
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise DatasetSnapshotConflictError(
                    "snapshot identity and idempotency key identify different rows"
                )
            existing = _cataloged_from_cursor(cursor, rows[0])
            if (
                str(rows[0]["idempotency_key"]) != idempotency_key
                or existing.bundle_ref != bundle_ref
                or existing.projection != projection
            ):
                raise DatasetSnapshotConflictError(
                    "snapshot identity or idempotency key identifies different content"
                )
            return snapshot.ref

    def get(self, ref: DatasetSnapshotRef) -> CatalogedDatasetSnapshot | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(postgres_sql.GET_EXACT_SNAPSHOT_SQL, _ref_parameters(ref))
            row = cursor.fetchone()
            return None if row is None else _cataloged_from_cursor(cursor, row)


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _register_object(cursor: psycopg.Cursor[dict[str, object]], ref: ObjectRef) -> None:
    parameters = _object_parameters(ref)
    cursor.execute(postgres_sql.REGISTER_OBJECT_SQL, parameters)
    cursor.execute(postgres_sql.VERIFY_OBJECT_SQL, parameters)
    row = cursor.fetchone()
    if row is None or (
        _database_int(row["byte_count"], "byte_count") != ref.byte_count
        or row["media_type"] != ref.media_type
        or row["format_id"] != ref.format_id
        or row["locator"] != ref.locator
    ):
        raise DatasetObjectCollisionError(
            f"object digest {ref.digest} identifies different metadata"
        )


def _snapshot_parameters(
    projection: DatasetSnapshotProjection,
    bundle_ref: ObjectRef,
    idempotency_key: str,
) -> dict[str, object]:
    return {
        **_ref_parameters(projection.ref),
        "bundle_digest_algorithm": bundle_ref.digest.algorithm.value,
        "bundle_digest_value": bundle_ref.digest.value,
        "evaluated_method_id": projection.evaluated_method_id,
        "selection_spec": projection.selection_spec,
        "selection_cutoff_utc_ns": projection.selection_cutoff_utc_ns,
        "promoted": projection.promoted,
        "promotion_warnings": Jsonb(list(projection.promotion_warnings)),
        "member_count": len(projection.members),
        "idempotency_key": idempotency_key,
    }


def _ref_parameters(ref: DatasetSnapshotRef) -> dict[str, object]:
    return {
        "snapshot_id": str(ref.snapshot_id),
        "feature_membership_digest_algorithm": (
            ref.feature_membership_digest.algorithm.value
        ),
        "feature_membership_digest_value": ref.feature_membership_digest.value,
        "snapshot_digest_algorithm": ref.snapshot_digest.algorithm.value,
        "snapshot_digest_value": ref.snapshot_digest.value,
    }


def _object_parameters(ref: ObjectRef) -> dict[str, object]:
    return {
        "digest_algorithm": ref.digest.algorithm.value,
        "digest_value": ref.digest.value,
        "byte_count": ref.byte_count,
        "media_type": ref.media_type,
        "format_id": ref.format_id,
        "locator": ref.locator,
    }


def _member_parameters(
    ref: DatasetSnapshotRef, member: DatasetMemberProjection
) -> dict[str, object]:
    return {
        "snapshot_id": str(ref.snapshot_id),
        "member_index": member.member_index,
        "feature_set_id": member.feature_set_id,
        "analysis_run_id": member.analysis_run_id,
        "feature_digest_algorithm": member.feature_digest.algorithm.value,
        "feature_digest_value": member.feature_digest.value,
        "feature_byte_count": member.feature_byte_count,
        "feature_media_type": member.feature_media_type,
        "feature_format_id": member.feature_format_id,
        "feature_locator": member.feature_locator,
        "split_group_id": member.split_group_id,
        "split": member.split,
        "role": member.role,
        "truth": Jsonb(json.loads(member.truth_json)),
    }


def _cataloged_from_cursor(
    cursor: psycopg.Cursor[dict[str, object]], row: dict[str, object]
) -> CatalogedDatasetSnapshot:
    ref = DatasetSnapshotRef(
        DatasetSnapshotId(str(row["snapshot_id"])),
        _digest_from_row(row, "feature_membership"),
        _digest_from_row(row, "snapshot"),
    )
    cursor.execute(postgres_sql.GET_MEMBERS_SQL, {"snapshot_id": str(ref.snapshot_id)})
    member_rows = cursor.fetchall()
    expected_count = _database_int(row["member_count"], "member_count")
    if len(member_rows) != expected_count:
        raise PostgresDatasetCatalogError(
            "dataset member count disagrees with snapshot projection"
        )
    warnings = row["promotion_warnings"]
    if not isinstance(warnings, list) or not all(
        isinstance(value, str) for value in warnings
    ):
        raise PostgresDatasetCatalogError("promotion warnings are invalid")
    projection = DatasetSnapshotProjection(
        ref=ref,
        evaluated_method_id=str(row["evaluated_method_id"]),
        selection_spec=str(row["selection_spec"]),
        selection_cutoff_utc_ns=_database_int(
            row["selection_cutoff_utc_ns"], "selection_cutoff_utc_ns"
        ),
        promoted=_database_bool(row["promoted"], "promoted"),
        promotion_warnings=tuple(warnings),
        members=tuple(_member_from_row(value) for value in member_rows),
    )
    bundle_ref = ObjectRef(
        _digest_from_row(row, "bundle"),
        _database_int(row["bundle_byte_count"], "bundle_byte_count"),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )
    return CatalogedDatasetSnapshot(projection, bundle_ref)


def _member_from_row(row: dict[str, object]) -> DatasetMemberProjection:
    return DatasetMemberProjection(
        member_index=_database_int(row["member_index"], "member_index"),
        feature_set_id=str(row["feature_set_id"]),
        analysis_run_id=str(row["analysis_run_id"]),
        feature_digest=_digest_from_row(row, "feature"),
        feature_byte_count=_database_int(
            row["feature_byte_count"], "feature_byte_count"
        ),
        feature_media_type=str(row["feature_media_type"]),
        feature_format_id=str(row["feature_format_id"]),
        feature_locator=str(row["feature_locator"]),
        split_group_id=str(row["split_group_id"]),
        split=str(row["split"]),
        role=str(row["role"]),
        truth_json=_canonical_database_json(row["truth"], "truth"),
    )


def _canonical_database_json(value: object, field: str) -> bytes:
    if not isinstance(value, dict):
        raise PostgresDatasetCatalogError(f"database {field} is not an object")
    return canonical_json_bytes(value)


def _digest_from_row(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _database_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PostgresDatasetCatalogError(f"database {field} is not an integer")
    return value


def _database_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise PostgresDatasetCatalogError(f"database {field} is not a boolean")
    return value
