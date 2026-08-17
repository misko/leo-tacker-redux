"""Exact scan-only V5 station deployment for the radio at 192.168.1.15."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import cast

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.capture.drivers.v5_preflight import ExpectedV5Radio, ExpectedV5Runtime
from leo_flow.capture.engine import CaptureIdentity, PlanCaptureEngine
from leo_flow.capture.scan_plan import (
    StarlinkEdgeScanSpec,
    build_starlink_edge_scan_plan,
)
from leo_flow.capture.v5_station import (
    V5CaptureState,
    V5CaptureStation,
    V5RadioDefinition,
    V5ScanDefinition,
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

GAUSS_RUNTIME_MANIFEST = Path(
    "/home/mouse9911/gits/leo-tracker-redux/deploy/v5-runtime/"
    "gauss-development.manifest.json"
)
GAUSS_RUNTIME_MANIFEST_DIGEST = Digest(
    DigestAlgorithm.SHA256,
    "1544c390d66a2a53c9b86dc0cf7a2fab63e9fca0a08563638744121b107f431f",
)
GAUSS_EXPECTED_RUNTIME = ExpectedV5Runtime(
    runtime_id="gauss-pluto-v5-libiio-0.25-spfmeta3",
    schema="leo-flow.v5-runtime/v1",
    iio_module_path=(
        "/home/mouse9911/.cache/leo-flow/v5-runtime/lib/python3.11/site-packages/iio.py"
    ),
    iio_version=(0, 25, "c26258b"),
    iio_commit="c26258bfa33098c2b215e19cf85d448e89499b1a",
    native_libiio_prefix="/home/mouse9911/.cache/leo-flow/v5-runtime",
    required_backends=frozenset(("local", "ip", "usb")),
    pyadi_version="0.0.21",
    pyadi_module_path=(
        "/home/mouse9911/gits/leo-tracker-redux/"
        ".venv/lib/python3.11/site-packages/adi/__init__.py"
    ),
    spf_module_path=(
        "/home/mouse9911/.cache/leo-flow/v5-build/spf/spf/direct_radio/iio_metadata.py"
    ),
    spf_revision="c40ee4116546889effd72056115adaaa1bc3fd40",
    spf_import="spf.direct_radio.iio_metadata:IioMetadataRx",
    metadata_protocol="spf-radio-metadata-v3",
)

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

DEVELOPMENT_STATION = V5CaptureStation(
    station_id=CAPTURE_IDENTITY.station_id,
    radio=V5RadioDefinition(
        uri="ip:192.168.1.15",
        expected_serial=CAPTURE_IDENTITY.radio_serial,
        radio_id=RADIO_ID,
        receiver_chain_ids=RECEIVER_CHAINS,
        firmware_release="v0.38-plutoplus-spf-libiio-metadata-v5",
        firmware_commit="d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8",
        io_timeout_ms=5_000,
    ),
    hardware_snapshot_id=CAPTURE_IDENTITY.hardware_metadata_snapshot_id,
    clock_status=CAPTURE_IDENTITY.clock_status,
    capture_implementation=CAPTURE_IDENTITY.producer,
    runtime_manifest=GAUSS_RUNTIME_MANIFEST,
    runtime_manifest_digest=GAUSS_RUNTIME_MANIFEST_DIGEST,
    expected_runtime=GAUSS_EXPECTED_RUNTIME,
    plan=V5ScanDefinition(
        plan_id=PLAN_ID,
        plan_digest=SCAN_PLAN_DIGEST,
        sample_rate_hz=2_083_332.0,
        bandwidth_hz=2_000_000.0,
        sample_count=262_144,
        edge_order="L",
        edge_order_draw_u32=0,
        arm_name="v5-qualified-1x262144",
        lnb_lo_hz=9_750_000_000.0,
        hardware_block_samples=262_144,
    ),
    state=V5CaptureState(
        state_root=Path("/home/mouse9911/.local/state/leo-flow/v5-scan/radio-15"),
        recording_root=Path(
            "/home/mouse9911/.local/state/leo-flow/v5-scan/radio-15/recordings"
        ),
        spool_database=Path(
            "/home/mouse9911/.local/state/leo-flow/v5-scan/"
            "radio-15/capture-spool.sqlite3"
        ),
        cas_root=Path("/home/mouse9911/.local/share/leo-flow/objects"),
        lock_path=Path(
            "/home/mouse9911/.local/state/leo-flow/v5-scan/radio-15/instance.lock"
        ),
        mode_lock_path=Path("/home/mouse9911/.local/state/leo-flow/pipeline-mode.lock"),
        minimum_free_bytes=MINIMUM_FREE_BYTES,
        require_cas_mount=False,
    ),
)


class ExactV5ScanPlanSource:
    def get(self, plan_id: PlanId) -> CapturePlan:
        if plan_id != PLAN_ID or canonical_digest(SCAN_PLAN) != SCAN_PLAN_DIGEST:
            raise KeyError("exact V5 scan plan is unavailable")
        return SCAN_PLAN


class ExactStationScanPlanSource:
    """Plan source bound to one already-validated station specification."""

    def __init__(self, plan: CapturePlan, digest: Digest) -> None:
        if canonical_digest(plan) != digest:
            raise ValueError("station plan differs from its immutable digest")
        self._plan = plan
        self._digest = digest

    def get(self, plan_id: PlanId) -> CapturePlan:
        if (
            plan_id != self._plan.plan_id
            or canonical_digest(self._plan) != self._digest
        ):
            raise KeyError("exact station scan plan is unavailable")
        return self._plan


def build_station_capture_cycle(
    station: V5CaptureStation, catalog_dsn: str
) -> OneShotV5PlanCycle:
    """Compose one station-bound cycle without opening resources.

    The returned cycle performs filesystem, PostgreSQL, and radio I/O only when
    its ``preflight`` method is invoked.
    """

    if not catalog_dsn:
        raise ValueError("catalog credential cannot be empty")
    if not station.radio.require_both_tx_muted:
        raise ValueError("station does not require both TX outputs muted")
    plan = station.capture_plan()
    expected_radio = ExpectedV5Radio(
        serial=station.radio.expected_serial,
        firmware_release=station.radio.firmware_release,
        firmware_commit=station.radio.firmware_commit,
        maximum_tx2_hardware_gain_db=-80.0,
        require_both_tx_muted=station.radio.require_both_tx_muted,
    )
    radio = V5RadioProvider(
        station.radio_config(),
        expected_radio=expected_radio,
        expected_runtime=station.expected_runtime,
        runtime_manifest=station.runtime_manifest,
        runtime_manifest_digest=station.runtime_manifest_digest,
    )
    state = station.state
    return OneShotV5PlanCycle(
        ExactStationScanPlanSource(plan, station.plan.plan_digest),
        radio,
        CaptureHostGuard(
            state.lock_path,
            (state.state_root, state.recording_root, state.cas_root),
            state.minimum_free_bytes,
            required_mounts=(state.cas_root,) if state.require_cas_mount else (),
        ),
        SigMFRecordingWriter(),
        V5SpoolSpec(state.spool_database, state.recording_root),
        V5PostgresPublicationProvider(catalog_dsn, state.cas_root),
        plan_id=station.plan.plan_id,
        exact_plan=plan,
        exact_plan_digest=station.plan.plan_digest,
        deployment_name=f"V5 scan {station.radio.radio_id}",
        engine=PlanCaptureEngine(station.capture_identity()),
    )


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
