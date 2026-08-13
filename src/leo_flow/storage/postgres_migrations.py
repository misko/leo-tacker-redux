"""Small ordered PostgreSQL migration runner for the first-slice schema."""

from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg


class MigrationError(RuntimeError):
    pass


def apply_migrations(
    connection: psycopg.Connection[tuple[object, ...]], migration_directory: Path
) -> tuple[str, ...]:
    files = sorted(migration_directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not files:
        raise MigrationError("no ordered SQL migrations found")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                name text PRIMARY KEY,
                sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
            """
        )
    connection.commit()
    applied: list[str] = []
    for path in files:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT sha256 FROM schema_migration WHERE name = %s", (path.name,)
            )
            existing = cursor.fetchone()
        if existing is not None:
            if str(existing[0]) != digest:
                raise MigrationError(f"applied migration changed: {path.name}")
            connection.commit()
            continue
        # End the read transaction so the migration and its receipt share one
        # top-level transaction rather than a savepoint inside caller state.
        connection.commit()
        sql = _without_transaction_wrapper(raw.decode("utf-8"))
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(sql)
            cursor.execute(
                "INSERT INTO schema_migration (name, sha256) VALUES (%s, %s)",
                (path.name, digest),
            )
        applied.append(path.name)
    return tuple(applied)


def _without_transaction_wrapper(sql: str) -> str:
    lines = sql.splitlines()
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if not nonempty:
        raise MigrationError("empty migration")
    first, last = nonempty[0], nonempty[-1]
    if (
        lines[first].strip().upper() != "BEGIN;"
        or lines[last].strip().upper() != "COMMIT;"
    ):
        raise MigrationError("migration must have an explicit BEGIN/COMMIT wrapper")
    return "\n".join(lines[first + 1 : last])
