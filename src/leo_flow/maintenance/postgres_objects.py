"""PostgreSQL inventory adapter for the operator-owned blob audit."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from leo_flow.contracts.core import Digest, DigestAlgorithm
from leo_flow.contracts.storage import ObjectRef

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class PostgresObjectInventory:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def objects(self) -> Iterable[ObjectRef]:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                """
                SELECT digest_algorithm, digest_value, byte_count,
                       media_type, format_id, locator
                FROM object_blob
                ORDER BY digest_algorithm, digest_value
                """
            ).fetchall()
        return tuple(_ref(row) for row in rows)


def service_connection_factory(
    service_name: str, service_file: Path
) -> ConnectionFactory:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", service_name):
        raise ValueError("service_name must be a token")
    if not service_file.is_file() or service_file.stat().st_mode & 0o077:
        raise ValueError("libpq service file must be a private regular file")
    return lambda: psycopg.connect(
        service=service_name,
        servicefile=str(service_file.resolve()),
        row_factory=dict_row,
    )


def _ref(row: dict[str, object]) -> ObjectRef:
    byte_count = row["byte_count"]
    if isinstance(byte_count, bool) or not isinstance(byte_count, int):
        raise TypeError("database byte_count is not an integer")
    return ObjectRef(
        Digest(
            DigestAlgorithm(str(row["digest_algorithm"])),
            str(row["digest_value"]),
        ),
        byte_count,
        str(row["media_type"]),
        str(row["format_id"]),
        str(row["locator"]),
    )
