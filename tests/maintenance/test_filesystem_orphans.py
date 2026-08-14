from __future__ import annotations

import hashlib
import io
import os

import pytest

from leo_flow.contracts.core import Digest
from leo_flow.maintenance.filesystem_orphans import (
    EvidenceChangedError,
    FileSystemCasInventory,
    InventoryBudgetExceeded,
    MaintenanceOrphanFileDeleter,
)
from leo_flow.maintenance.orphan_reconciliation import InventoryKind
from leo_flow.storage import FileSystemBlobStore


def _put(root, payload: bytes):
    digest = Digest.sha256(payload)
    return FileSystemBlobStore(root).put(
        io.BytesIO(payload),
        expected_digest=digest,
        expected_bytes=len(payload),
        media_type="application/octet-stream",
        format_id="test-v1",
        idempotency_key=digest.value,
    )


def test_inventory_is_canonical_bounded_and_cursor_deterministic(tmp_path) -> None:
    first = _put(tmp_path, b"first")
    second = _put(tmp_path, b"second")
    corrupt = tmp_path / "sha256" / "zz"
    corrupt.mkdir(parents=True)

    inventory = FileSystemCasInventory(tmp_path)
    page_one = inventory.inventory(after=None, limit=1)
    assert len(page_one.entries) == 1
    assert page_one.next_cursor == page_one.entries[0].key
    page_two = inventory.inventory(after=page_one.next_cursor, limit=10)
    combined = page_one.entries + page_two.entries

    assert [entry.key for entry in combined] == sorted(entry.key for entry in combined)
    canonical = [entry for entry in combined if entry.kind is InventoryKind.CANONICAL]
    assert {entry.digest for entry in canonical} == {first.digest, second.digest}
    assert any(entry.key == "sha256/zz" for entry in combined)
    assert all(inventory.exact(entry) for entry in canonical)


def test_invalid_leaf_and_symlink_are_reported_but_never_given_identity(
    tmp_path,
) -> None:
    shard = tmp_path / "sha256" / "aa"
    shard.mkdir(parents=True)
    (shard / "not-a-digest").write_bytes(b"x")
    os.symlink(shard / "not-a-digest", shard / ("a" * 64))

    entries = FileSystemCasInventory(tmp_path).inventory(after=None, limit=10).entries

    assert len(entries) == 2
    assert all(entry.kind is InventoryKind.CORRUPT_NAME for entry in entries)
    assert all(entry.digest is None and entry.evidence is None for entry in entries)


def test_deleter_rechecks_exact_inode_size_and_mtime(tmp_path) -> None:
    ref = _put(tmp_path, b"original")
    inventory = FileSystemCasInventory(tmp_path)
    entry = inventory.inventory(after=None, limit=1).entries[0]
    path = tmp_path / "sha256" / ref.digest.value[:2] / ref.digest.value
    path.unlink()
    path.write_bytes(b"changed!")

    with pytest.raises(EvidenceChangedError):
        MaintenanceOrphanFileDeleter(inventory).delete_exact(entry)
    assert path.exists()


def test_missing_leaf_is_idempotent_after_crash(tmp_path) -> None:
    ref = _put(tmp_path, b"resume")
    inventory = FileSystemCasInventory(tmp_path)
    entry = inventory.inventory(after=None, limit=1).entries[0]
    path = tmp_path / "sha256" / ref.digest.value[:2] / ref.digest.value
    path.unlink()

    MaintenanceOrphanFileDeleter(inventory).delete_exact(entry)


def test_digest_name_does_not_substitute_for_byte_verification(tmp_path) -> None:
    payload = b"real"
    digest = hashlib.sha256(payload).hexdigest()
    path = tmp_path / "sha256" / digest[:2] / digest
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fake")

    entry = FileSystemCasInventory(tmp_path).inventory(after=None, limit=1).entries[0]

    # Inventory evidence describes what exists; it never asserts the filename's
    # digest is scientifically valid. Existing object audit owns content hashing.
    assert entry.evidence is not None and entry.evidence.byte_count == 4


def test_shard_replacement_is_not_followed_for_delete(tmp_path) -> None:
    ref = _put(tmp_path, b"pinned-shard")
    inventory = FileSystemCasInventory(tmp_path)
    entry = inventory.inventory(after=None, limit=1).entries[0]
    shard = tmp_path / "sha256" / ref.digest.value[:2]
    moved = tmp_path / "sha256" / f"{ref.digest.value[:2]}-old"
    shard.rename(moved)
    shard.mkdir()
    replacement = shard / ref.digest.value
    replacement.write_bytes(b"replacement")

    with pytest.raises(EvidenceChangedError):
        MaintenanceOrphanFileDeleter(inventory).delete_exact(entry)
    assert replacement.read_bytes() == b"replacement"


def test_root_fd_is_pinned_across_path_replacement(tmp_path) -> None:
    root = tmp_path / "cas"
    root.mkdir()
    ref = _put(root, b"pinned-root")
    inventory = FileSystemCasInventory(root)
    entry = inventory.inventory(after=None, limit=1).entries[0]
    old_root = tmp_path / "old-cas"
    root.rename(old_root)
    replacement = root / "sha256" / ref.digest.value[:2] / ref.digest.value
    replacement.parent.mkdir(parents=True)
    replacement.write_bytes(b"replacement")

    MaintenanceOrphanFileDeleter(inventory).delete_exact(entry)

    assert replacement.read_bytes() == b"replacement"
    assert not (old_root / entry.key).exists()


def test_inventory_fails_closed_when_scan_budget_is_exhausted(tmp_path) -> None:
    shard = tmp_path / "sha256" / "aa"
    shard.mkdir(parents=True)
    (shard / "one").write_bytes(b"1")
    (shard / "two").write_bytes(b"2")

    with pytest.raises(InventoryBudgetExceeded):
        FileSystemCasInventory(tmp_path, scan_budget=2).inventory(after=None, limit=1)
