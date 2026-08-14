"""Exact scan-only V5 station deployment for the radio at 192.168.1.15."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import cast

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.capture.engine import CaptureIdentity, PlanCaptureEngine
from leo_flow.capture.scan_plan import (
    StarlinkEdgeScanSpec,
    build_starlink_edge_scan_plan,
)
from leo_flow.contracts.capture import (
    CapturePlan,
    GainMode,
    GainSetting,
)
from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    StationId,
    canonical_digest,
)
from leo_flow.contracts.ports import CapturePlanSource
from leo_flow.deployments.v5_canary import (
    DATABASE_SECRET,
    SECRET_PROVIDER,
    CaptureHostGuard,
    OneShotV5PlanCycle,
    V5PostgresPublicationProvider,
    V5RadioProvider,
    V5SpoolSpec,
)
from leo_flow.services.bootstrap import (
    AdapterBuildContext,
    AdapterManifest,
    AdapterSet,
    Capability,
    DeploymentPlugin,
    Process,
)
from leo_flow.services.capture import build_capture_service
from leo_flow.services.config import CaptureServiceConfig, ServiceConfig
from leo_flow.services.lifecycle import DiagnosticSink, ServiceLoop
from leo_flow.storage.recording_codec import SigMFRecordingWriter

PLAN_SOURCE_REF = "plans.v5-edge-scan-2026-08-14-v1"
RADIO_REF = "radio.pluto-v5-192-168-1-15-v1"
PREFLIGHT_REF = "capture.v5-scan-host-guard-v1"
RECORDING_WRITER_REF = "recording.sigmf-pair-v1"
SPOOL_REF = "spool.v5-scan-sqlite-v1"
RECORDING_PUBLISHER_REF = "recording.fs-cas-postgres-projection-v1"

STATE_ROOT = Path("/var/lib/leo-flow-v5-scan")
RECORDING_ROOT = STATE_ROOT / "recordings"
SPOOL_DATABASE = STATE_ROOT / "capture-spool.sqlite3"
# This mount is the sole bulk-data handoff. PostgreSQL carries immutable refs;
# authorized downstream consumers bind the same content-addressed namespace.
CAS_ROOT = Path("/var/lib/leo-flow/objects")
LOCK_PATH = Path("/run/leo-flow-v5-scan/instance.lock")
MINIMUM_FREE_BYTES = 1 << 30

RADIO_ID = RadioId("radio_pluto_v5_canary_15")
RECEIVER_CHAINS = (ReceiverChainId("rx_v5_1"), ReceiverChainId("rx_v5_2"))
PLAN_ID = PlanId("plan_v5_scan_20260814_v1")

SCAN_PLAN = build_starlink_edge_scan_plan(
    StarlinkEdgeScanSpec(
        plan_id=PLAN_ID,
        radio_id=RADIO_ID,
        receiver_chain_ids=RECEIVER_CHAINS,
        gain=GainSetting(GainMode.AGC),
        sample_rate_hz=2_083_332.0,
        bandwidth_hz=2_000_000.0,
        sample_count=262_144,
        edge_order="L",
        edge_order_draw_u32=0,
        arm_name="v5-qualified-1x262144",
        hardware_block_samples=262_144,
    )
)
SCAN_PLAN_DIGEST = Digest(
    DigestAlgorithm.SHA256,
    "bf6947c46dbe06eaf9efcd2039785a1f432015610080c6e32965f1a58a560ab6",
)
if canonical_digest(SCAN_PLAN) != SCAN_PLAN_DIGEST:
    raise RuntimeError("embedded V5 scan plan differs from its immutable digest")

CAPTURE_IDENTITY = CaptureIdentity(
    StationId("station_leo_primary"),
    "104000b29905000e17000800065934759d",
    "system-realtime-v5-metadata",
    HardwareSnapshotId("hw_v5_canary_20260814_v2"),
    "leo-flow-v5-edge-scan-v1",
)


class ExactV5ScanPlanSource:
    def get(self, plan_id: PlanId) -> CapturePlan:
        if plan_id != PLAN_ID or canonical_digest(SCAN_PLAN) != SCAN_PLAN_DIGEST:
            raise KeyError("exact V5 scan plan is unavailable")
        return SCAN_PLAN


def _plan_source(context: AdapterBuildContext) -> ExactV5ScanPlanSource:
    del context
    return ExactV5ScanPlanSource()


def _radio(context: AdapterBuildContext) -> V5RadioProvider:
    del context
    return V5RadioProvider()


def _preflight(context: AdapterBuildContext) -> CaptureHostGuard:
    del context
    return CaptureHostGuard(
        LOCK_PATH,
        (RECORDING_ROOT, CAS_ROOT),
        MINIMUM_FREE_BYTES,
        required_mounts=(CAS_ROOT,),
    )


def _writer(context: AdapterBuildContext) -> SigMFRecordingWriter:
    del context
    return SigMFRecordingWriter()


def _spool(context: AdapterBuildContext) -> V5SpoolSpec:
    del context
    return V5SpoolSpec(SPOOL_DATABASE, RECORDING_ROOT)


def _publisher(context: AdapterBuildContext) -> V5PostgresPublicationProvider:
    try:
        dsn = context.secrets[DATABASE_SECRET]
    except KeyError as error:
        raise ValueError("catalog database credential was not configured") from error
    return V5PostgresPublicationProvider(dsn, CAS_ROOT)


def _build_capture(
    config: ServiceConfig,
    adapters: AdapterSet,
    diagnostics: DiagnosticSink,
) -> ServiceLoop:
    if not isinstance(config, CaptureServiceConfig):
        raise TypeError("V5 scan requires capture configuration")
    cycle = OneShotV5PlanCycle(
        cast(CapturePlanSource, adapters[Capability.PLAN_SOURCE]),
        cast(V5RadioProvider, adapters[Capability.RADIO]),
        cast(CaptureHostGuard, adapters[Capability.CAPTURE_PREFLIGHT]),
        cast(SigMFRecordingWriter, adapters[Capability.RECORDING_WRITER]),
        cast(V5SpoolSpec, adapters[Capability.SPOOL]),
        cast(V5PostgresPublicationProvider, adapters[Capability.RECORDING_PUBLISHER]),
        plan_id=PLAN_ID,
        exact_plan=SCAN_PLAN,
        exact_plan_digest=SCAN_PLAN_DIGEST,
        deployment_name="V5 edge scan",
        engine=PlanCaptureEngine(CAPTURE_IDENTITY),
    )
    return build_capture_service(config, cycle, diagnostics=diagnostics)


MANIFEST = AdapterManifest(
    {
        Process.CAPTURE: {
            Capability.PLAN_SOURCE: {PLAN_SOURCE_REF: _plan_source},
            Capability.RADIO: {RADIO_REF: _radio},
            Capability.CAPTURE_PREFLIGHT: {PREFLIGHT_REF: _preflight},
            Capability.RECORDING_WRITER: {RECORDING_WRITER_REF: _writer},
            Capability.SPOOL: {SPOOL_REF: _spool},
            Capability.RECORDING_PUBLISHER: {RECORDING_PUBLISHER_REF: _publisher},
        }
    }
)

PLUGIN = DeploymentPlugin(
    MANIFEST,
    MappingProxyType({SECRET_PROVIDER: SystemdCredentialProvider()}),
    MappingProxyType({Process.CAPTURE: _build_capture}),
)
