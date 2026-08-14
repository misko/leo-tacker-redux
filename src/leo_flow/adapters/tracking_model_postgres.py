"""PostgreSQL authority adapter for immutable tracking-model outputs."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.model.tracking_model_persistence import (
    TrackingModelCatalogProjection,
    TrackingModelIntegrityError,
    TrackingModelSnapshotRef,
)
from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    ModelRunId,
    ModelSnapshotId,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.contracts.tracking_input import TrackingInputSnapshotIdentity

from . import tracking_model_postgres_sql as sql

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresTrackingModelError(RuntimeError):
    """Tracking-model catalog operation failed closed."""


class TrackingModelObjectCollisionError(PostgresTrackingModelError):
    """The CAS digest is registered with different metadata."""


class TrackingModelConflictError(PostgresTrackingModelError):
    """A durable publication identity was reused for different content."""


class PostgresTrackingModelCatalog:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(
        self,
        projection: TrackingModelCatalogProjection,
        *,
        idempotency_key: str,
    ) -> TrackingModelSnapshotRef:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            return publish_tracking_model_with_cursor(
                cursor, projection, idempotency_key=idempotency_key
            )

    def get(
        self, ref: TrackingModelSnapshotRef
    ) -> TrackingModelCatalogProjection | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(sql.GET_EXACT_SQL, _ref_parameters(ref))
            row = cursor.fetchone()
            if row is None:
                return None
            projection = _projection(row)
            if projection.ref != ref:
                raise TrackingModelIntegrityError(
                    "catalog returned a substituted tracking model reference"
                )
            return projection


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def publish_tracking_model_with_cursor(
    cursor: psycopg.Cursor[dict[str, object]],
    projection: TrackingModelCatalogProjection,
    *,
    idempotency_key: str,
) -> TrackingModelSnapshotRef:
    """Publish within a caller-owned transaction, suitable for fenced completion."""

    _validate(projection, idempotency_key)
    parameters = _parameters(projection, idempotency_key)
    cursor.execute(sql.REGISTER_OBJECT_SQL, parameters)
    cursor.execute(sql.VERIFY_OBJECT_SQL, parameters)
    row = cursor.fetchone()
    if row is None or any(
        row[name] != parameters[f"bundle_{name}"]
        for name in ("byte_count", "media_type", "format_id", "locator")
    ):
        raise TrackingModelObjectCollisionError("tracking model CAS metadata conflicts")
    cursor.execute(sql.PUBLISH_SQL, parameters)
    decision = cursor.fetchone()
    if decision is None:
        raise PostgresTrackingModelError(
            "tracking model publication returned no decision"
        )
    if bool(decision["inserted"]):
        return projection.ref
    cursor.execute(sql.GET_CONFLICTS_SQL, parameters)
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise TrackingModelConflictError(
            "tracking model identities identify different rows"
        )
    existing = _projection(rows[0])
    if str(rows[0]["idempotency_key"]) != idempotency_key or existing != projection:
        raise TrackingModelConflictError(
            "tracking model identity or idempotency key was reused differently"
        )
    return existing.ref


def _validate(projection: TrackingModelCatalogProjection, idempotency_key: str) -> None:
    if not idempotency_key or any(character.isspace() for character in idempotency_key):
        raise ValueError("idempotency_key must be a token")
    if projection.output_digest != projection.bundle_ref.digest:
        raise TrackingModelIntegrityError(
            "tracking output digest differs from canonical bundle digest"
        )


def _parameters(
    projection: TrackingModelCatalogProjection, idempotency_key: str
) -> dict[str, object]:
    identity = projection.tracking_input_identity
    bundle = projection.bundle_ref
    publication: dict[str, object] = {
        "model_snapshot_id": str(projection.model_snapshot_id),
        "model_run_id": str(projection.model_run_id),
        "scientific_snapshot_digest_algorithm": projection.scientific_snapshot_digest.algorithm.value,
        "scientific_snapshot_digest_value": projection.scientific_snapshot_digest.value,
        "run_digest_algorithm": projection.run_digest.algorithm.value,
        "run_digest_value": projection.run_digest.value,
        "output_digest_algorithm": projection.output_digest.algorithm.value,
        "output_digest_value": projection.output_digest.value,
        "evidence_digest_algorithm": projection.evidence_digest.algorithm.value,
        "evidence_digest_value": projection.evidence_digest.value,
        "provenance_digest_algorithm": projection.provenance_digest.algorithm.value,
        "provenance_digest_value": projection.provenance_digest.value,
        "tracking_input_snapshot_id": identity.snapshot_id,
        "tracking_input_snapshot_digest_algorithm": identity.snapshot_digest.algorithm.value,
        "tracking_input_snapshot_digest_value": identity.snapshot_digest.value,
        "tracking_input_membership_digest_algorithm": identity.membership_digest.algorithm.value,
        "tracking_input_membership_digest_value": identity.membership_digest.value,
        "tracking_input_bundle_digest_algorithm": identity.bundle_digest.algorithm.value,
        "tracking_input_bundle_digest_value": identity.bundle_digest.value,
        "tracking_input_bundle_byte_count": identity.bundle_byte_count,
        "tracking_input_bundle_media_type": identity.bundle_media_type,
        "tracking_input_bundle_format_id": identity.bundle_format_id,
        "parameter_block_count": projection.parameter_block_count,
        "accepted_association_count": projection.accepted_association_count,
        "rejected_association_count": projection.rejected_association_count,
        "warning_count": projection.warning_count,
        "bundle_digest_algorithm": bundle.digest.algorithm.value,
        "bundle_digest_value": bundle.digest.value,
        "bundle_byte_count": bundle.byte_count,
        "bundle_media_type": bundle.media_type,
        "bundle_format_id": bundle.format_id,
        "bundle_locator": bundle.locator,
        "idempotency_key": idempotency_key,
    }
    return {
        **publication,
        "publication": Jsonb(publication),
    }


def _ref_parameters(ref: TrackingModelSnapshotRef) -> dict[str, object]:
    return {
        "model_snapshot_id": str(ref.model_snapshot_id),
        "model_run_id": str(ref.model_run_id),
        "output_digest_algorithm": ref.output_digest.algorithm.value,
        "output_digest_value": ref.output_digest.value,
        "bundle_digest_algorithm": ref.bundle_ref.digest.algorithm.value,
        "bundle_digest_value": ref.bundle_ref.digest.value,
        "bundle_byte_count": ref.bundle_ref.byte_count,
        "bundle_media_type": ref.bundle_ref.media_type,
        "bundle_format_id": ref.bundle_ref.format_id,
    }


def _projection(row: dict[str, object]) -> TrackingModelCatalogProjection:
    identity = TrackingInputSnapshotIdentity(
        str(row["tracking_input_snapshot_id"]),
        _digest(row, "tracking_input_snapshot"),
        _digest(row, "tracking_input_membership"),
        _digest(row, "tracking_input_bundle"),
        _integer(row["tracking_input_bundle_byte_count"], "input byte count"),
        str(row["tracking_input_bundle_media_type"]),
        str(row["tracking_input_bundle_format_id"]),
    )
    bundle_ref = ObjectRef(
        _digest(row, "bundle"),
        _integer(row["bundle_byte_count"], "bundle byte count"),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )
    return TrackingModelCatalogProjection(
        ModelSnapshotId(str(row["model_snapshot_id"])),
        ModelRunId(str(row["model_run_id"])),
        _digest(row, "scientific_snapshot"),
        _digest(row, "run"),
        _digest(row, "output"),
        _digest(row, "evidence"),
        _digest(row, "provenance"),
        identity,
        _integer(row["parameter_block_count"], "parameter block count"),
        _integer(row["accepted_association_count"], "accepted association count"),
        _integer(row["rejected_association_count"], "rejected association count"),
        _integer(row["warning_count"], "warning count"),
        bundle_ref,
    )


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TrackingModelIntegrityError(f"catalog {name} is not an integer")
    return value
