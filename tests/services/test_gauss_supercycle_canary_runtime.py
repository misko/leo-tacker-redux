from __future__ import annotations

from io import StringIO

from leo_flow.deployments.gauss_v5_supercycle_canary_operator import main


def test_armed_canary_requires_one_sealed_runtime_config() -> None:
    errors = StringIO()

    assert main(["capture-run"], stderr=errors) == 2
    assert errors.getvalue().strip() == '{"event":"canary_composition_failed"}'


def test_canary_rejects_repeated_runtime_config_before_composition() -> None:
    errors = StringIO()

    assert (
        main(
            [
                "--runtime-config",
                "/release/config/runtime.json",
                "--runtime-config=/substituted/runtime.json",
                "capture-run",
            ],
            stderr=errors,
        )
        == 2
    )
    assert errors.getvalue().strip() == '{"event":"canary_composition_failed"}'
