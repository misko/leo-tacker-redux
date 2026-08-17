"""Gauss production composition for deferred continuous dual collection."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from leo_flow.capture.campaign import CampaignDefinition
from leo_flow.capture.v5_station import V5CaptureStation

_RUNTIME_OPTION = "--runtime-config"
_ARMED_COMMANDS = {
    "capture-next",
    "close",
    "analyze-next",
    "capture-run",
    "drain-analysis",
    "drain-analysis-staged",
    "drain-analysis-online",
}


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        runtime_path, forwarded = _extract_runtime_option(arguments)
    except ValueError:
        _emit_error(stderr)
        return 2
    command = next((item for item in forwarded if not item.startswith("-")), None)
    if command not in _ARMED_COMMANDS:
        from leo_flow.deployments.v5_continuous_operator import main as component_main

        return component_main(
            forwarded,
            stdout=stdout,
            stderr=stderr,
            program_name="leo-v5-continuous",
            show_deployment_runtime_option=True,
        )
    if runtime_path is None:
        _emit_error(stderr)
        return 2
    try:
        from leo_flow.adapters.campaign_online_analysis_postgres import (
            PostgresRegisteredAnalysisSafetyGateV2,
        )
        from leo_flow.adapters.capture_analysis_drain_postgres import (
            PostgresCaptureAnalysisDrainGate,
        )
        from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
        from leo_flow.deployments.gauss_campaign_runtime import (
            LinuxExternalRadioOwnershipGate,
            LocalCampaignCapacity,
            ProcessIsolatedCampaignCapture,
            build_gauss_campaign_analysis,
        )
        from leo_flow.deployments.gauss_staged_analysis_runtime import (
            build_gauss_online_campaign_analysis,
            build_gauss_staged_campaign_analysis,
        )
        from leo_flow.deployments.gauss_v5_campaign_operator import (
            _require_runtime_station_pair,
            load_gauss_campaign_runtime_config,
        )
        from leo_flow.services.config import AnalysisServiceConfig, load_service_config

        runtime = load_gauss_campaign_runtime_config(runtime_path)
        analysis = load_service_config(runtime.analysis_config)
        if not isinstance(analysis, AnalysisServiceConfig):
            raise TypeError("continuous analysis configuration has another process")
        capture_credential = SystemdCredentialProvider(
            runtime.capture_credential_directory
        ).resolve("catalog-dsn")
    except Exception:  # noqa: BLE001 - sanitized deployment boundary
        _emit_error(stderr)
        return 2

    ownership = LinuxExternalRadioOwnershipGate(runtime.radio_ips)

    def capture_builder(
        definition: CampaignDefinition,
        station_a: V5CaptureStation,
        station_b: V5CaptureStation,
        state_root: Path,
    ) -> ProcessIsolatedCampaignCapture:
        _require_runtime_station_pair(runtime, station_a, station_b)
        if (
            station_a.state.cas_root != runtime.cas_root
            or station_b.state.cas_root != runtime.cas_root
        ):
            raise ValueError("continuous runtime CAS differs from station identity")
        return ProcessIsolatedCampaignCapture(
            definition,
            station_a,
            station_b,
            state_root,
            state_root / "capture-batches.sqlite3",
            runtime.capture_credential_directory,
            ownership,
            admission_builder=lambda dsn: PostgresRegisteredAnalysisSafetyGateV2(
                dsn, definition.digest
            ),
            secondary_dispatch_delay_s=runtime.secondary_dispatch_delay_ms / 1_000,
        )

    from leo_flow.deployments.v5_continuous_operator import main as component_main

    return component_main(
        forwarded,
        stdout=stdout,
        stderr=stderr,
        capture_builder=capture_builder,
        analysis_builder=lambda _definition: build_gauss_campaign_analysis(
            analysis,
            runtime.analysis_credential_directory,
            runtime.dashboard_credential_directory,
            lock_analysis=command != "drain-analysis-staged",
        ),
        staged_analysis_builder=lambda definition, coordinator, compute, projection: (
            build_gauss_staged_campaign_analysis(
                definition,
                coordinator,
                compute,
                projection,
                runtime.analysis_credential_directory,
            )
        ),
        online_analysis_builder=lambda definition, compute, projection: (
            build_gauss_online_campaign_analysis(
                definition,
                compute,
                projection,
                runtime.analysis_credential_directory,
            )
        ),
        capacity_builder=lambda state_root: LocalCampaignCapacity(
            runtime.cas_root, state_root
        ),
        start_admission=PostgresCaptureAnalysisDrainGate(capture_credential),
        program_name="leo-v5-continuous",
        show_deployment_runtime_option=True,
    )


def _extract_runtime_option(arguments: list[str]) -> tuple[Path | None, list[str]]:
    runtime: Path | None = None
    forwarded: list[str] = []
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item == _RUNTIME_OPTION:
            if runtime is not None or index + 1 >= len(arguments):
                raise ValueError("runtime option is missing or repeated")
            runtime = Path(arguments[index + 1])
            index += 2
            continue
        if item.startswith(f"{_RUNTIME_OPTION}="):
            if runtime is not None:
                raise ValueError("runtime option is repeated")
            value = item.partition("=")[2]
            if not value:
                raise ValueError("runtime option is empty")
            runtime = Path(value)
            index += 1
            continue
        forwarded.append(item)
        index += 1
    return runtime, forwarded


def _emit_error(stderr: TextIO) -> None:
    stderr.write('{"event":"continuous_runtime_configuration_error"}\n')
    stderr.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
