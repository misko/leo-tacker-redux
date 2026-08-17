"""Production composition for the finite Gauss V5 campaign operator."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from leo_flow.capture.campaign import CampaignDefinition
from leo_flow.capture.v5_station import V5CaptureStation
from leo_flow.services.config import AnalysisServiceConfig, load_service_config

_RUNTIME_OPTION = "--runtime-config"
_ARMED_COMMANDS = {"run", "run-next"}
_EXPECTED_KEYS = {
    "schema",
    "analysis_config",
    "capture_credential_directory",
    "analysis_credential_directory",
    "dashboard_credential_directory",
    "cas_root",
    "radio_ips",
    "secondary_dispatch_delay_ms",
}


@dataclass(frozen=True, slots=True)
class GaussCampaignRuntimeConfig:
    analysis_config: Path
    capture_credential_directory: Path
    analysis_credential_directory: Path
    dashboard_credential_directory: Path
    cas_root: Path
    radio_ips: tuple[str, str]
    secondary_dispatch_delay_ms: int

    def __post_init__(self) -> None:
        paths = (
            self.analysis_config,
            self.capture_credential_directory,
            self.analysis_credential_directory,
            self.dashboard_credential_directory,
            self.cas_root,
        )
        if any(not item.is_absolute() or ".." in item.parts for item in paths):
            raise ValueError("campaign runtime paths must be absolute and normalized")
        if len(set(self.radio_ips)) != 2:
            raise ValueError("campaign runtime requires two distinct radio IPs")
        if self.secondary_dispatch_delay_ms != 10:
            raise ValueError("campaign runtime requires the reviewed 10 ms delay")


def load_gauss_campaign_runtime_config(path: Path) -> GaussCampaignRuntimeConfig:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("campaign runtime configuration is unavailable") from error
    if (
        not isinstance(value, dict)
        or set(value) != _EXPECTED_KEYS
        or value.get("schema") != "org.leo-flow.gauss-v5-campaign-runtime/v1"
    ):
        raise ValueError("campaign runtime configuration shape differs")
    radio_ips = value["radio_ips"]
    if (
        not isinstance(radio_ips, list)
        or len(radio_ips) != 2
        or not all(isinstance(item, str) and item for item in radio_ips)
    ):
        raise ValueError("campaign runtime radio IPs are invalid")
    path_fields = {
        key: Path(_nonempty_string(value[key], key))
        for key in _EXPECTED_KEYS
        if key
        not in {
            "schema",
            "radio_ips",
            "secondary_dispatch_delay_ms",
        }
    }
    return GaussCampaignRuntimeConfig(
        path_fields["analysis_config"],
        path_fields["capture_credential_directory"],
        path_fields["analysis_credential_directory"],
        path_fields["dashboard_credential_directory"],
        path_fields["cas_root"],
        (radio_ips[0], radio_ips[1]),
        _exact_int(value["secondary_dispatch_delay_ms"], "secondary_dispatch_delay_ms"),
    )


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
        from leo_flow.deployments.v5_campaign_operator import main as component_main

        return component_main(
            forwarded,
            stdout=stdout,
            stderr=stderr,
            program_name="leo-v5-campaign",
            show_deployment_runtime_option=True,
        )
    if runtime_path is None:
        _emit_error(stderr)
        return 2
    try:
        runtime = load_gauss_campaign_runtime_config(runtime_path)
        analysis = load_service_config(runtime.analysis_config)
        if not isinstance(analysis, AnalysisServiceConfig):
            raise TypeError("campaign analysis configuration has another process")
    except Exception:  # noqa: BLE001 - sanitized deployment boundary
        _emit_error(stderr)
        return 2

    # Live-only imports keep help, planning, validation, and status independent
    # of the optional PostgreSQL/server dependency.
    from leo_flow.deployments.gauss_campaign_runtime import (
        LinuxExternalRadioOwnershipGate,
        LocalCampaignCapacity,
        ProcessIsolatedCampaignCapture,
        build_gauss_campaign_analysis,
    )
    from leo_flow.deployments.v5_campaign_operator import main as component_main

    ownership = LinuxExternalRadioOwnershipGate(runtime.radio_ips)

    def capture_builder(
        definition: CampaignDefinition,
        station_a: V5CaptureStation,
        station_b: V5CaptureStation,
        campaign_state_root: Path,
    ) -> ProcessIsolatedCampaignCapture:
        _require_runtime_station_pair(runtime, station_a, station_b)
        if (
            station_a.state.cas_root != runtime.cas_root
            or station_b.state.cas_root != runtime.cas_root
        ):
            raise ValueError("campaign runtime CAS differs from station identity")
        return ProcessIsolatedCampaignCapture(
            definition,
            station_a,
            station_b,
            campaign_state_root,
            campaign_state_root / "capture-batches.sqlite3",
            runtime.capture_credential_directory,
            ownership,
            secondary_dispatch_delay_s=(runtime.secondary_dispatch_delay_ms / 1_000),
        )

    return component_main(
        forwarded,
        stdout=stdout,
        stderr=stderr,
        capture_builder=capture_builder,
        analysis_builder=lambda _definition: build_gauss_campaign_analysis(
            analysis,
            runtime.analysis_credential_directory,
            runtime.dashboard_credential_directory,
        ),
        capacity_builder=lambda state_root: LocalCampaignCapacity(
            runtime.cas_root, state_root
        ),
        program_name="leo-v5-campaign",
        show_deployment_runtime_option=True,
    )


def _require_runtime_station_pair(
    runtime: GaussCampaignRuntimeConfig,
    station_a: V5CaptureStation,
    station_b: V5CaptureStation,
) -> None:
    expected = tuple(f"ip:{address}" for address in runtime.radio_ips)
    observed = (station_a.radio.uri, station_b.radio.uri)
    if observed != expected:
        raise ValueError("campaign runtime radio endpoints differ from station pair")


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


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _exact_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return value


def _emit_error(stderr: TextIO) -> None:
    stderr.write('{"event":"campaign_runtime_configuration_error"}\n')
    stderr.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
