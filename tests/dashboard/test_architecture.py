from __future__ import annotations

import ast
import inspect
from pathlib import Path

from leo_flow.dashboard.repository import InMemoryDashboardRepository

DASHBOARD = Path(__file__).resolve().parents[2] / "src" / "leo_flow" / "dashboard"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_dashboard_uses_only_contracts_and_stdlib() -> None:
    forbidden = (
        "sqlalchemy",
        "psycopg",
        "numpy",
        "scipy",
        "h5py",
        "sigmf",
        "django",
        "flask",
        "fastapi",
        "leo_tracker",
    )
    for path in DASHBOARD.rglob("*.py"):
        modules = imported_modules(path)
        assert not {
            module
            for module in modules
            if module.startswith("leo_flow.")
            and not module.startswith("leo_flow.contracts")
        }, path
        assert not {
            module
            for module in modules
            if any(term in module.lower() for term in forbidden)
        }, path


def test_dashboard_query_repository_has_no_mutation_methods() -> None:
    forbidden = {
        "add",
        "append",
        "create",
        "delete",
        "insert",
        "publish",
        "remove",
        "save",
        "update",
    }
    public = {
        name
        for name, _ in inspect.getmembers(
            InMemoryDashboardRepository, predicate=inspect.isfunction
        )
        if not name.startswith("_")
    }
    assert not public & forbidden


def test_dashboard_source_has_no_filesystem_scan_or_scientific_thresholding() -> None:
    forbidden = (
        "os.walk",
        "rglob(",
        "glob(",
        "/mnt/",
        "threshold",
        "false_alarm",
        "tle",
    )
    for path in DASHBOARD.rglob("*.py"):
        text = path.read_text().lower()
        assert not any(token in text for token in forbidden), path
