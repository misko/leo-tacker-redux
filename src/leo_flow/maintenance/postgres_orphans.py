"""PostgreSQL fence adapter for maintenance-only orphan reconciliation."""

from __future__ import annotations

from collections.abc import Callable

from leo_flow.contracts.core import Digest, DigestAlgorithm

from .orphan_reconciliation import (
    FileEvidence,
    InventoryEntry,
    InventoryKind,
    OrphanReconciliationCatalog,
    PendingClaim,
    ReconciliationCategory,
    deterministic_claim_token,
)
from .postgres_objects import ConnectionFactory


class PostgresOrphanReconciliationCatalog(OrphanReconciliationCatalog):
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def pending_claims(self, *, limit: int) -> tuple[PendingClaim, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                """
                SELECT digest_algorithm, digest_value, byte_count, locator,
                       filesystem_device, filesystem_inode,
                       filesystem_parent_device, filesystem_parent_inode,
                       filesystem_mtime_ns,
                       claim_token
                  FROM object_orphan_observation
                 WHERE state = 'claimed'
                 ORDER BY digest_algorithm, digest_value
                 LIMIT %(limit)s
                """,
                {"limit": limit},
            ).fetchall()
        return tuple(
            PendingClaim(_entry_from_row(row), str(row["claim_token"])) for row in rows
        )

    def observe(self, entry: InventoryEntry) -> ReconciliationCategory:
        digest, evidence = _canonical(entry)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT observe_unregistered_object(
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    digest.algorithm.value,
                    digest.value,
                    evidence.byte_count,
                    entry.locator,
                    evidence.device,
                    evidence.inode,
                    evidence.parent_device,
                    evidence.parent_inode,
                    evidence.mtime_ns,
                ),
            ).fetchone()
        if row is None:
            raise TypeError("database did not classify inventory entry")
        return ReconciliationCategory(str(next(iter(row.values()))))

    def claim(self, entry: InventoryEntry, *, minimum_age_seconds: int) -> str | None:
        if minimum_age_seconds <= 0:
            raise ValueError("minimum_age_seconds must be positive")
        digest, evidence = _canonical(entry)
        proposed = deterministic_claim_token(entry)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT claim_unregistered_object(
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    digest.algorithm.value,
                    digest.value,
                    evidence.byte_count,
                    entry.locator,
                    evidence.device,
                    evidence.inode,
                    evidence.parent_device,
                    evidence.parent_inode,
                    evidence.mtime_ns,
                    proposed,
                    minimum_age_seconds,
                ),
            ).fetchone()
        if row is None:
            raise TypeError("database did not return an orphan claim outcome")
        value = next(iter(row.values()))
        return None if value is None else str(value)

    def delete_under_fence(
        self,
        entry: InventoryEntry,
        *,
        claim_token: str,
        delete: Callable[[], None],
    ) -> str:
        digest, evidence = _canonical(entry)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT orphan_claim_is_current(
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    digest.algorithm.value,
                    digest.value,
                    evidence.byte_count,
                    entry.locator,
                    evidence.device,
                    evidence.inode,
                    evidence.parent_device,
                    evidence.parent_inode,
                    evidence.mtime_ns,
                    claim_token,
                ),
            ).fetchone()
            if row is None or next(iter(row.values())) is not True:
                return "skipped"
            try:
                # The advisory transaction lock acquired by the function above
                # remains held during this exact-evidence external side effect.
                delete()
            except Exception as error:
                detail = f"orphan-delete:{type(error).__name__}"
                failed = connection.execute(
                    """
                    SELECT record_unregistered_object_delete_failure(%s, %s, %s, %s)
                    """,
                    (digest.algorithm.value, digest.value, claim_token, detail),
                ).fetchone()
                if failed is None or next(iter(failed.values())) is not True:
                    raise RuntimeError(
                        "orphan delete failure lost its fence"
                    ) from error
                return "delete_failed"
            completed = connection.execute(
                """
                SELECT complete_unregistered_object_delete(%s, %s, %s)
                """,
                (digest.algorithm.value, digest.value, claim_token),
            ).fetchone()
            if completed is None or next(iter(completed.values())) is not True:
                raise RuntimeError("orphan delete completion lost its fence")
        return "deleted"


def _canonical(entry: InventoryEntry) -> tuple[Digest, FileEvidence]:
    if (
        entry.kind is not InventoryKind.CANONICAL
        or entry.digest is None
        or entry.evidence is None
    ):
        raise ValueError("orphan catalog accepts only canonical inventory entries")
    return entry.digest, entry.evidence


def _entry_from_row(row: dict[str, object]) -> InventoryEntry:
    digest = Digest(
        DigestAlgorithm(str(row["digest_algorithm"])), str(row["digest_value"])
    )
    evidence = FileEvidence(
        _integer(row["byte_count"]),
        _integer(row["filesystem_device"]),
        _integer(row["filesystem_inode"]),
        _integer(row["filesystem_parent_device"]),
        _integer(row["filesystem_parent_inode"]),
        _integer(row["filesystem_mtime_ns"]),
    )
    return InventoryEntry(
        f"sha256/{digest.value[:2]}/{digest.value}",
        InventoryKind.CANONICAL,
        digest,
        evidence,
    )


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("database evidence is not an integer")
    return value
