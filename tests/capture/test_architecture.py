from __future__ import annotations

import ast
from pathlib import Path

CAPTURE = Path(__file__).resolve().parents[2] / "src" / "leo_flow" / "capture"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_capture_imports_only_contracts_storage_ports_and_stdlib() -> None:
    allowed = ("leo_flow.contracts", "leo_flow.storage.ports")
    forbidden_terms = (
        "analysis",
        "dashboard",
        "ephemeris",
        "tle",
        "h5py",
        "sigmf",
        "sqlalchemy",
        "psycopg",
        "leo_tracker",
    )
    for path in CAPTURE.rglob("*.py"):
        modules = imported_modules(path)
        assert not {
            module
            for module in modules
            if module.startswith("leo_flow.") and not module.startswith(allowed)
        }, path
        assert not {
            module
            for module in modules
            if any(term in module.lower() for term in forbidden_terms)
        }, path


def test_capture_source_has_no_analysis_job_or_nfs_protocol() -> None:
    forbidden = (
        "create_analysis_job",
        ".job",
        ".running",
        "/mnt/qnap",
        "done/",
        "failed/",
    )
    for path in CAPTURE.rglob("*.py"):
        text = path.read_text()
        assert not any(token in text for token in forbidden), path
