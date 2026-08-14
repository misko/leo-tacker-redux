from __future__ import annotations

from dataclasses import dataclass, field

from leo_flow.contracts.core import Digest
from leo_flow.maintenance.orphan_reconciliation import (
    FileEvidence,
    InventoryBatch,
    InventoryEntry,
    InventoryKind,
    PendingClaim,
    ReconciliationCategory,
    reconcile_unregistered_objects,
)


def _entry(label: str) -> InventoryEntry:
    digest = Digest.sha256(label.encode())
    return InventoryEntry(
        f"sha256/{digest.value[:2]}/{digest.value}",
        InventoryKind.CANONICAL,
        digest,
        FileEvidence(len(label), 1, len(label), 1, 2, 10),
    )


@dataclass
class Inventory:
    entries: tuple[InventoryEntry, ...]

    def inventory(self, *, after: str | None, limit: int) -> InventoryBatch:
        values = tuple(
            entry for entry in self.entries if after is None or entry.key > after
        )
        selected = values[:limit]
        return InventoryBatch(
            selected, selected[-1].key if len(selected) == limit else None
        )

    def exact(self, entry: InventoryEntry) -> bool:
        return entry in self.entries


@dataclass
class Catalog:
    category: ReconciliationCategory = ReconciliationCategory.UNREGISTERED
    pending: tuple[PendingClaim, ...] = ()
    calls: list[str] = field(default_factory=list)

    def pending_claims(self, *, limit: int) -> tuple[PendingClaim, ...]:
        return self.pending[:limit]

    def observe(self, entry: InventoryEntry) -> ReconciliationCategory:
        self.calls.append(f"observe:{entry.key}")
        return self.category

    def claim(self, entry: InventoryEntry, *, minimum_age_seconds: int) -> str | None:
        self.calls.append(f"claim:{minimum_age_seconds}")
        return "token"

    def delete_under_fence(self, entry, *, claim_token, delete):
        self.calls.append(f"fence:{claim_token}")
        delete()
        return "deleted"


@dataclass
class Deleter:
    values: list[InventoryEntry] = field(default_factory=list)

    def delete_exact(self, entry: InventoryEntry) -> None:
        self.values.append(entry)


def test_report_only_observes_without_claim_or_delete() -> None:
    entry = _entry("one")
    catalog = Catalog()

    report = reconcile_unregistered_objects(Inventory((entry,)), catalog)

    assert report.report_only
    assert report.results[0].outcome == "reported"
    assert catalog.calls == [f"observe:{entry.key}"]


def test_delete_mode_claims_and_finalizes_only_through_catalog_fence() -> None:
    entry = _entry("one")
    catalog = Catalog()
    deleter = Deleter()

    report = reconcile_unregistered_objects(
        Inventory((entry,)), catalog, minimum_age_seconds=60, deleter=deleter
    )

    assert report.results[0].outcome == "deleted"
    assert catalog.calls == [f"observe:{entry.key}", "claim:60", "fence:token"]
    assert deleter.values == [entry]


def test_restart_resumes_persisted_claim_before_new_inventory() -> None:
    claimed = _entry("claimed")
    new = _entry("new")
    catalog = Catalog(pending=(PendingClaim(claimed, "persisted-token"),))
    deleter = Deleter()

    report = reconcile_unregistered_objects(
        Inventory((new,)), catalog, limit=1, deleter=deleter
    )

    assert [(item.category, item.outcome) for item in report.results] == [
        (ReconciliationCategory.IN_FLIGHT, "deleted")
    ]
    assert catalog.calls == ["fence:persisted-token"]
    assert deleter.values == [claimed]


def test_corrupt_names_are_never_sent_to_catalog() -> None:
    corrupt = InventoryEntry("sha256/ZZ", InventoryKind.CORRUPT_NAME)
    catalog = Catalog()

    report = reconcile_unregistered_objects(Inventory((corrupt,)), catalog)

    assert report.results[0].category is ReconciliationCategory.CORRUPT_NAME
    assert catalog.calls == []
