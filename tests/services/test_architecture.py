from __future__ import annotations

import ast
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[2] / "src" / "leo_flow" / "services"


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_capture_process_does_not_know_analysis_or_dashboard() -> None:
    modules = imports(SERVICES / "capture.py")
    assert not any("analysis" in module or "dashboard" in module for module in modules)


def test_analysis_process_does_not_import_capture_implementations() -> None:
    modules = imports(SERVICES / "analysis.py")
    assert not any(module.startswith("leo_flow.capture") for module in modules)


def test_dashboard_process_depends_only_on_public_dashboard_surface() -> None:
    modules = imports(SERVICES / "dashboard.py")
    forbidden = (
        "leo_flow.capture",
        "leo_flow.analysis",
        "leo_flow.storage",
        "leo_flow.jobs",
    )
    assert not any(module.startswith(forbidden) for module in modules)


def test_service_layer_has_no_network_database_legacy_or_shell_dependencies() -> None:
    forbidden = ("socket", "subprocess", "psycopg", "requests", "leo_tracker")
    for path in SERVICES.glob("*.py"):
        modules = imports(path)
        assert not any(
            module == prefix or module.startswith(prefix + ".")
            for module in modules
            for prefix in forbidden
        ), path
