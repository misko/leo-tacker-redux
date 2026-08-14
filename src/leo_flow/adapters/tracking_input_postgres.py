"""PostgreSQL catalog for immutable tracking-input snapshots."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.analysis.model.tracking_input_codec import MAX_TRACKING_INPUT_BYTES
from leo_flow.analysis.model.tracking_input_persistence import (
    CatalogedTrackingInput,
    TrackingInputEntryProjection,
    TrackingInputIntegrityError,
    TrackingInputProjection,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    DatasetSnapshotId,
    Digest,
    DigestAlgorithm,
    SchemaRef,
    SchemaVersion,
    canonical_json_bytes,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.contracts.tracking_input import (
    TRACKING_INPUT_FORMAT_ID,
    TRACKING_INPUT_MEDIA_TYPE,
    DurableDatasetIdentity,
    TrackingInputSnapshotIdentity,
    TrackingInputSnapshotRef,
)

from . import tracking_input_postgres_sql as sql

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresTrackingInputError(RuntimeError):
    """Tracking-input catalog operation failed closed."""


class TrackingInputObjectCollisionError(PostgresTrackingInputError):
    """A content digest is already registered with different object metadata."""


class TrackingInputConflictError(PostgresTrackingInputError):
    """A publication identity or idempotency key was reused differently."""


class PostgresTrackingInputCatalog:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(
        self,
        projection: TrackingInputProjection,
        *,
        idempotency_key: str,
    ) -> TrackingInputSnapshotRef:
        if not idempotency_key or any(
            character.isspace() for character in idempotency_key
        ):
            raise ValueError("idempotency_key must be a token")
        _validate_projection(projection)
        parameters = _parameters(projection, idempotency_key)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            _register_object(cursor, parameters)
            cursor.execute(sql.PUBLISH_SQL, parameters)
            published = cursor.fetchone()
            if published is None:
                raise PostgresTrackingInputError(
                    "tracking input publication returned no decision"
                )
            if bool(published["inserted"]):
                return projection.ref

            cursor.execute(sql.GET_CONFLICTS_SQL, parameters)
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise TrackingInputConflictError(
                    "tracking input identities identify different rows"
                )
            existing = _cataloged(cursor, rows[0])
            if (
                str(rows[0]["idempotency_key"]) != idempotency_key
                or existing.projection != projection
            ):
                raise TrackingInputConflictError(
                    "tracking input identity or idempotency key identifies "
                    "different content"
                )
            return existing.projection.ref

    def get(self, ref: TrackingInputSnapshotRef) -> CatalogedTrackingInput | None:
        return self.get_by_identity(ref.identity())

    def get_by_identity(
        self, identity: TrackingInputSnapshotIdentity
    ) -> CatalogedTrackingInput | None:
        parameters = _identity_parameters(identity)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(sql.GET_EXACT_SQL, parameters)
            row = cursor.fetchone()
            if row is None:
                return None
            cataloged = _cataloged(cursor, row)
            if not cataloged.projection.ref.matches_identity(identity):
                raise TrackingInputIntegrityError(
                    "catalog returned a substituted tracking input identity"
                )
            return cataloged


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _validate_projection(projection: TrackingInputProjection) -> None:
    bundle = projection.ref.bundle_ref
    if (
        bundle.byte_count < 1
        or bundle.byte_count > MAX_TRACKING_INPUT_BYTES
        or bundle.media_type != TRACKING_INPUT_MEDIA_TYPE
        or bundle.format_id != TRACKING_INPUT_FORMAT_ID
    ):
        raise TrackingInputIntegrityError(
            "tracking input bundle metadata is outside catalog bounds"
        )
    if projection.entry_count != len(projection.entries) or not projection.entries:
        raise TrackingInputIntegrityError("tracking input entry count differs")
    if any(
        entry.entry_index != index for index, entry in enumerate(projection.entries)
    ):
        raise TrackingInputIntegrityError("tracking input entries are not contiguous")
    if projection.builder_ref.schema is None or projection.selector_ref.schema is None:
        raise TrackingInputIntegrityError(
            "tracking input builder and selector require schemas"
        )
    if any(
        entry.calibration_ref.schema is None
        or entry.prediction_policy_ref.schema is None
        for entry in projection.entries
    ):
        raise TrackingInputIntegrityError(
            "tracking input entry artifacts require schemas"
        )


def _parameters(
    projection: TrackingInputProjection, idempotency_key: str
) -> dict[str, object]:
    ref = projection.ref
    dataset = projection.durable_dataset
    builder = _artifact_document(projection.builder_ref)
    selector = _artifact_document(projection.selector_ref)
    publication: dict[str, object] = {
        "snapshot_id": ref.snapshot_id,
        "snapshot_digest_algorithm": ref.snapshot_digest.algorithm.value,
        "snapshot_digest_value": ref.snapshot_digest.value,
        "membership_digest_algorithm": ref.membership_digest.algorithm.value,
        "membership_digest_value": ref.membership_digest.value,
        "dataset_snapshot_id": str(dataset.snapshot_id),
        "dataset_membership_digest_algorithm": (
            dataset.feature_membership_digest.algorithm.value
        ),
        "dataset_membership_digest_value": dataset.feature_membership_digest.value,
        "dataset_snapshot_digest_algorithm": dataset.snapshot_digest.algorithm.value,
        "dataset_snapshot_digest_value": dataset.snapshot_digest.value,
        "builder_artifact_id": builder["artifact_id"],
        "builder_digest_algorithm": builder["digest_algorithm"],
        "builder_digest_value": builder["digest_value"],
        "builder_schema_id": builder["schema_id"],
        "builder_schema_version": builder["schema_version"],
        "selector_artifact_id": selector["artifact_id"],
        "selector_digest_algorithm": selector["digest_algorithm"],
        "selector_digest_value": selector["digest_value"],
        "selector_schema_id": selector["schema_id"],
        "selector_schema_version": selector["schema_version"],
        "provenance_digest_algorithm": projection.provenance_digest.algorithm.value,
        "provenance_digest_value": projection.provenance_digest.value,
        "bundle_digest_algorithm": ref.bundle_ref.digest.algorithm.value,
        "bundle_digest_value": ref.bundle_ref.digest.value,
        "bundle_byte_count": ref.bundle_ref.byte_count,
        "bundle_media_type": ref.bundle_ref.media_type,
        "bundle_format_id": ref.bundle_ref.format_id,
        "bundle_locator": ref.bundle_ref.locator,
        "entry_count": projection.entry_count,
        "idempotency_key": idempotency_key,
        "entries": [_entry_document(entry) for entry in projection.entries],
    }
    if len(canonical_json_bytes(publication)) > MAX_TRACKING_INPUT_BYTES:
        raise TrackingInputIntegrityError(
            "tracking input catalog publication exceeds its size bound"
        )
    return {
        "publication": Jsonb(publication),
        "bundle_digest_algorithm": ref.bundle_ref.digest.algorithm.value,
        "bundle_digest_value": ref.bundle_ref.digest.value,
        "bundle_byte_count": ref.bundle_ref.byte_count,
        "bundle_media_type": ref.bundle_ref.media_type,
        "bundle_format_id": ref.bundle_ref.format_id,
        "bundle_locator": ref.bundle_ref.locator,
        "snapshot_id": ref.snapshot_id,
        "snapshot_digest_algorithm": ref.snapshot_digest.algorithm.value,
        "snapshot_digest_value": ref.snapshot_digest.value,
        "membership_digest_algorithm": ref.membership_digest.algorithm.value,
        "membership_digest_value": ref.membership_digest.value,
        "idempotency_key": idempotency_key,
    }


def _entry_document(entry: TrackingInputEntryProjection) -> dict[str, object]:
    calibration = _artifact_document(entry.calibration_ref)
    prediction = _artifact_document(entry.prediction_policy_ref)
    return {
        "entry_index": entry.entry_index,
        "feature_set_id": entry.feature_set_id,
        "analysis_run_id": entry.analysis_run_id,
        "feature_bundle_digest_algorithm": entry.feature_bundle_digest.algorithm.value,
        "feature_bundle_digest_value": entry.feature_bundle_digest.value,
        "feature_id": entry.feature_id,
        "recording_id": entry.recording_id,
        "recording_identity_digest_algorithm": (
            entry.recording_identity_digest.algorithm.value
        ),
        "recording_identity_digest_value": entry.recording_identity_digest.value,
        "receiver_chain_id": entry.receiver_chain_id,
        "midpoint_utc_ns": entry.midpoint_utc_ns,
        "hardware_link_id": entry.hardware_link_id,
        "hardware_link_digest_algorithm": entry.hardware_link_digest.algorithm.value,
        "hardware_link_digest_value": entry.hardware_link_digest.value,
        "ephemeris_link_id": entry.ephemeris_link_id,
        "ephemeris_link_digest_algorithm": entry.ephemeris_link_digest.algorithm.value,
        "ephemeris_link_digest_value": entry.ephemeris_link_digest.value,
        "calibration_artifact_id": calibration["artifact_id"],
        "calibration_digest_algorithm": calibration["digest_algorithm"],
        "calibration_digest_value": calibration["digest_value"],
        "calibration_schema_id": calibration["schema_id"],
        "calibration_schema_version": calibration["schema_version"],
        "prediction_policy_artifact_id": prediction["artifact_id"],
        "prediction_policy_digest_algorithm": prediction["digest_algorithm"],
        "prediction_policy_digest_value": prediction["digest_value"],
        "prediction_policy_schema_id": prediction["schema_id"],
        "prediction_policy_schema_version": prediction["schema_version"],
    }


def _artifact_document(ref: ArtifactRef) -> dict[str, str]:
    if ref.schema is None:
        raise TrackingInputIntegrityError("tracking input artifact requires a schema")
    return {
        "artifact_id": ref.artifact_id,
        "digest_algorithm": ref.digest.algorithm.value,
        "digest_value": ref.digest.value,
        "schema_id": ref.schema.schema_id,
        "schema_version": str(ref.schema.version),
    }


def _identity_parameters(identity: TrackingInputSnapshotIdentity) -> dict[str, object]:
    return {
        "snapshot_id": identity.snapshot_id,
        "snapshot_digest_algorithm": identity.snapshot_digest.algorithm.value,
        "snapshot_digest_value": identity.snapshot_digest.value,
        "membership_digest_algorithm": identity.membership_digest.algorithm.value,
        "membership_digest_value": identity.membership_digest.value,
        "bundle_digest_algorithm": identity.bundle_digest.algorithm.value,
        "bundle_digest_value": identity.bundle_digest.value,
        "bundle_byte_count": identity.bundle_byte_count,
        "bundle_media_type": identity.bundle_media_type,
        "bundle_format_id": identity.bundle_format_id,
    }


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
        raise TrackingInputObjectCollisionError(
            "tracking input object metadata conflicts"
        )


def _cataloged(
    cursor: psycopg.Cursor[dict[str, object]], row: dict[str, object]
) -> CatalogedTrackingInput:
    ref = TrackingInputSnapshotRef(
        str(row["snapshot_id"]),
        _digest(row, "snapshot"),
        _digest(row, "membership"),
        ObjectRef(
            _digest(row, "bundle"),
            _integer(row["bundle_byte_count"], "bundle_byte_count"),
            str(row["bundle_media_type"]),
            str(row["bundle_format_id"]),
            str(row["bundle_locator"]),
        ),
    )
    cursor.execute(sql.GET_ENTRIES_SQL, {"catalog_snapshot_id": ref.snapshot_id})
    entry_rows = cursor.fetchall()
    entries = tuple(_entry(row) for row in entry_rows)
    entry_count = _integer(row["entry_count"], "entry_count")
    if len(entries) != entry_count:
        raise TrackingInputIntegrityError("tracking input catalog entry count differs")
    return CatalogedTrackingInput(
        TrackingInputProjection(
            ref,
            DurableDatasetIdentity(
                DatasetSnapshotId(str(row["dataset_snapshot_id"])),
                _digest(row, "dataset_membership"),
                _digest(row, "dataset_snapshot"),
            ),
            _artifact(row, "builder"),
            _artifact(row, "selector"),
            _digest(row, "provenance"),
            entry_count,
            entries,
        )
    )


def _entry(row: dict[str, object]) -> TrackingInputEntryProjection:
    return TrackingInputEntryProjection(
        _integer(row["entry_index"], "entry_index"),
        str(row["feature_set_id"]),
        str(row["analysis_run_id"]),
        _digest(row, "feature_bundle"),
        str(row["feature_id"]),
        str(row["recording_id"]),
        _digest(row, "recording_identity"),
        str(row["receiver_chain_id"]),
        _integer(row["midpoint_utc_ns"], "midpoint_utc_ns"),
        str(row["hardware_link_id"]),
        _digest(row, "hardware_link"),
        str(row["ephemeris_link_id"]),
        _digest(row, "ephemeris_link"),
        _artifact(row, "calibration"),
        _artifact(row, "prediction_policy"),
    )


def _artifact(row: dict[str, object], prefix: str) -> ArtifactRef:
    return ArtifactRef(
        str(row[f"{prefix}_artifact_id"]),
        _digest(row, prefix),
        SchemaRef(
            str(row[f"{prefix}_schema_id"]),
            SchemaVersion.parse(str(row[f"{prefix}_schema_version"])),
        ),
    )


def _digest(row: dict[str, object], prefix: str) -> Digest:
    return Digest(
        DigestAlgorithm(str(row[f"{prefix}_digest_algorithm"])),
        str(row[f"{prefix}_digest_value"]),
    )


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TrackingInputIntegrityError(f"catalog {name} is not an integer")
    return value
