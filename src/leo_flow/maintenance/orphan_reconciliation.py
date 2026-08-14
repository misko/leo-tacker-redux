"""Bounded, maintenance-only reconciliation of unregistered CAS leaves."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import partial
from typing import Protocol

from leo_flow.contracts.core import Digest


class InventoryKind(str, Enum):
    CANONICAL = "canonical"
    CORRUPT_NAME = "corrupt_name"


class ReconciliationCategory(str, Enum):
    LIVE = "live"
    REGISTERED = "registered"
    TOMBSTONE = "tombstone"
    IN_FLIGHT = "in_flight"
    UNREGISTERED = "unregistered"
    CORRUPT_NAME = "corrupt_name"


class OrphanReconciliationError(RuntimeError):
    """An operator reconciliation run failed without exposing internals."""


@dataclass(frozen=True, order=True)
class FileEvidence:
    """Filesystem identity evidence, never a retention or scientific policy."""

    byte_count: int
    device: int
    inode: int
    parent_device: int
    parent_inode: int
    mtime_ns: int

    def __post_init__(self) -> None:
        if (
            min(
                self.byte_count,
                self.device,
                self.inode,
                self.parent_device,
                self.parent_inode,
                self.mtime_ns,
            )
            < 0
        ):
            raise ValueError("filesystem evidence values must be non-negative")


@dataclass(frozen=True)
class InventoryEntry:
    key: str
    kind: InventoryKind
    digest: Digest | None = None
    evidence: FileEvidence | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("inventory key cannot be empty")
        if self.kind is InventoryKind.CANONICAL:
            if self.digest is None or self.evidence is None:
                raise ValueError("canonical inventory entries require exact evidence")
        elif self.digest is not None or self.evidence is not None:
            raise ValueError("corrupt-name entries cannot imply an object identity")

    @property
    def locator(self) -> str:
        if self.digest is None:
            raise ValueError("corrupt-name entries have no locator")
        return f"cas:sha256:{self.digest.value}"


@dataclass(frozen=True)
class InventoryBatch:
    entries: tuple[InventoryEntry, ...]
    next_cursor: str | None


class BoundedCasInventory(Protocol):
    """Explicit operator inventory; runtime services never receive this port."""

    def inventory(self, *, after: str | None, limit: int) -> InventoryBatch: ...

    def exact(self, entry: InventoryEntry) -> bool: ...


class UnregisteredObjectDeleter(Protocol):
    """Delete only an exact inventory entry after rechecking its evidence."""

    def delete_exact(self, entry: InventoryEntry) -> None: ...


class OrphanReconciliationCatalog(Protocol):
    def pending_claims(self, *, limit: int) -> tuple[PendingClaim, ...]: ...

    def observe(self, entry: InventoryEntry) -> ReconciliationCategory: ...

    def claim(
        self, entry: InventoryEntry, *, minimum_age_seconds: int
    ) -> str | None: ...

    def delete_under_fence(
        self,
        entry: InventoryEntry,
        *,
        claim_token: str,
        delete: Callable[[], None],
    ) -> str: ...


@dataclass(frozen=True)
class ReconciliationResult:
    key: str
    category: ReconciliationCategory
    outcome: str


@dataclass(frozen=True)
class ReconciliationReport:
    results: tuple[ReconciliationResult, ...]
    next_cursor: str | None
    report_only: bool


@dataclass(frozen=True)
class PendingClaim:
    entry: InventoryEntry
    claim_token: str


def reconcile_unregistered_objects(
    inventory: BoundedCasInventory,
    catalog: OrphanReconciliationCatalog,
    *,
    after: str | None = None,
    limit: int = 100,
    minimum_age_seconds: int = 86_400,
    deleter: UnregisteredObjectDeleter | None = None,
) -> ReconciliationReport:
    """Inventory one deterministic page and optionally delete proven orphans.

    Absence of a deleter is the report-only mode. The catalog's database clock,
    not filesystem timestamps or this process's clock, determines object age.
    """

    if limit <= 0 or minimum_age_seconds <= 0:
        raise ValueError("limit and minimum_age_seconds must be positive")
    results: list[ReconciliationResult] = []
    pending = () if deleter is None else catalog.pending_claims(limit=limit)
    for claim in pending:
        assert deleter is not None
        outcome = catalog.delete_under_fence(
            claim.entry,
            claim_token=claim.claim_token,
            delete=partial(deleter.delete_exact, claim.entry),
        )
        results.append(
            ReconciliationResult(
                claim.entry.key, ReconciliationCategory.IN_FLIGHT, outcome
            )
        )
    remaining = limit - len(pending)
    if remaining == 0:
        return ReconciliationReport(tuple(results), after, False)
    batch = inventory.inventory(after=after, limit=remaining)
    for entry in batch.entries:
        if entry.kind is InventoryKind.CORRUPT_NAME:
            results.append(
                ReconciliationResult(
                    entry.key, ReconciliationCategory.CORRUPT_NAME, "reported"
                )
            )
            continue
        category = catalog.observe(entry)
        if category is not ReconciliationCategory.UNREGISTERED:
            results.append(ReconciliationResult(entry.key, category, "reported"))
            continue
        if deleter is None:
            results.append(ReconciliationResult(entry.key, category, "reported"))
            continue
        claim_token = catalog.claim(entry, minimum_age_seconds=minimum_age_seconds)
        if claim_token is None:
            results.append(ReconciliationResult(entry.key, category, "not_eligible"))
            continue
        outcome = catalog.delete_under_fence(
            entry,
            claim_token=claim_token,
            delete=partial(deleter.delete_exact, entry),
        )
        results.append(ReconciliationResult(entry.key, category, outcome))
    return ReconciliationReport(tuple(results), batch.next_cursor, deleter is None)


def deterministic_claim_token(entry: InventoryEntry) -> str:
    """Return the stable token for one exact observed filesystem identity."""

    if entry.digest is None or entry.evidence is None:
        raise ValueError("only canonical entries can be claimed")
    payload = (
        "leo-orphan-claim-v1\0"
        f"{entry.digest.algorithm.value}\0{entry.digest.value}\0"
        f"{entry.evidence.byte_count}\0{entry.evidence.device}\0"
        f"{entry.evidence.inode}\0{entry.evidence.parent_device}\0"
        f"{entry.evidence.parent_inode}\0{entry.evidence.mtime_ns}"
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()
