"""Transactional PostgreSQL writers for rebuildable dashboard projections."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping

import psycopg

from leo_flow.application.projection_writers import (
    AuthoritativeProjectionIdentity,
    FeatureProjectionCommand,
    ModelProjectionCommand,
    ModelReleaseProjectionCommand,
    ProjectionConflict,
    ProjectionReceipt,
    RecordingProjectionCommand,
    TrackProjectionCommand,
    feature_identities,
    model_identity,
    recording_identities,
    release_identity,
    track_identity,
    validate_feature_command,
    validate_model_command,
    validate_recording_command,
    validate_release_command,
    validate_storage_health,
    validate_track_command,
)
from leo_flow.application.projections import ProjectionInputError
from leo_flow.contracts.dashboard import StorageHealth
from leo_flow.contracts.model import ModelSnapshotBundle
from leo_flow.contracts.storage import RecordingObjectRef

from . import dashboard_projection_postgres_sql as sql
from .dashboard_recording_postgres import (
    publish_recording_capture_detail,
    recording_capture_detail_view_v0_1,
)

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresCaptureProjectionWriter:
    """Project only capture-owned recording and activity facts."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def project_recording(
        self, command: RecordingProjectionCommand
    ) -> ProjectionReceipt:
        validate_recording_command(command)
        manifest = command.manifest
        identities = recording_identities(command)
        with self._connect() as connection:
            _lock(
                connection,
                (f"{identity.kind}:{identity.logical_id}" for identity in identities),
            )
            identity_rows = {
                (identity.kind, identity.logical_id): _check_identity(
                    connection, identity, capture_owned=True
                )
                for identity in identities
            }
            _require_catalog_recording(
                connection, command.published_ref.recording_object
            )
            recording_values: dict[str, object] = {
                "recording_id": str(manifest.recording_id),
                "radio_id": str(manifest.radio_id),
                "started_utc_ns": int(manifest.capture_started_utc_ns),
                "finished_utc_ns": int(manifest.capture_finished_utc_ns),
                "segment_count": len(manifest.segments),
                "recording_object_available": command.recording_object_available,
            }
            latest = connection.execute(
                sql.LATEST_RECORDING_SQL,
                {"recording_id": str(manifest.recording_id)},
            ).fetchone()
            if latest is not None and _row_matches(latest, recording_values):
                recording_sequence = _sequence(latest)
            else:
                recording_values["analysis_state"] = (
                    "pending" if latest is None else latest["analysis_state"]
                )
                row = connection.execute(
                    sql.INSERT_RECORDING_SQL, recording_values
                ).fetchone()
                recording_sequence = _required_sequence(row)
            recording_identity = identities[0]
            if (
                identity_rows[(recording_identity.kind, recording_identity.logical_id)]
                is None
            ):
                _insert_identity(
                    connection,
                    recording_identity,
                    recording_sequence,
                    capture_owned=True,
                )

            activity_values = {
                str(activity.activity_id): {
                    "activity_id": str(activity.activity_id),
                    "recording_id": str(manifest.recording_id),
                    "radio_id": str(manifest.radio_id),
                    "kind": activity.kind.value,
                    "started_utc_ns": int(activity.started_utc_ns),
                }
                for activity in manifest.activities
            }
            existing_rows = (
                connection.execute(
                    sql.LATEST_ACTIVITIES_SQL,
                    {"activity_ids": sorted(activity_values)},
                ).fetchall()
                if activity_values
                else []
            )
            existing = {str(row["activity_id"]): row for row in existing_rows}
            activity_sequences: list[int] = []
            activity_identities = {
                identity.logical_id: identity for identity in identities[1:]
            }
            for activity_id, values in activity_values.items():
                identity = activity_identities[activity_id]
                row = existing.get(activity_id)
                if row is not None:
                    if not _row_matches(row, values):
                        raise ProjectionConflict(
                            f"activity {activity_id} was projected differently"
                        )
                    sequence = _sequence(row)
                    activity_sequences.append(sequence)
                else:
                    inserted = connection.execute(
                        sql.INSERT_ACTIVITY_SQL, values
                    ).fetchone()
                    sequence = _required_sequence(inserted)
                    activity_sequences.append(sequence)
                if identity_rows[(identity.kind, identity.logical_id)] is None:
                    _insert_identity(connection, identity, sequence, capture_owned=True)
            detail_sequence = publish_recording_capture_detail(
                connection,
                recording_capture_detail_view_v0_1(
                    manifest,
                    command.published_ref,
                    analysis_state="pending",
                    recording_object_available=command.recording_object_available,
                ),
            )
        return ProjectionReceipt(
            (recording_sequence, *activity_sequences, detail_sequence)
        )


