from pathlib import Path

import pytest

from leo_flow.contracts.core import Digest
from leo_flow.contracts.storage import ObjectRef
from leo_flow.maintenance.filesystem_gc import MaintenanceFileSystemBlobDeleter


def test_deleter_removes_only_exact_cas_identity(tmp_path: Path) -> None:
    digest = Digest.sha256(b"bytes")
    path = tmp_path / "sha256" / digest.value[:2] / digest.value
    path.parent.mkdir(parents=True)
    path.write_bytes(b"bytes")
    ref = ObjectRef(
        digest, 5, "application/octet-stream", "x", f"cas:sha256:{digest.value}"
    )

    deleter = MaintenanceFileSystemBlobDeleter(tmp_path)
    deleter.delete(ref)
    deleter.delete(ref)

    assert not path.exists()


def test_deleter_rejects_noncanonical_locator(tmp_path: Path) -> None:
    digest = Digest.sha256(b"bytes")
    ref = ObjectRef(digest, 5, "application/octet-stream", "x", "file:/tmp/bytes")
    with pytest.raises(ValueError):
        MaintenanceFileSystemBlobDeleter(tmp_path).delete(ref)
