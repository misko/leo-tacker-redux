"""Psycopg catalog for atomic immutable FeatureSet publication."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.analysis.recording.persistence import (
    CatalogedFeatureSet,
    FeatureSetCatalogProjection,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    Digest,
    DigestAlgorithm,
    FeatureSetId,
)
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

from . import feature_postgres_sql

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresFeatureSetError(RuntimeError):
    pass


class FeatureObjectCollisionError(PostgresFeatureSetError):
    pass


class FeatureSetConflictError(PostgresFeatureSetError):
    pass


class FeatureRecordingMismatchError(PostgresFeatureSetError):
    pass


class PostgresFeatureSetCatalog:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(
        self,
        projection: FeatureSetCatalogProjection,
        bundle_ref: ObjectRef,
        recording_ref: RecordingObjectRef,
        *,
        idempotency_key: str,
    ) -> FeatureSetRef:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        if (
            projection.recording_id != str(recording_ref.recording_id)
            or projection.input_recording_digest != recording_ref.identity_digest()
        ):
            raise FeatureRecordingMismatchError(
                "feature projection and recording reference differ"
            )
        parameters = _parameters(projection, bundle_ref, idempotency_key)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            existing_recording = PostgresRecordingCatalog._get_with_cursor(
                cursor, str(recording_ref.recording_id)
            )
            if (
                existing_recording is None
                or existing_recording.recording_object != recording_ref
            ):
                raise FeatureRecordingMismatchError(
                    "recording catalog does not contain the exact analysis input"
                )
            _register_object(cursor, bundle_ref)
            cursor.execute(feature_postgres_sql.PUBLISH_FEATURE_SET_SQL, parameters)
            if cursor.fetchone() is not None:
                return _ref(projection, bundle_ref)
            cursor.execute(feature_postgres_sql.GET_CONFLICTS_SQL, parameters)
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise FeatureSetConflictError(
                    "feature identity and idempotency key identify different rows"
                )
            existing = _cataloged(rows[0])
            if (
                str(rows[0]["idempotency_key"]) != idempotency_key
                or existing.projection != projection
                or existing.bundle_ref != bundle_ref
            ):
                raise FeatureSetConflictError(
                    "feature identity or idempotency key identifies different content"
                )
            return existing.ref

    def get(self, ref: FeatureSetRef) -> CatalogedFeatureSet | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                feature_postgres_sql.GET_EXACT_FEATURE_SET_SQL, _ref_parameters(ref)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cataloged = _cataloged(row)
            return cataloged if cataloged.bundle_ref == ref.bundle_ref else None


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _register_object(cursor: psycopg.Cursor[dict[str, object]], ref: ObjectRef) -> None:
    parameters = _object_parameters(ref)
    cursor.execute(feature_postgres_sql.REGISTER_OBJECT_SQL, parameters)
    cursor.execute(feature_postgres_sql.VERIFY_OBJECT_SQL, parameters)
    row = cursor.fetchone()
    if row is None or (
        _database_int(row["byte_count"], "byte_count") != ref.byte_count
        or row["media_type"] != ref.media_type
        or row["format_id"] != ref.format_id
        or row["locator"] != ref.locator
    ):
        raise FeatureObjectCollisionError(
            f"object digest {ref.digest} identifies different metadata"
        )


def _parameters(
    projection: FeatureSetCatalogProjection,
    bundle_ref: ObjectRef,
    idempotency_key: str,
) -> dict[str, object]:
    return {
        "feature_set_id": projection.feature_set_id,
        "analysis_run_id": projection.analysis_run_id,
        "recording_id": projection.recording_id,
        "input_recording_digest_algorithm": projection.input_recording_digest.algorithm.value,
        "input_recording_digest_value": projection.input_recording_digest.value,
        "request_digest_algorithm": projection.request_digest.algorithm.value,
        "request_digest_value": projection.request_digest.value,
        "bundle_digest_algorithm": bundle_ref.digest.algorithm.value,
        "bundle_digest_value": bundle_ref.digest.value,
        "observation_count": projection.observation_count,
        "method_score_count": projection.method_score_count,
        "idempotency_key": idempotency_key,
    }


def _ref_parameters(ref: FeatureSetRef) -> dict[str, object]:
    return {
        "feature_set_id": str(ref.feature_set_id),
        "analysis_run_id": str(ref.analysis_run_id),
        "bundle_digest_algorithm": ref.bundle_ref.digest.algorithm.value,
        "bundle_digest_value": ref.bundle_ref.digest.value,
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


def _cataloged(row: dict[str, object]) -> CatalogedFeatureSet:
    projection = FeatureSetCatalogProjection(
        feature_set_id=str(row["feature_set_id"]),
        analysis_run_id=str(row["analysis_run_id"]),
        recording_id=str(row["recording_id"]),
        input_recording_digest=_digest(row, "input_recording"),
        request_digest=_digest(row, "request"),
        observation_count=_database_int(row["observation_count"], "observation_count"),
        method_score_count=_database_int(
            row["method_score_count"], "method_score_count"
        ),
    )
    bundle = ObjectRef(
        _digest(row, "bundle"),
        _database_int(row["bundle_byte_count"], "bundle_byte_count"),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )
    return CatalogedFeatureSet(projection, bundle)


def _ref(
    projection: FeatureSetCatalogProjection, bundle_ref: ObjectRef
) -> FeatureSetRef:
    return FeatureSetRef(
        FeatureSetId(projection.feature_set_id),
        AnalysisRunId(projection.analysis_run_id),
        bundle_ref,
    )


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _database_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PostgresFeatureSetError(f"database {name} is not an integer")
    return value
