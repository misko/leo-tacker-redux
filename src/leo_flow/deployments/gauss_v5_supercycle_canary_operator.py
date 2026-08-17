"""Sealed Gauss composition for the isolated 36-slot supercycle canary."""

from __future__ import annotations

import resource
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from leo_flow.capture.campaign import (
    CampaignAnalysisPort,
    CampaignCapacityPort,
    CampaignCapturePort,
    CampaignUnit,
)
from leo_flow.capture.supercycle_canary import (
    CANARY_SLOTS,
    SupercycleCanaryCoordinator,
    SupercycleCanaryDefinition,
    materialize_canary_station,
)
from leo_flow.capture.v5_station import V5CaptureStation
from leo_flow.contracts.capture_batch import CaptureBatchSnapshot
from leo_flow.contracts.deferred_analysis import (
    DeferredAnalysisCampaignDefinitionV1,
    DeferredAnalysisWindowV1,
)
from leo_flow.deployments.supercycle_canary_analysis import (
    SupercycleCanaryStagedAnalysis,
)

_RUNTIME_OPTION = "--runtime-config"
_ARMED_COMMANDS = {"capture-run", "drain-analysis"}


class _CanaryWindowPreparer:
    def __init__(self, credential_directory: Path) -> None:
        from leo_flow.deployments.gauss_staged_analysis_runtime import (
            GaussDeferredAnalysisWindowPreparerV1,
        )

        self._delegate = GaussDeferredAnalysisWindowPreparerV1(credential_directory)

    def prepare(
        self,
        definition: SupercycleCanaryDefinition,
        first_success_index: int,
        snapshots: tuple[CaptureBatchSnapshot, ...],
    ) -> DeferredAnalysisWindowV1:
        return self._delegate.prepare(
            DeferredAnalysisCampaignDefinitionV1(
                definition.digest,
                False,
                False,
                CANARY_SLOTS,
            ),
            first_success_index,
            snapshots,
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
        return _error(stderr)
    command = next((item for item in forwarded if not item.startswith("-")), None)
    if command not in _ARMED_COMMANDS:
        from leo_flow.deployments.v5_supercycle_canary_operator import (
            main as component_main,
        )

        return component_main(forwarded, stdout=stdout, stderr=stderr)
    if runtime_path is None:
        return _error(stderr)
    try:
        from leo_flow.adapters.campaign_scoped_claims_postgres import (
            PostgresCampaignAnalysisLaneStateReaderV1,
        )
        from leo_flow.adapters.supercycle_canary_closure_postgres import (
            PostgresSupercycleCanaryClosureReaderV1,
        )
        from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
        from leo_flow.deployments.gauss_campaign_runtime import (
            LinuxExternalRadioOwnershipGate,
            LocalCampaignCapacity,
            ProcessIsolatedCampaignCapture,
            _dashboard_connection_factory,
            build_gauss_campaign_analysis,
        )
        from leo_flow.deployments.gauss_staged_analysis_runtime import (
            GaussCampaignScopedAnalysisWorkerV1,
        )
        from leo_flow.deployments.gauss_v5_campaign_operator import (
            _require_runtime_station_pair,
            load_gauss_campaign_runtime_config,
        )
        from leo_flow.deployments.process_mode_lock import ExclusiveModeLock
        from leo_flow.deployments.recording_submission_v1 import (
            analysis_connection_factory,
        )
        from leo_flow.deployments.staged_analysis_pool import (
            BoundedSpawnDeferredAnalysisLaneV1,
        )
        from leo_flow.deployments.v5_supercycle_canary_operator import (
            main as component_main,
        )
        from leo_flow.services.config import AnalysisServiceConfig, load_service_config
        from leo_station.analysis_v1 import MODE_LOCK_PATH

        runtime = load_gauss_campaign_runtime_config(runtime_path)
        analysis = load_service_config(runtime.analysis_config)
        if not isinstance(analysis, AnalysisServiceConfig):
            raise TypeError("canary analysis configuration has another process")
        analysis_credentials = SystemdCredentialProvider(
            runtime.analysis_credential_directory
        )
        analysis_connect = analysis_connection_factory(
            analysis_credentials.resolve("catalog-dsn")
        )
        dashboard_connect = _dashboard_connection_factory(
            SystemdCredentialProvider(runtime.dashboard_credential_directory).resolve(
                "catalog-dsn"
            )
        )
        ownership = LinuxExternalRadioOwnershipGate(runtime.radio_ips)

        def capture_builder(
            definition: SupercycleCanaryDefinition,
            station_a: V5CaptureStation,
            station_b: V5CaptureStation,
            state_root: Path,
        ) -> tuple[CampaignCapturePort, CampaignCapacityPort]:
            _require_runtime_station_pair(runtime, station_a, station_b)
            if (
                station_a.state.cas_root != runtime.cas_root
                or station_b.state.cas_root != runtime.cas_root
            ):
                raise ValueError("canary runtime CAS differs from station identity")

            def station_materializer(unit: CampaignUnit, side: str) -> V5CaptureStation:
                return materialize_canary_station(
                    definition,
                    station_a if side == "a" else station_b,
                    unit,
                    side=side,
                    canary_state_root=state_root,
                )

            return (
                ProcessIsolatedCampaignCapture(
                    definition,
                    station_a,
                    station_b,
                    state_root,
                    state_root / definition.canary_id / "capture-batches.sqlite3",
                    runtime.capture_credential_directory,
                    ownership,
                    station_materializer=station_materializer,
                    secondary_dispatch_delay_s=(
                        runtime.secondary_dispatch_delay_ms / 1_000
                    ),
                ),
                LocalCampaignCapacity(runtime.cas_root, state_root),
            )

        def analysis_builder(
            definition: SupercycleCanaryDefinition, state_root: Path
        ) -> tuple[CampaignAnalysisPort, CampaignCapacityPort]:
            del definition
            return (
                build_gauss_campaign_analysis(
                    analysis,
                    runtime.analysis_credential_directory,
                    runtime.dashboard_credential_directory,
                    lock_analysis=False,
                ),
                LocalCampaignCapacity(runtime.cas_root, state_root),
            )

        def staged_builder(
            definition: SupercycleCanaryDefinition,
            coordinator: SupercycleCanaryCoordinator,
        ) -> SupercycleCanaryStagedAnalysis:
            lane = BoundedSpawnDeferredAnalysisLaneV1(
                GaussCampaignScopedAnalysisWorkerV1(
                    runtime.analysis_credential_directory
                ),
                PostgresCampaignAnalysisLaneStateReaderV1(analysis_connect),
            )
            return SupercycleCanaryStagedAnalysis(
                definition,
                coordinator,
                _CanaryWindowPreparer(runtime.analysis_credential_directory),
                lane,
                monotonic_ns=time.monotonic_ns,
                process_time_ns=time.process_time_ns,
                peak_rss_bytes=lambda: (
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
                ),
            )

        lock = (
            ExclusiveModeLock(MODE_LOCK_PATH) if command == "drain-analysis" else None
        )
        if lock is not None:
            lock.acquire()
        try:
            return component_main(
                forwarded,
                stdout=stdout,
                stderr=stderr,
                capture_runtime_builder=capture_builder,
                analysis_runtime_builder=analysis_builder,
                staged_builder=staged_builder,
                closure_reader=PostgresSupercycleCanaryClosureReaderV1(
                    analysis_connect, dashboard_connect
                ),
            )
        finally:
            if lock is not None:
                lock.release()
    except Exception:  # noqa: BLE001 - sanitized deployment boundary
        return _error(stderr)


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
            runtime = Path(item.partition("=")[2])
            index += 1
            continue
        forwarded.append(item)
        index += 1
    return runtime, forwarded


def _error(stderr: TextIO) -> int:
    print('{"event":"canary_composition_failed"}', file=stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
