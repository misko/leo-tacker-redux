"""Delete-capable filesystem adapter available only to maintenance composition."""

from __future__ import annotations

from pathlib import Path

from leo_flow.contracts.core import DigestAlgorithm
from leo_flow.contracts.storage import ObjectRef


class MaintenanceFileSystemBlobDeleter:
    def __init__(self, root: Path) -> None:
        self._root = root

    def delete(self, ref: ObjectRef) -> None:
        if ref.digest.algorithm is not DigestAlgorithm.SHA256:
            raise ValueError("maintenance filesystem supports only SHA-256")
        if ref.locator != f"cas:sha256:{ref.digest.value}":
            raise ValueError("object locator is not the exact maintenance CAS identity")
        path = self._root / "sha256" / ref.digest.value[:2] / ref.digest.value
        try:
            path.unlink()
        except FileNotFoundError:
            # A prior process may have deleted bytes before losing its DB lease.
            return
