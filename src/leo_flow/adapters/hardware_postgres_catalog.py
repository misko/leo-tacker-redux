"""Psycopg catalog for atomic immutable hardware snapshots."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from leo_flow.contracts.core import Digest, DigestAlgorithm, HardwareSnapshotId
from leo_flow.contracts.hardware import HardwareMetadataSnapshotRef
from leo_flow.contracts.storage import ObjectRef
from leo_flow.hardware.persistence import (
    CatalogedHardwareSnapshot,
    HardwareChainProjection,
    HardwareSnapshotProjection,
)

from . import hardware_postgres_sql as sql

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresHardwareSnapshotError(RuntimeError):
    pass


class HardwareObjectCollisionError(PostgresHardwareSnapshotError):
    pass


class HardwareSnapshotConflictError(PostgresHardwareSnapshotError):
    pass


class PostgresHardwareSnapshotCatalog:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(
        self,
        projection: HardwareSnapshotProjection,
        bundle_ref: ObjectRef,
        *,
        idempotency_key: str,
    ) -> HardwareMetadataSnapshotRef:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        parameters = _parameters(projection, bundle_ref, idempotency_key)
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            _register_object(cursor, bundle_ref)
            cursor.execute(sql.PUBLISH_SNAPSHOT_SQL, parameters)
            if cursor.fetchone() is not None:
                for index, radio_id in enumerate(projection.radio_ids):
                    cursor.execute(
                        sql.PUBLISH_RADIO_SQL,
                        {
                            "snapshot_id": str(projection.ref.snapshot_id),
                            "radio_index": index,
                            "radio_id": radio_id,
                        },
                    )
                for chain in projection.chains:
                    cursor.execute(
                        sql.PUBLISH_CHAIN_SQL,
                        _chain_parameters(projection.ref.snapshot_id, chain),
                    )
                return projection.ref
            cursor.execute(sql.GET_CONFLICTS_SQL, parameters)
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise HardwareSnapshotConflictError(
                    "hardware identities identify different rows"
                )
            existing = _cataloged(cursor, rows[0])
            if (
                str(rows[0]["idempotency_key"]) != idempotency_key
                or existing.projection != projection
                or existing.bundle_ref != bundle_ref
            ):
                raise HardwareSnapshotConflictError(
                    "hardware identity or idempotency key identifies different content"
                )
            return existing.projection.ref

    def get(self, ref: HardwareMetadataSnapshotRef) -> CatalogedHardwareSnapshot | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(sql.GET_EXACT_SQL, _ref_parameters(ref))
            row = cursor.fetchone()
            return None if row is None else _cataloged(cursor, row)

    def resolve(
        self, snapshot_id: HardwareSnapshotId
    ) -> CatalogedHardwareSnapshot | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(sql.GET_BY_ID_SQL, {"snapshot_id": str(snapshot_id)})
            row = cursor.fetchone()
            return None if row is None else _cataloged(cursor, row)


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _register_object(cursor: psycopg.Cursor[dict[str, object]], ref: ObjectRef) -> None:
    parameters = _object_parameters(ref)
    cursor.execute(sql.REGISTER_OBJECT_SQL, parameters)
    cursor.execute(sql.VERIFY_OBJECT_SQL, parameters)
    row = cursor.fetchone()
    if row is None or (
        _integer(row["byte_count"], "byte_count") != ref.byte_count
        or row["media_type"] != ref.media_type
        or row["format_id"] != ref.format_id
        or row["locator"] != ref.locator
    ):
        raise HardwareObjectCollisionError("hardware object metadata conflicts")


def _parameters(
    projection: HardwareSnapshotProjection,
    bundle_ref: ObjectRef,
    idempotency_key: str,
) -> dict[str, object]:
    return {
        **_ref_parameters(projection.ref),
        "bundle_digest_algorithm": bundle_ref.digest.algorithm.value,
        "bundle_digest_value": bundle_ref.digest.value,
        "station_id": projection.station_id,
        "radio_count": len(projection.radio_ids),
        "chain_count": len(projection.chains),
        "idempotency_key": idempotency_key,
    }


def _ref_parameters(ref: HardwareMetadataSnapshotRef) -> dict[str, object]:
    return {
        "snapshot_id": str(ref.snapshot_id),
        "snapshot_digest_algorithm": ref.digest.algorithm.value,
        "snapshot_digest_value": ref.digest.value,
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


def _cataloged(
    cursor: psycopg.Cursor[dict[str, object]], row: dict[str, object]
) -> CatalogedHardwareSnapshot:
    ref = HardwareMetadataSnapshotRef(
        HardwareSnapshotId(str(row["snapshot_id"])),
        Digest(
            DigestAlgorithm(str(row["snapshot_digest_algorithm"])),
            str(row["snapshot_digest_value"]),
        ),
    )
    snapshot_parameters = {"snapshot_id": str(ref.snapshot_id)}
    cursor.execute(sql.GET_RADIOS_SQL, snapshot_parameters)
    radio_rows = cursor.fetchall()
    if len(radio_rows) != _integer(row["radio_count"], "radio_count"):
        raise PostgresHardwareSnapshotError("hardware radio count differs")
    radio_ids = tuple(str(value["radio_id"]) for value in radio_rows)
    cursor.execute(sql.GET_CHAINS_SQL, snapshot_parameters)
    chain_rows = cursor.fetchall()
    if len(chain_rows) != _integer(row["chain_count"], "chain_count"):
        raise PostgresHardwareSnapshotError("hardware chain count differs")
    projection = HardwareSnapshotProjection(
        ref,
        str(row["station_id"]),
        radio_ids,
        tuple(_chain(value) for value in chain_rows),
    )
    bundle = ObjectRef(
        Digest(
            DigestAlgorithm(str(row["bundle_digest_algorithm"])),
            str(row["bundle_digest_value"]),
        ),
        _integer(row["bundle_byte_count"], "bundle_byte_count"),
        str(row["bundle_media_type"]),
        str(row["bundle_format_id"]),
        str(row["bundle_locator"]),
    )
    return CatalogedHardwareSnapshot(projection, bundle)


def _chain(row: dict[str, object]) -> HardwareChainProjection:
    until = row["valid_until_utc_ns"]
    return HardwareChainProjection(
        _integer(row["chain_index"], "chain_index"),
        str(row["receiver_chain_id"]),
        str(row["radio_id"]),
        _integer(row["radio_channel"], "radio_channel"),
        str(row["lnb_id"]),
        None if row["polarization"] is None else str(row["polarization"]),
        None if row["cable_id"] is None else str(row["cable_id"]),
        _integer(row["valid_from_utc_ns"], "valid_from_utc_ns"),
        None if until is None else _integer(until, "valid_until_utc_ns"),
    )


def _chain_parameters(
    snapshot_id: HardwareSnapshotId, chain: HardwareChainProjection
) -> dict[str, object]:
    return {
        "snapshot_id": str(snapshot_id),
        "chain_index": chain.chain_index,
        "receiver_chain_id": chain.receiver_chain_id,
        "radio_id": chain.radio_id,
        "radio_channel": chain.radio_channel,
        "lnb_id": chain.lnb_id,
        "polarization": chain.polarization,
        "cable_id": chain.cable_id,
        "valid_from_utc_ns": chain.valid_from_utc_ns,
        "valid_until_utc_ns": chain.valid_until_utc_ns,
    }


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PostgresHardwareSnapshotError(f"database {name} is not an integer")
    return value
