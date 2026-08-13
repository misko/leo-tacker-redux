from __future__ import annotations

import ast
from pathlib import Path

CAPTURE = Path(__file__).resolve().parents[3] / "src" / "leo_flow" / "capture"
OWNED = (CAPTURE / "drivers", CAPTURE / "qualification")


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_adapter_has_no_analysis_dashboard_storage_or_legacy_dependency() -> None:
    forbidden = (
        "leo_flow.analysis",
        "leo_flow.dashboard",
        "leo_flow.storage",
        "leo_flow.ephemeris",
        "leo_tracker",
    )
    for directory in OWNED:
        for path in directory.rglob("*.py"):
            modules = imports(path)
            assert not {
                module
                for module in modules
                if any(module.startswith(prefix) for prefix in forbidden)
            }, path


def test_pyadi_and_numpy_are_never_eagerly_imported() -> None:
    for directory in OWNED:
        for path in directory.rglob("*.py"):
            modules = imports(path)
            assert "adi" not in modules
            assert "numpy" not in modules


def test_adapter_source_has_no_nfs_or_analysis_protocol() -> None:
    forbidden = ("/mnt/", ".job", ".running", "threshold", "tle")
    for directory in OWNED:
        for path in directory.rglob("*.py"):
            text = path.read_text().lower()
            assert not any(token in text for token in forbidden), path
