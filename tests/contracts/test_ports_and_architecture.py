from __future__ import annotations

import ast
import inspect
from pathlib import Path

from leo_flow.contracts.ports import FeatureSetPublisher, RecordingPublisher
from leo_flow.storage.ports import BlobReader, BlobWriter

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "leo_flow"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_contract_foundation_never_imports_legacy_repository() -> None:
    for path in SRC.rglob("*.py"):
        assert not any(
            module == "leo_tracker" or module.startswith("leo_tracker.")
            for module in imported_modules(path)
        ), path


def test_storage_and_jobs_depend_only_on_contracts_and_stdlib() -> None:
    for component in (SRC / "storage", SRC / "jobs"):
        for path in component.rglob("*.py"):
            forbidden = {
                module
                for module in imported_modules(path)
                if module.startswith("leo_flow.")
                and not module.startswith("leo_flow.contracts")
            }
            assert not forbidden, f"{path}: {forbidden}"


def test_generic_blob_ports_have_no_delete_capability() -> None:
    assert "delete" not in BlobWriter.__dict__
    assert "delete" not in BlobReader.__dict__


def test_publication_ports_require_explicit_idempotency_keys() -> None:
    assert "idempotency_key" in inspect.signature(RecordingPublisher.publish).parameters
    assert (
        "idempotency_key" in inspect.signature(FeatureSetPublisher.publish).parameters
    )


def test_contract_source_does_not_encode_nfs_control_plane_layouts() -> None:
    forbidden = (".job", ".running", "/mnt/qnap", "done/", "failed/")
    for path in (SRC / "contracts").rglob("*.py"):
        text = path.read_text()
        assert not any(token in text for token in forbidden), path
