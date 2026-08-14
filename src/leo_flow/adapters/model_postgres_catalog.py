"""Psycopg catalog for authoritative models and append-only releases."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.analysis.model.persistence import (
    CatalogedModelSnapshot,
    ModelSnapshotCatalogProjection,
    ModelSnapshotIntegrityError,
    model_snapshot_projection,
)
from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    ModelRunId,
    ModelSnapshotId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.model import (
    ModelAnalysisRequest,
    ModelApproval,
    ModelRelease,
    ModelSnapshotBundle,
    ModelSnapshotRef,
)
from leo_flow.contracts.storage import ObjectRef

from . import model_postgres_sql as sql

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresModelSnapshotError(RuntimeError):
    pass


class ModelObjectCollisionError(PostgresModelSnapshotError):
    pass


class ModelSnapshotConflictError(PostgresModelSnapshotError):
    pass


class ModelDatasetMismatchError(PostgresModelSnapshotError):
    pass


class ModelReleaseConflictError(PostgresModelSnapshotError):
    pass


class PostgresModelSnapshotCatalog:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(
        self,
        projection: ModelSnapshotCatalogProjection,
        bundle_ref: ObjectRef,
        request: ModelAnalysisRequest,
        bundle: ModelSnapshotBundle,
        *,
        idempotency_key: str,
    ) -> ModelSnapshotRef:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        if model_snapshot_projection(request, bundle) != projection:
            raise ModelSnapshotIntegrityError(
                "model catalog projection is not derived from its bundle"
            )
        parameters = _parameters(projection, bundle_ref, idempotency_key)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            self._verify_dataset_inputs(cursor, parameters, bundle)
            _register_object(cursor, parameters)
            cursor.execute(sql.PUBLISH_MODEL_SQL, parameters)
            if cursor.fetchone() is not None:
                return _ref(projection, bundle_ref)
            cursor.execute(sql.GET_CONFLICTS_SQL, parameters)
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise ModelSnapshotConflictError(
                    "model identities identify different rows"
                )
            existing = _cataloged(rows[0])
            if (
                str(rows[0]["idempotency_key"]) != idempotency_key
                or existing.projection != projection
                or existing.bundle_ref != bundle_ref
            ):
                raise ModelSnapshotConflictError(
                    "model identity or idempotency key identifies different content"
                )
            return existing.ref

    @staticmethod
    def _verify_dataset_inputs(
        cursor: psycopg.Cursor[dict[str, object]],
        parameters: dict[str, object],
        bundle: ModelSnapshotBundle,
    ) -> None:
        cursor.execute(sql.DATASET_INPUTS_SQL, parameters)
        rows = cursor.fetchall()
        if not rows or rows[0]["snapshot_id"] is None:
            raise ModelDatasetMismatchError(
                "model request does not identify an authoritative dataset"
            )
        expected = (bundle.dataset_membership_digest,) + tuple(
            _digest(row, "feature") for row in rows
        )
        if bundle.provenance.input_digests != expected:
            raise ModelDatasetMismatchError(
                "model provenance does not close over exact dataset members"
            )

    def get(self, ref: ModelSnapshotRef) -> CatalogedModelSnapshot | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(sql.GET_EXACT_MODEL_SQL, _ref_parameters(ref))
            row = cursor.fetchone()
            if row is None:
                return None
            cataloged = _cataloged(row)
            return cataloged if cataloged.bundle_ref == ref.bundle_ref else None

    def release(
        self,
        model_ref: ModelSnapshotRef,
        alias: str,
        approval: ModelApproval,
        *,
        idempotency_key: str,
    ) -> ModelRelease:
        candidate = ModelRelease(alias, model_ref, approval)
        parameters = _release_parameters(candidate, idempotency_key)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(sql.GET_EXACT_MODEL_SQL, _ref_parameters(model_ref))
            model_row = cursor.fetchone()
            if model_row is None or _cataloged(model_row).ref != model_ref:
                raise ModelSnapshotIntegrityError(
                    "release must reference an exact published model"
                )
            cursor.execute(sql.PUBLISH_RELEASE_SQL, parameters)
            if cursor.fetchone() is not None:
                return candidate
            cursor.execute(sql.GET_RELEASE_CONFLICTS_SQL, parameters)
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise ModelReleaseConflictError(
                    "release identities identify different rows"
                )
            existing = _release(rows[0])
            if (
                str(rows[0]["idempotency_key"]) != idempotency_key
                or existing != candidate
            ):
                raise ModelReleaseConflictError(
                    "release identity or idempotency key was reused differently"
                )
            return existing

    def get_release(self, alias: str) -> ModelRelease | None:
        if not alias:
            raise ValueError("alias cannot be empty")
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(sql.GET_CURRENT_RELEASE_SQL, {"alias": alias})
            row = cursor.fetchone()
            return None if row is None else _release(row)


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _register_object(
    cursor: psycopg.Cursor[dict[str, object]], parameters: dict[str, object]
) -> None:
    cursor.execute(sql.REGISTER_OBJECT_SQL, parameters)
    cursor.execute(sql.VERIFY_OBJECT_SQL, parameters)
    row = cursor.fetchone()
    if row is None or (
        _integer(row["byte_count"], "byte_count") != parameters["bundle_byte_count"]
        or row["media_type"] != parameters["bundle_media_type"]
        or row["format_id"] != parameters["bundle_format_id"]
        or row["locator"] != parameters["bundle_locator"]
    ):
        raise ModelObjectCollisionError("model object metadata conflicts")


def _parameters(
    projection: ModelSnapshotCatalogProjection,
    bundle_ref: ObjectRef,
    idempotency_key: str,
) -> dict[str, object]:
    return {
        "model_snapshot_id": projection.model_snapshot_id,
        "model_run_id": projection.model_run_id,
        "dataset_snapshot_id": projection.dataset_snapshot_id,
        "dataset_membership_digest_algorithm": projection.dataset_membership_digest.algorithm.value,
        "dataset_membership_digest_value": projection.dataset_membership_digest.value,
        "request_digest_algorithm": projection.request_digest.algorithm.value,
        "request_digest_value": projection.request_digest.value,
        "provenance_digest_algorithm": projection.provenance_digest.algorithm.value,
        "provenance_digest_value": projection.provenance_digest.value,
        "bundle_digest_algorithm": bundle_ref.digest.algorithm.value,
        "bundle_digest_value": bundle_ref.digest.value,
        "bundle_byte_count": bundle_ref.byte_count,
        "bundle_media_type": bundle_ref.media_type,
        "bundle_format_id": bundle_ref.format_id,
        "bundle_locator": bundle_ref.locator,
        "parameter_count": projection.parameter_count,
        "idempotency_key": idempotency_key,
    }


def _ref_parameters(ref: ModelSnapshotRef) -> dict[str, object]:
    return {
        "model_snapshot_id": str(ref.model_snapshot_id),
        "model_run_id": str(ref.model_run_id),
        "bundle_digest_algorithm": ref.bundle_ref.digest.algorithm.value,
        "bundle_digest_value": ref.bundle_ref.digest.value,
    }


def _ref(
    projection: ModelSnapshotCatalogProjection, bundle_ref: ObjectRef
) -> ModelSnapshotRef:
    return ModelSnapshotRef(
        ModelSnapshotId(projection.model_snapshot_id),
        ModelRunId(projection.model_run_id),
        bundle_ref,
    )


def _cataloged(row: dict[str, object]) -> CatalogedModelSnapshot:
    projection = ModelSnapshotCatalogProjection(
        str(row["model_snapshot_id"]),
        str(row["model_run_id"]),
        str(row["dataset_snapshot_id"]),
        _digest(row, "dataset_membership"),
        _digest(row, "request"),
        _digest(row, "provenance"),
        _integer(row["parameter_count"], "parameter_count"),
    )
    return CatalogedModelSnapshot(projection, _object_ref(row))


def _release_parameters(
    release: ModelRelease, idempotency_key: str
) -> dict[str, object]:
    approval = release.approval
    ref = release.model_ref
    digest = canonical_digest(approval)
    return {
        **_ref_parameters(ref),
        "alias": release.alias,
        "approved_by": approval.approved_by,
        "approved_utc_ns": int(approval.approved_utc_ns),
        "rationale": approval.rationale,
        "approval_digest_algorithm": digest.algorithm.value,
        "approval_digest_value": digest.value,
        "idempotency_key": idempotency_key,
    }


def _release(row: dict[str, object]) -> ModelRelease:
    return ModelRelease(
        str(row["alias"]),
        ModelSnapshotRef(
            ModelSnapshotId(str(row["model_snapshot_id"])),
            ModelRunId(str(row["model_run_id"])),
            _object_ref(row),
        ),
        ModelApproval(
            str(row["approved_by"]),
            UtcNs(_integer(row["approved_utc_ns"], "approved_utc_ns")),
            str(row["rationale"]),
        ),
    )


def _object_ref(row: dict[str, object]) -> ObjectRef:
    return ObjectRef(
        _digest(row, "bundle"),
        _integer(row["bundle_byte_count"], "bundle_byte_count"),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PostgresModelSnapshotError(f"database {field} is not an integer")
    return value
