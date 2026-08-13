from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path("src/leo_flow/analysis/ephemeris")


def imports(path: Path) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_component_has_no_network_client_or_other_scientific_dependencies() -> None:
    forbidden = (
        "urllib.request",
        "http.client",
        "requests",
        "leo_flow.analysis.recording",
        "leo_flow.analysis.model",
        "leo_flow.capture",
        "leo_flow.dashboard",
    )
    for path in SOURCE.rglob("*.py"):
        modules = imports(path)
        assert not any(
            module == prefix or module.startswith(prefix + ".")
            for module in modules
            for prefix in forbidden
        ), path


def test_no_provider_secret_field_is_serialized_or_logged() -> None:
    provider_path = SOURCE / "providers.py"
    providers = provider_path.read_text()
    assert "logging" not in imports(provider_path)
    assert "credentials=" in providers  # capability passed separately to transport
    assert '("Authorization"' not in providers
