from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path("src/leo_flow")


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_platform_has_no_legacy_or_scientific_runtime_dependencies() -> None:
    forbidden_prefixes = (
        "leo_tracker",
        "leo_flow.analysis",
        "leo_flow.capture",
        "leo_flow.dashboard",
        "numpy",
        "h5py",
    )
    for component in (SOURCE_ROOT / "storage", SOURCE_ROOT / "jobs"):
        for path in component.rglob("*.py"):
            assert not any(
                module == prefix or module.startswith(prefix + ".")
                for module in imports(path)
                for prefix in forbidden_prefixes
            ), path


def test_no_control_plane_marker_protocol_is_created() -> None:
    forbidden = ('".job"', '".running"', '"done/"', '"failed/"', "/mnt/qnap")
    for component in (SOURCE_ROOT / "storage", SOURCE_ROOT / "jobs"):
        for path in component.rglob("*.py"):
            text = path.read_text()
            assert not any(token in text for token in forbidden), path


def test_blob_writer_capability_is_not_redeclared_by_consumers() -> None:
    for path in SOURCE_ROOT.rglob("*.py"):
        if path != SOURCE_ROOT / "storage" / "ports.py":
            assert "class _BlobWriter" not in path.read_text(), path
