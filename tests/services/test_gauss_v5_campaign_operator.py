from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from leo_flow.deployments.gauss_v5_campaign_operator import (
    load_gauss_campaign_runtime_config,
    main,
)


def test_checked_gauss_campaign_runtime_is_exact() -> None:
    config = load_gauss_campaign_runtime_config(
        Path("deploy/gauss-campaign-v1/runtime.json")
    )

    assert config.analysis_config == Path(
        "/home/mouse9911/gits/leo-tracker-redux/deploy/gauss-analysis-v1/analysis.json"
    )
    assert config.cas_root == Path("/home/mouse9911/.local/share/leo-flow/objects")
    assert config.radio_ips == ("192.168.1.20", "192.168.1.21")
    assert config.secondary_dispatch_delay_ms == 10


def test_runtime_config_rejects_unknown_keys(tmp_path: Path) -> None:
    source = json.loads(
        Path("deploy/gauss-campaign-v1/runtime.json").read_text(encoding="utf-8")
    )
    source["unexpected"] = True
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ValueError, match="shape differs"):
        load_gauss_campaign_runtime_config(path)


def test_armed_command_requires_runtime_config_before_component_ports() -> None:
    stderr = StringIO()

    assert main(["run"], stderr=stderr) == 2
    assert stderr.getvalue() == '{"event":"campaign_runtime_configuration_error"}\n'


def test_offline_help_imports_without_optional_postgres_dependency() -> None:
    script = """
import sys
sys.modules['psycopg'] = None
from leo_flow.deployments.gauss_v5_campaign_operator import main
try:
    main(['--help'])
except SystemExit as error:
    raise SystemExit(error.code)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.startswith("usage: leo-v5-campaign")
    assert "--runtime-config PATH" in completed.stdout
    assert "plan-qualification" in completed.stdout
    assert "run-next" in completed.stdout
    assert completed.stderr == ""
