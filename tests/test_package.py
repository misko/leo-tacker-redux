import tomllib
from pathlib import Path

from leo_flow import __version__


def test_package_version_is_exposed() -> None:
    assert __version__ == "0.1.0"


def test_operator_console_scripts_are_part_of_the_release_surface() -> None:
    document = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"]["scripts"] == {
        "leo-dashboard": "leo_flow.deployments.dashboard_operator:main",
        "leo-gauss-analysis": "leo_station.analysis_operator:main",
        "leo-v5-capture": "leo_flow.deployments.v5_capture_operator:main",
        "leo-v5-campaign": "leo_flow.deployments.gauss_v5_campaign_operator:main",
        "leo-v5-continuous": "leo_flow.deployments.gauss_v5_continuous_operator:main",
        "leo-v5-dual-capture": "leo_flow.deployments.v5_dual_capture_operator:main",
        "leo-verify-release": "leo_flow.deployments.release_verifier:main",
        "leo-gauss-focused-capture": "leo_flow.deployments.gauss_focused_capture_operator:main",
        "leo-gauss-focused-analysis": "leo_flow.deployments.gauss_focused_analysis_operator:main",
        "leo-v5-supercycle-canary": "leo_flow.deployments.gauss_v5_supercycle_canary_operator:main",
    }
