"""Explicit bounded filesystem inventory and deletion adapters for CAS orphans."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Self

from leo_flow.contracts.core import Digest, DigestAlgorithm

from .orphan_reconciliation import (
    FileEvidence,
    InventoryBatch,
    InventoryEntry,
    InventoryKind,
)

_LOWER_HEX_2 = re.compile(r"[0-9a-f]{2}")
_LOWER_HEX_64 = re.compile(r"[0-9a-f]{64}")
_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_LEAF_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


class InventoryBudgetExceeded(RuntimeError):
    """The explicit scan budget was exhausted before a stable page was built."""


class EvidenceChangedError(RuntimeError):
    """The canonical name or its pinned parent now has different evidence."""


class FileSystemCasInventory:
    """Maintenance-only inventory of the adapter's exact SHA-256 leaf layout.

    The root directory is pinned by file descriptor at construction. A page may
    inspect at most ``scan_budget`` directory entries; it fails closed rather
    than claiming bounded I/O after sorting an arbitrarily large shard.
    """

    def __init__(self, root: Path, *, scan_budget: int = 100_000) -> None:
        if scan_budget <= 0:
            raise ValueError("scan_budget must be positive")
        self._root_fd = os.open(root, _DIRECTORY_FLAGS)
        self._scan_budget = scan_budget

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def inventory(self, *, after: str | None, limit: int) -> InventoryBatch:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if after is not None and (
            not after or after.startswith("/") or ".." in after.split("/")
        ):
            raise ValueError("inventory cursor is not a relative CAS key")
        entries = sorted(self._all_entries(), key=lambda entry: entry.key)
        selected = tuple(
            entry for entry in entries if after is None or entry.key > after
        )[:limit]
        if not selected:
            return InventoryBatch((), None)
        has_more = any(entry.key > selected[-1].key for entry in entries)
        return InventoryBatch(selected, selected[-1].key if has_more else None)

    def exact(self, entry: InventoryEntry) -> bool:
        if entry.kind is not InventoryKind.CANONICAL or entry.evidence is None:
            return False
        try:
            with self._open_parent(entry) as parent_fd:
                current = _leaf_evidence(parent_fd, _leaf_name(entry), entry.evidence)
        except (FileNotFoundError, NotADirectoryError, OSError):
            return False
        return current == entry.evidence

    def unlink_exact(self, entry: InventoryEntry) -> None:
        if entry.evidence is None:
            raise ValueError("corrupt-name entry cannot be deleted")
        try:
            with self._open_parent(entry) as parent_fd:
                current = _leaf_evidence(parent_fd, _leaf_name(entry), entry.evidence)
                if current != entry.evidence:
                    raise EvidenceChangedError(
                        "CAS leaf no longer matches claimed evidence"
                    )
                os.unlink(_leaf_name(entry), dir_fd=parent_fd)
        except FileNotFoundError:
            # A crash after unlink but before DB completion is safely resumable.
            return

    def _all_entries(self) -> Iterator[InventoryEntry]:
        budget = [self._scan_budget]
        try:
            sha256_fd = os.open("sha256", _DIRECTORY_FLAGS, dir_fd=self._root_fd)
        except FileNotFoundError:
            return
        try:
            for shard_name in _names(sha256_fd, budget):
                shard_key = f"sha256/{shard_name}"
                if _LOWER_HEX_2.fullmatch(shard_name) is None:
                    yield InventoryEntry(shard_key, InventoryKind.CORRUPT_NAME)
                    continue
                try:
                    shard_fd = os.open(shard_name, _DIRECTORY_FLAGS, dir_fd=sha256_fd)
                except OSError:
                    yield InventoryEntry(shard_key, InventoryKind.CORRUPT_NAME)
                    continue
                try:
                    parent_stat = os.fstat(shard_fd)
                    for leaf_name in _names(shard_fd, budget):
                        key = f"{shard_key}/{leaf_name}"
                        if _LOWER_HEX_64.fullmatch(
                            leaf_name
                        ) is None or not leaf_name.startswith(shard_name):
                            yield InventoryEntry(key, InventoryKind.CORRUPT_NAME)
                            continue
                        try:
                            leaf_stat = _open_leaf_stat(shard_fd, leaf_name)
                        except OSError:
                            yield InventoryEntry(key, InventoryKind.CORRUPT_NAME)
                            continue
                        yield InventoryEntry(
                            key,
                            InventoryKind.CANONICAL,
                            Digest(DigestAlgorithm.SHA256, leaf_name),
                            _evidence(leaf_stat, parent_stat),
                        )
                finally:
                    os.close(shard_fd)
        finally:
            os.close(sha256_fd)

    def _open_parent(self, entry: InventoryEntry) -> _DirectoryFd:
        if entry.digest is None or entry.evidence is None:
            raise ValueError("corrupt-name entry has no canonical parent")
        expected_key = f"sha256/{entry.digest.value[:2]}/{entry.digest.value}"
        if entry.key != expected_key:
            raise ValueError("inventory key does not match digest identity")
        sha256_fd = os.open("sha256", _DIRECTORY_FLAGS, dir_fd=self._root_fd)
        try:
            shard_fd = os.open(
                entry.digest.value[:2], _DIRECTORY_FLAGS, dir_fd=sha256_fd
            )
        finally:
            os.close(sha256_fd)
        parent = os.fstat(shard_fd)
        if (
            parent.st_dev != entry.evidence.parent_device
            or parent.st_ino != entry.evidence.parent_inode
        ):
            os.close(shard_fd)
            raise EvidenceChangedError("CAS shard identity changed")
        return _DirectoryFd(shard_fd)


class MaintenanceOrphanFileDeleter:
    """Exact-evidence unlink port, composed only by the maintenance CLI."""

    def __init__(self, inventory: FileSystemCasInventory) -> None:
        self._inventory = inventory

    def delete_exact(self, entry: InventoryEntry) -> None:
        self._inventory.unlink_exact(entry)


class _DirectoryFd:
    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    def __enter__(self) -> int:
        return self._descriptor

    def __exit__(self, *_args: object) -> None:
        os.close(self._descriptor)


def _names(directory_fd: int, budget: list[int]) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(directory_fd) as iterator:
        for item in iterator:
            budget[0] -= 1
            if budget[0] < 0:
                raise InventoryBudgetExceeded("CAS inventory scan budget exceeded")
            names.append(item.name)
    return tuple(names)


def _open_leaf_stat(parent_fd: int, leaf_name: str) -> os.stat_result:
    descriptor = os.open(leaf_name, _LEAF_FLAGS, dir_fd=parent_fd)
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode):
            raise OSError("CAS leaf is not a regular file")
        return value
    finally:
        os.close(descriptor)


def _leaf_evidence(
    parent_fd: int, leaf_name: str, expected: FileEvidence
) -> FileEvidence:
    return _evidence(_open_leaf_stat(parent_fd, leaf_name), os.fstat(parent_fd))


def _evidence(value: os.stat_result, parent: os.stat_result) -> FileEvidence:
    return FileEvidence(
        value.st_size,
        value.st_dev,
        value.st_ino,
        parent.st_dev,
        parent.st_ino,
        value.st_mtime_ns,
    )


def _leaf_name(entry: InventoryEntry) -> str:
    if entry.digest is None:
        raise ValueError("corrupt-name entry has no leaf")
    return entry.digest.value
