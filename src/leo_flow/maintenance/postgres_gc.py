"""PostgreSQL adapter for the privileged object garbage-collection catalog."""

from __future__ import annotations

from datetime import UTC, datetime

from leo_flow.contracts.storage import ObjectRef

from .object_gc import GarbageCollectionCatalog
from .postgres_objects import ConnectionFactory, _ref


class PostgresGarbageCollectionCatalog(GarbageCollectionCatalog):
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def candidates(self, *, as_of_utc_ns: int, limit: int) -> tuple[ObjectRef, ...]:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                """
                SELECT b.digest_algorithm, b.digest_value, b.byte_count,
                       b.media_type, b.format_id, b.locator
                  FROM object_retention_status s
                  JOIN object_blob b USING (digest_algorithm, digest_value)
                 WHERE s.lifecycle_state IN ('live', 'gc_delete_failed')
                   AND s.policy_count > 0
                   AND s.all_policies_allow_delete
                   AND s.eligible_after <= %(as_of)s
                   AND s.live_reference_count = 0
                 ORDER BY s.eligible_after, b.digest_algorithm, b.digest_value
                 LIMIT %(limit)s
                """,
                {"as_of": _instant(as_of_utc_ns), "limit": limit},
            ).fetchall()
        return tuple(_ref(row) for row in rows)

    def claim(
        self,
        ref: ObjectRef,
        *,
        claim_token: str,
        claimed_at_utc_ns: int,
        claim_expires_at_utc_ns: int,
    ) -> ObjectRef | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT digest_algorithm, digest_value, byte_count,
                       media_type, format_id, locator
                  FROM gc_claim_object(%s, %s, %s, %s, %s)
                """,
                (
                    ref.digest.algorithm.value,
                    ref.digest.value,
                    claim_token,
                    _instant(claimed_at_utc_ns),
                    _instant(claim_expires_at_utc_ns),
                ),
            ).fetchone()
        return None if row is None else _ref(row)

    def complete(
        self, ref: ObjectRef, *, claim_token: str, completed_at_utc_ns: int
    ) -> bool:
        return self._outcome(
            "SELECT gc_complete_object_delete(%s, %s, %s, %s)",
            ref,
            claim_token,
            completed_at_utc_ns,
        )

    def fail(
        self,
        ref: ObjectRef,
        *,
        claim_token: str,
        failed_at_utc_ns: int,
        detail: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT gc_record_delete_failure(%s, %s, %s, %s, %s)",
                (
                    ref.digest.algorithm.value,
                    ref.digest.value,
                    claim_token,
                    _instant(failed_at_utc_ns),
                    detail,
                ),
            ).fetchone()
        if row is None:
            raise TypeError("database did not return an outcome")
        return _boolean_result(row)

    def _outcome(
        self, statement: str, ref: ObjectRef, token: str, at_utc_ns: int
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                statement,
                (
                    ref.digest.algorithm.value,
                    ref.digest.value,
                    token,
                    _instant(at_utc_ns),
                ),
            ).fetchone()
        if row is None:
            raise TypeError("database did not return an outcome")
        return _boolean_result(row)


def _instant(utc_ns: int) -> datetime:
    if utc_ns < 0:
        raise ValueError("UTC nanoseconds must be non-negative")
    return datetime.fromtimestamp(utc_ns / 1_000_000_000, tz=UTC)


def _boolean_result(row: dict[str, object]) -> bool:
    value = next(iter(row.values()))
    if not isinstance(value, bool):
        raise TypeError("database outcome is not boolean")
    return value