class PostgresAnalysisProjectionWriter:
    """Project analysis and maintenance facts; never expose dashboard mutation."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def project_features(self, command: FeatureProjectionCommand) -> ProjectionReceipt:
        validate_feature_command(command)
        bundle = command.bundle
        identities = feature_identities(command)
        with self._connect() as connection:
            keys = [f"recording:{bundle.recording_id}"]
            keys.extend(
                f"{identity.kind}:{identity.logical_id}" for identity in identities
            )
            _lock(connection, keys)
            identity_rows = {
                identity.logical_id: _check_identity(
                    connection, identity, capture_owned=False
                )
                for identity in identities
            }
            _require_catalog_recording(
                connection, command.recording_ref.recording_object
            )
            values_by_id = {
                str(observation.feature_id): {
                    "feature_id": str(observation.feature_id),
                    "recording_id": str(bundle.recording_id),
                    "method_id": observation.method_id,
                    "score": observation.score,
                    "score_semantics": observation.score_semantics,
                }
                for observation in bundle.observations
            }
            rows = (
                connection.execute(
                    sql.LATEST_FEATURES_SQL,
                    {"feature_ids": sorted(values_by_id)},
                ).fetchall()
                if values_by_id
                else []
            )
            existing = {str(row["feature_id"]): row for row in rows}
            identities_by_id = {
                identity.logical_id: identity for identity in identities
            }
            sequences: list[int] = []
            for feature_id, values in values_by_id.items():
                identity = identities_by_id[feature_id]
                row = existing.get(feature_id)
                if row is not None:
                    if not _row_matches(row, values):
                        raise ProjectionConflict(
                            f"feature {feature_id} was projected differently"
                        )
                    sequence = _sequence(row)
                    sequences.append(sequence)
                else:
                    inserted = connection.execute(
                        sql.INSERT_FEATURE_SQL, values
                    ).fetchone()
                    sequence = _required_sequence(inserted)
                    sequences.append(sequence)
                if identity_rows[feature_id] is None:
                    _insert_identity(
                        connection, identity, sequence, capture_owned=False
                    )
            recording_sequence = _complete_recording_projection(
                connection, str(bundle.recording_id)
            )
        return ProjectionReceipt((*sequences, recording_sequence))

    def project_model(self, command: ModelProjectionCommand) -> ProjectionReceipt:
        validate_model_command(command)
        return self._project_model_row(
            command.bundle,
            model_identity(command),
            release_alias=None,
        )

    def project_model_release(
        self, command: ModelReleaseProjectionCommand
    ) -> ProjectionReceipt:
        validate_release_command(command)
        return self._project_model_row(
            command.bundle,
            release_identity(command),
            release_alias=command.release.alias,
            require_base=True,
            required_model_identity=model_identity(
                ModelProjectionCommand(command.bundle, command.release.model_ref)
            ),
        )

    def project_track(self, command: TrackProjectionCommand) -> ProjectionReceipt:
        validate_track_command(command)
        view = command.view
        values: dict[str, object] = {
            "track_id": view.track_id,
            "model_snapshot_id": str(view.model_snapshot_id),
            "radio_id": str(command.radio_id),
            "started_utc_ns": int(view.started_utc_ns),
            "finished_utc_ns": int(view.finished_utc_ns),
        }
        identity = track_identity(command)
        with self._connect() as connection:
            _lock(
                connection,
                (
                    f"model:{view.model_snapshot_id}",
                    f"track:{view.track_id}",
                ),
            )
            identity_row = _check_identity(connection, identity, capture_owned=False)
            _require_model_base(
                connection,
                command.model_bundle,
                model_identity(
                    ModelProjectionCommand(command.model_bundle, command.model_ref)
                ),
            )
            latest = connection.execute(
                sql.LATEST_TRACK_SQL, {"track_id": view.track_id}
            ).fetchone()
            if latest is not None:
                if not _row_matches(latest, values):
                    raise ProjectionConflict(
                        f"track {view.track_id} was projected differently"
                    )
                sequence = _sequence(latest)
            else:
                sequence = _required_sequence(
                    connection.execute(sql.INSERT_TRACK_SQL, values).fetchone()
                )
            if identity_row is None:
                _insert_identity(connection, identity, sequence, capture_owned=False)
        return ProjectionReceipt((sequence,))

    def project_storage_health(self, health: StorageHealth) -> ProjectionReceipt:
        validate_storage_health(health)
        values: dict[str, object] = {
            "available": health.available,
            "total_bytes": health.total_bytes,
            "free_bytes": health.free_bytes,
        }
        with self._connect() as connection:
            _lock(connection, ("storage-health",))
            latest = connection.execute(sql.LATEST_STORAGE_SQL).fetchone()
            if latest is not None and _row_matches(latest, values):
                sequence = _sequence(latest)
            else:
                sequence = _required_sequence(
                    connection.execute(sql.INSERT_STORAGE_SQL, values).fetchone()
                )
        return ProjectionReceipt((sequence,))

    def _project_model_row(
        self,
        bundle: ModelSnapshotBundle,
        identity: AuthoritativeProjectionIdentity,
        *,
        release_alias: str | None,
        require_base: bool = False,
        required_model_identity: AuthoritativeProjectionIdentity | None = None,
    ) -> ProjectionReceipt:
        model_snapshot_id = str(bundle.model_snapshot_id)
        warnings = bundle.warnings
        values: dict[str, object] = {
            "model_snapshot_id": model_snapshot_id,
            "release_alias": release_alias,
            "parameter_count": len(bundle.parameters),
            "warnings": json.dumps(warnings),
        }
        with self._connect() as connection:
            keys = [f"model:{model_snapshot_id}"]
            if release_alias is not None:
                keys.append(f"release:{release_alias}")
            _lock(connection, keys)
            identity_row = _check_identity(connection, identity, capture_owned=False)
            if (
                required_model_identity is not None
                and _check_identity(
                    connection, required_model_identity, capture_owned=False
                )
                is None
            ):
                raise ProjectionInputError(
                    "model release cannot be projected before its exact model"
                )
            rows = connection.execute(
                sql.MODEL_ROWS_SQL,
                {
                    "model_snapshot_id": model_snapshot_id,
                    "release_alias": release_alias,
                },
            ).fetchall()
            if require_base and not any(
                row["model_snapshot_id"] == model_snapshot_id
                and row["release_alias"] is None
                and _model_content_matches(row, values)
                for row in rows
            ):
                raise ProjectionInputError(
                    "model release cannot be projected before its model"
                )
            for row in rows:
                if row["release_alias"] == release_alias:
                    if row[
                        "model_snapshot_id"
                    ] != model_snapshot_id or not _model_content_matches(row, values):
                        display_identity = release_alias or model_snapshot_id
                        raise ProjectionConflict(
                            f"model identity {display_identity} was projected differently"
                        )
                    sequence = _sequence(row)
                    if identity_row is None:
                        _insert_identity(
                            connection, identity, sequence, capture_owned=False
                        )
                    return ProjectionReceipt((sequence,))
            sequence = _required_sequence(
                connection.execute(sql.INSERT_MODEL_SQL, values).fetchone()
            )
            if identity_row is None:
                _insert_identity(connection, identity, sequence, capture_owned=False)
        return ProjectionReceipt((sequence,))


def _require_catalog_recording(
    connection: psycopg.Connection[dict[str, object]], ref: RecordingObjectRef
) -> None:
    row = connection.execute(
        sql.CATALOG_RECORDING_SQL, {"recording_id": str(ref.recording_id)}
    ).fetchone()
    expected: dict[str, object] = {
        "recording_id": str(ref.recording_id),
        "data_digest_algorithm": ref.data_object.digest.algorithm.value,
        "data_digest_value": ref.data_object.digest.value,
        "data_byte_count": ref.data_object.byte_count,
        "data_media_type": ref.data_object.media_type,
        "data_format_id": ref.data_object.format_id,
        "data_locator": ref.data_object.locator,
        "metadata_digest_algorithm": ref.metadata_object.digest.algorithm.value,
        "metadata_digest_value": ref.metadata_object.digest.value,
        "metadata_byte_count": ref.metadata_object.byte_count,
        "metadata_media_type": ref.metadata_object.media_type,
        "metadata_format_id": ref.metadata_object.format_id,
        "metadata_locator": ref.metadata_object.locator,
        "manifest_digest_value": ref.manifest_digest.value,
    }
    if row is None:
        raise ProjectionInputError("recording is not present in the published catalog")
    if not _row_matches(row, expected):
        raise ProjectionConflict(
            "catalog recording differs from the published reference"
        )


def _require_model_base(
    connection: psycopg.Connection[dict[str, object]],
    bundle: ModelSnapshotBundle,
    identity: AuthoritativeProjectionIdentity,
) -> None:
    if _check_identity(connection, identity, capture_owned=False) is None:
        raise ProjectionInputError("track cannot be projected before its exact model")
    model_snapshot_id = str(bundle.model_snapshot_id)
    values = {
        "model_snapshot_id": model_snapshot_id,
        "parameter_count": len(bundle.parameters),
        "warnings": bundle.warnings,
    }
    rows = connection.execute(
        sql.MODEL_ROWS_SQL,
        {"model_snapshot_id": model_snapshot_id, "release_alias": None},
    ).fetchall()
    if not any(
        row["release_alias"] is None and _model_content_matches(row, values)
        for row in rows
    ):
        raise ProjectionInputError("track cannot be projected before its model")


def _complete_recording_projection(
    connection: psycopg.Connection[dict[str, object]], recording_id: str
) -> int:
    latest = connection.execute(
        sql.LATEST_RECORDING_SQL, {"recording_id": recording_id}
    ).fetchone()
    if latest is None:
        raise ProjectionInputError(
            "features cannot be projected before their recording projection"
        )
    if latest["analysis_state"] == "complete":
        return _sequence(latest)
    values: dict[str, object] = {
        "recording_id": recording_id,
        "radio_id": latest["radio_id"],
        "started_utc_ns": latest["started_utc_ns"],
        "finished_utc_ns": latest["finished_utc_ns"],
        "analysis_state": "complete",
        "segment_count": latest["segment_count"],
        "recording_object_available": latest["recording_object_available"],
    }
    return _required_sequence(
        connection.execute(sql.INSERT_RECORDING_SQL, values).fetchone()
    )


def _lock(
    connection: psycopg.Connection[dict[str, object]], keys: Iterable[str]
) -> None:
    for key in sorted(set(keys)):
        connection.execute(sql.LOCK_SQL, {"key": key})


def _check_identity(
    connection: psycopg.Connection[dict[str, object]],
    identity: AuthoritativeProjectionIdentity,
    *,
    capture_owned: bool,
) -> dict[str, object] | None:
    statement = sql.CAPTURE_IDENTITY_SQL if capture_owned else sql.ANALYSIS_IDENTITY_SQL
    row = connection.execute(
        statement,
        {"projection_kind": identity.kind, "logical_id": identity.logical_id},
    ).fetchone()
    if row is None:
        return None
    expected_document = json.loads(identity.document_json)
    if (
        row["authoritative_identity_digest"] != identity.digest
        or row["authoritative_identity"] != expected_document
    ):
        raise ProjectionConflict(
            f"{identity.kind} {identity.logical_id} has another authoritative identity"
        )
    return row


def _insert_identity(
    connection: psycopg.Connection[dict[str, object]],
    identity: AuthoritativeProjectionIdentity,
    first_sequence: int,
    *,
    capture_owned: bool,
) -> None:
    statement = (
        sql.INSERT_CAPTURE_IDENTITY_SQL
        if capture_owned
        else sql.INSERT_ANALYSIS_IDENTITY_SQL
    )
    connection.execute(
        statement,
        {
            "projection_kind": identity.kind,
            "logical_id": identity.logical_id,
            "authoritative_identity_digest": identity.digest,
            "authoritative_identity": identity.document_json,
            "first_projection_sequence": first_sequence,
        },
    )


def _row_matches(row: dict[str, object], values: Mapping[str, object]) -> bool:
    return all(row.get(key) == value for key, value in values.items())


def _model_content_matches(
    row: dict[str, object], values: Mapping[str, object]
) -> bool:
    warnings = row["warnings"]
    expected_warnings = values["warnings"]
    if isinstance(expected_warnings, str):
        expected_warnings = json.loads(expected_warnings)
    return (
        row["parameter_count"] == values["parameter_count"]
        and isinstance(warnings, list)
        and isinstance(expected_warnings, (list, tuple))
        and tuple(warnings) == tuple(expected_warnings)
    )


def _sequence(row: dict[str, object]) -> int:
    value = row["projection_sequence"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database projection_sequence is not an integer")
    return value


def _required_sequence(row: dict[str, object] | None) -> int:
    if row is None:
        raise RuntimeError("projection insert did not return a sequence")
    return _sequence(row)
