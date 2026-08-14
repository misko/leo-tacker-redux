"""One-shot station V5 canary deployment with restart-safe publication.

Importing this module constructs immutable values and adapter registries only.
Filesystem, PostgreSQL, and radio I/O begin in the capture cycle preflight.
"""

from __future__ import annotations

import fcntl
import importlib
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.application.projection_writers import (
    CaptureProjectionWriter,
    RecordingProjectionCommand,
)
from leo_flow.capture.drivers.pluto import (
    PlutoDevice,
    PlutoPairedRadio,
    PlutoRadioConfig,
)
from leo_flow.capture.drivers.spf_v3 import (
    SpfV3MetadataReader,
    spf_iio_session_factory,
)
from leo_flow.capture.drivers.v5_observers import (
    observe_current_v5_runtime,
    observe_v5_radio,
)
from leo_flow.capture.drivers.v5_preflight import (
    ExpectedV5Radio,
    ExpectedV5Runtime,
    create_attested_v5_radio,
)
from leo_flow.capture.engine import CaptureIdentity, PlanCaptureEngine
from leo_flow.capture.publication import PublicationReconciler
from leo_flow.capture.spool import SQLiteLocalSpool
from leo_flow.contracts.capture import (
    ActivityKind,
    ActivityRequest,
    CapturePlan,
    CompletedLocalRecording,
    GainMode,
    GainSetting,
    SegmentRequest,
)
from leo_flow.contracts.continuity import ContinuityPolicy
from leo_flow.contracts.core import (
    ActivityId,
    Digest,
    DigestAlgorithm,
    HardwareSnapshotId,
    PlanId,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    SegmentId,
    StationId,
    canonical_digest,
)
from leo_flow.contracts.ports import CapturePlanSource, RadioDevice, RecordingPublisher
from leo_flow.contracts.storage import PublishedRecordingRef
from leo_flow.services.bootstrap import (
    AdapterBuildContext,
    AdapterManifest,
    AdapterSet,
    Capability,
    DeploymentPlugin,
    Process,
)
from leo_flow.services.capture import build_capture_service
from leo_flow.services.config import CaptureServiceConfig, SecretRef, ServiceConfig
from leo_flow.services.lifecycle import DiagnosticSink, ServiceLoop
from leo_flow.storage.local_recording import (
    LocalRecordingNotFinalizedError,
    RootedSigMFRecordingStore,
)
from leo_flow.storage.recording_codec import SigMFRecordingWriter

PLAN_SOURCE_REF = "plans.v5-canary-2026-08-13-v1"
RADIO_REF = "radio.pluto-v5-192-168-1-15-v1"
PREFLIGHT_REF = "capture.host-guard-v1"
RECORDING_WRITER_REF = "recording.sigmf-pair-v1"
SPOOL_REF = "spool.sqlite-v1"
RECORDING_PUBLISHER_REF = "recording.fs-cas-postgres-projection-v1"
SECRET_PROVIDER = "systemd-credential"
DATABASE_SECRET = SecretRef(SECRET_PROVIDER, "catalog-dsn")

STATE_ROOT = Path("/var/lib/leo-flow-v5-canary")
RECORDING_ROOT = STATE_ROOT / "recordings"
SPOOL_DATABASE = STATE_ROOT / "capture-spool.sqlite3"
CAS_ROOT = STATE_ROOT / "cas"
LOCK_PATH = Path("/run/leo-flow-v5-canary/instance.lock")
RUNTIME_MANIFEST = Path("/opt/leo-v5/runtime-manifest.json")
MINIMUM_FREE_BYTES = 1 << 30
POSTGRES_TIMEOUT_S = 5

RADIO_ID = RadioId("radio_pluto_v5_canary_15")
RECEIVER_CHAINS = (ReceiverChainId("rx_v5_1"), ReceiverChainId("rx_v5_2"))
PLAN_ID = PlanId("plan_v5_canary_20260813_v1")

CANARY_PLAN = CapturePlan(
    schema=SchemaRef(CapturePlan.SCHEMA_ID),
    plan_id=PLAN_ID,
    radio_id=RADIO_ID,
    receiver_chain_ids=RECEIVER_CHAINS,
    activities=(
        ActivityRequest(
            ActivityId("act_v5_canary_20260813_v1"),
            ActivityKind.TEST,
            (
                SegmentRequest.create(
                    segment_id=SegmentId("seg_v5_canary_20260813_v1"),
                    center_frequency_hz=1_825_117_187.5,
                    sample_rate_hz=2_083_332.0,
                    bandwidth_hz=2_000_000.0,
                    receiver_chain_ids=RECEIVER_CHAINS,
                    gain=GainSetting(GainMode.AGC),
                    sample_count=262_144,
                    tags={"purpose": "passive-v5-rx-canary", "tx": "prohibited"},
                ),
            ),
        ),
    ),
    experiment_tags=(
        ("fixture", "rx1-rx2-to-tx2-sma-tee-no-lnb"),
        ("purpose", "v5-runtime-capture-canary"),
    ),
)
CANARY_PLAN_DIGEST = Digest(
    DigestAlgorithm.SHA256,
    "302ae9dd1daecd1963dee663eddd822b4007e878b2fe4ea78640976d471bdd91",
)
if canonical_digest(CANARY_PLAN) != CANARY_PLAN_DIGEST:
    raise RuntimeError("embedded V5 canary plan differs from its immutable digest")

EXPECTED_RUNTIME = ExpectedV5Runtime(
    runtime_id="pluto-v5-libiio-0.25-spfmeta3",
    schema="leo-flow.v5-runtime/v1",
    iio_module_path="/usr/local/lib/python3.11/dist-packages/iio.py",
    iio_version=(0, 25, "c26258b"),
    iio_commit="c26258bfa33098c2b215e19cf85d448e89499b1a",
    native_libiio_prefix="/opt/leo-v5",
    required_backends=frozenset(("local", "ip", "usb")),
    pyadi_version="0.0.21",
    pyadi_module_path="/usr/local/lib/python3.11/dist-packages/adi/__init__.py",
    spf_module_path=(
        "/usr/local/lib/python3.11/dist-packages/spf/direct_radio/iio_metadata.py"
    ),
    spf_revision="c40ee4116546889effd72056115adaaa1bc3fd40",
    spf_import="spf.direct_radio.iio_metadata:IioMetadataRx",
    metadata_protocol="spf-radio-metadata-v3",
)
EXPECTED_RADIO = ExpectedV5Radio(
    serial="104000b29905000e17000800065934759d",
    firmware_release="v0.38-plutoplus-spf-libiio-metadata-v5",
    firmware_commit="d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8",
)
RADIO_CONFIG = PlutoRadioConfig(
    uri="ip:192.168.1.15",
    expected_serial=EXPECTED_RADIO.serial,
    radio_id=RADIO_ID,
    receiver_chain_ids=RECEIVER_CHAINS,
    block_samples=262_144,
    continuity_policy=ContinuityPolicy.ALLOW_VERIFIED_GAPPED,
    io_timeout_ms=5_000,
)
CAPTURE_IDENTITY = CaptureIdentity(
    StationId("station_leo_primary"),
    EXPECTED_RADIO.serial,
    "system-realtime-v5-metadata",
    HardwareSnapshotId("hw_v5_canary_20260813_v1"),
    "leo-flow-v5-canary-v1",
)


class CanaryDeploymentError(RuntimeError):
    """The one-shot canary cannot safely make forward progress."""


class _RadioProvider(Protocol):
    def open(self) -> PlutoPairedRadio: ...


class _PublicationProvider(Protocol):
    def build(self, local: RootedSigMFRecordingStore) -> _ReadinessCheckedPublisher: ...


class _DiskUsage(Protocol):
    @property
    def free(self) -> int: ...


def _disk_usage(path: Path) -> _DiskUsage:
    return shutil.disk_usage(path)


class ExactCanaryPlanSource:
    def get(self, plan_id: PlanId) -> CapturePlan:
        if plan_id != PLAN_ID or canonical_digest(CANARY_PLAN) != CANARY_PLAN_DIGEST:
            raise KeyError("exact V5 canary plan is unavailable")
        return CANARY_PLAN


@dataclass(frozen=True)
class _SpoolSpec:
    database_path: Path
    recording_root: Path


@dataclass(frozen=True)
class _PostgresPublicationProvider:
    dsn: str
    cas_root: Path
    timeout_s: int = POSTGRES_TIMEOUT_S

    def build(self, local: RootedSigMFRecordingStore) -> _ReadinessCheckedPublisher:
        try:
            import psycopg
            from psycopg.rows import dict_row

            from leo_flow.adapters.dashboard_projection_postgres import (
                PostgresCaptureProjectionWriter,
            )
            from leo_flow.storage.filesystem import FileSystemBlobStore
            from leo_flow.storage.postgres_catalog import (
                PostgresRecordingCatalog,
                PostgresRecordingPublisher,
            )
        except ImportError as error:
            raise CanaryDeploymentError(
                "V5 publication requires the pinned server runtime dependencies"
            ) from error

        def connect() -> Any:
            connection = psycopg.connect(
                self.dsn,
                row_factory=dict_row,
                connect_timeout=self.timeout_s,
                options=(
                    f"-c statement_timeout={self.timeout_s * 1000} "
                    f"-c lock_timeout={self.timeout_s * 1000}"
                ),
            )
            connection.execute("SET ROLE leo_capture")
            return connection

        publisher = PostgresRecordingPublisher(
            local,
            FileSystemBlobStore(self.cas_root),
            PostgresRecordingCatalog(connect),
        )
        projection = PostgresCaptureProjectionWriter(connect)
        return _ReadinessCheckedPublisher(publisher, projection, connect)


class _ReadinessCheckedPublisher:
    def __init__(
        self,
        publisher: RecordingPublisher,
        projection: CaptureProjectionWriter,
        connect: Callable[[], Any],
    ) -> None:
        self._publisher = publisher
        self._projection = projection
        self._connect = connect

    def preflight(self) -> None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    has_table_privilege(
                        current_user, 'object_blob', 'SELECT,INSERT'
                    ) AS object_catalog,
                    has_table_privilege(
                        current_user, 'recording', 'SELECT,INSERT'
                    ) AS recording_catalog,
                    has_table_privilege(
                        current_user, 'dashboard_recording_projection',
                        'SELECT,INSERT'
                    ) AS recording_projection,
                    has_table_privilege(
                        current_user, 'dashboard_activity_projection',
                        'SELECT,INSERT'
                    ) AS activity_projection,
                    has_table_privilege(
                        current_user, 'dashboard_capture_projection_identity',
                        'SELECT,INSERT'
                    ) AS projection_identity,
                    has_sequence_privilege(
                        current_user, 'dashboard_projection_sequence',
                        'USAGE,SELECT'
                    ) AS projection_sequence
                """
            ).fetchone()
        if row is None or not all(value is True for value in row.values()):
            raise CanaryDeploymentError(
                "PostgreSQL capture publication capability is incomplete"
            )

    def publish(
        self, recording: CompletedLocalRecording, *, idempotency_key: str
    ) -> PublishedRecordingRef:
        published = self._publisher.publish(recording, idempotency_key=idempotency_key)
        self._projection.project_recording(
            RecordingProjectionCommand(recording.manifest, published, True)
        )
        return published


class _V5RadioProvider:
    def open(self) -> PlutoPairedRadio:
        metadata = SpfV3MetadataReader(spf_iio_session_factory)
        return create_attested_v5_radio(
            RADIO_CONFIG,
            expected_runtime=EXPECTED_RUNTIME,
            expected_radio=EXPECTED_RADIO,
            observe_runtime=lambda: observe_current_v5_runtime(RUNTIME_MANIFEST),
            observe_radio=observe_v5_radio,
            device_factory=_open_pyadi_pluto,
            metadata_reader=metadata,
        )


class CaptureHostGuard:
    """Own the process lock and verify local capacity before radio contact."""

    def __init__(
        self,
        lock_path: Path,
        writable_roots: tuple[Path, ...],
        minimum_free_bytes: int,
        *,
        disk_usage: Callable[[Path], _DiskUsage] = _disk_usage,
    ) -> None:
        if minimum_free_bytes <= 0 or not writable_roots:
            raise ValueError("capture host guard requires roots and positive capacity")
        self._lock_path = lock_path
        self._writable_roots = writable_roots
        self._minimum_free_bytes = minimum_free_bytes
        self._disk_usage = disk_usage
        self._lock_fd: int | None = None

    def acquire(self) -> None:
        if self._lock_fd is not None:
            return
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._lock_path.parent.is_symlink():
                raise CanaryDeploymentError("capture runtime root cannot be a symlink")
            for root in self._writable_roots:
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                if root.is_symlink() or not root.is_dir():
                    raise CanaryDeploymentError(
                        "capture writable root is not a real directory"
                    )
        except OSError as error:
            raise CanaryDeploymentError("capture roots cannot be prepared") from error
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._lock_path, flags, 0o600)
        except OSError as error:
            raise CanaryDeploymentError(
                "capture instance lock cannot be opened"
            ) from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CanaryDeploymentError(
                    "capture instance lock is not a regular file"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise CanaryDeploymentError(
                "another capture instance owns the lock"
            ) from error
        except CanaryDeploymentError:
            os.close(descriptor)
            raise
        except OSError as error:
            os.close(descriptor)
            raise CanaryDeploymentError(
                "capture instance lock failed validation"
            ) from error
        try:
            for root in self._writable_roots:
                if self._disk_usage(root).free < self._minimum_free_bytes:
                    raise CanaryDeploymentError(
                        "capture capacity gate rejected the one-shot plan"
                    )
        except OSError as error:
            os.close(descriptor)
            raise CanaryDeploymentError(
                "capture capacity cannot be inspected"
            ) from error
        except CanaryDeploymentError:
            os.close(descriptor)
            raise
        self._lock_fd = descriptor

    def close(self) -> None:
        descriptor, self._lock_fd = self._lock_fd, None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


class OneShotV5CanaryCycle:
    """Recover, publish, or capture the one exact plan—never recapture on retry."""

    def __init__(
        self,
        plan_source: CapturePlanSource,
        radio_provider: _RadioProvider,
        host_guard: CaptureHostGuard,
        writer: SigMFRecordingWriter,
        spool_spec: _SpoolSpec,
        publication_provider: _PublicationProvider,
        *,
        engine: PlanCaptureEngine | None = None,
    ) -> None:
        self._plan_source = plan_source
        self._radio_provider = radio_provider
        self._host_guard = host_guard
        self._writer = writer
        self._spool_spec = spool_spec
        self._publication_provider = publication_provider
        self._engine = engine or PlanCaptureEngine(CAPTURE_IDENTITY)
        self._spool: SQLiteLocalSpool | None = None
        self._local: RootedSigMFRecordingStore | None = None
        self._publisher: _ReadinessCheckedPublisher | None = None
        self._reconciler: PublicationReconciler | None = None
        self._radio: RadioDevice | None = None
        self._closed = False

    def preflight(self) -> None:
        if self._closed:
            raise CanaryDeploymentError("closed canary cycle cannot restart")
        if self._spool is not None:
            return
        self._host_guard.acquire()
        spool = SQLiteLocalSpool(
            self._spool_spec.database_path, self._spool_spec.recording_root
        )
        local = RootedSigMFRecordingStore(self._spool_spec.recording_root)
        self._recover(spool, local)
        publisher = self._publication_provider.build(local)
        publisher.preflight()
        self._spool = spool
        self._local = local
        self._publisher = publisher
        self._reconciler = PublicationReconciler(spool, publisher, local)
        if not spool.has_durable_recording(PLAN_ID):
            self._radio = self._radio_provider.open()

    def capture_and_publish_once(self) -> bool:
        spool, reconciler = self._ready()
        if spool.has_durable_recording(PLAN_ID):
            result = reconciler.reconcile()
            self._require_reconciled(result.deferred, result.errors)
            return bool(result.published or result.cleaned)
        if self._radio is None:
            raise CanaryDeploymentError("attested V5 radio is absent")
        plan = self._plan_source.get(PLAN_ID)
        if plan is not CANARY_PLAN or canonical_digest(plan) != CANARY_PLAN_DIGEST:
            raise CanaryDeploymentError("plan source changed the immutable canary plan")
        self._engine.execute(plan, self._radio, self._writer, spool)
        result = reconciler.reconcile()
        self._require_reconciled(result.deferred, result.errors)
        return True

    def close(self, timeout_s: float) -> None:
        if timeout_s <= 0:
            raise ValueError("shutdown timeout must be positive")
        if self._closed:
            return
        self._closed = True
        radio, self._radio = self._radio, None
        failure: BaseException | None = None
        try:
            close = getattr(radio, "close", None)
            if callable(close):
                close()
        except BaseException as error:  # noqa: BLE001 - release the process lock
            failure = error
        finally:
            self._host_guard.close()
        if failure is not None:
            raise CanaryDeploymentError(
                f"V5 canary shutdown failed: {type(failure).__name__}"
            ) from failure

    def _ready(self) -> tuple[SQLiteLocalSpool, PublicationReconciler]:
        if self._spool is None or self._reconciler is None:
            raise CanaryDeploymentError("V5 canary preflight has not completed")
        return self._spool, self._reconciler

    @staticmethod
    def _recover(spool: SQLiteLocalSpool, local: RootedSigMFRecordingStore) -> None:
        for entry in spool.incomplete_allocations():
            try:
                recovered = local.recover_finalized(
                    entry.recording_id, entry.plan_id, entry.destination
                )
            except LocalRecordingNotFinalizedError:
                local.quarantine_incomplete(entry.recording_id, entry.destination)
                spool.record_failure(entry.recording_id, "capture process restarted")
            else:
                spool.record_complete(recovered)

    @staticmethod
    def _require_reconciled(deferred: int, errors: tuple[str, ...]) -> None:
        if deferred:
            kinds = ",".join(
                sorted({item.split(":", 2)[1] for item in errors if ":" in item})
            )
            raise CanaryDeploymentError(
                f"recording publication remains deferred ({kinds or 'unknown'})"
            )


def _open_pyadi_pluto(uri: str) -> PlutoDevice:
    module = importlib.import_module("adi")
    device_type = module.Pluto
    return cast(PlutoDevice, device_type(uri=uri))


def _plan_source(context: AdapterBuildContext) -> ExactCanaryPlanSource:
    del context
    return ExactCanaryPlanSource()


def _radio(context: AdapterBuildContext) -> _V5RadioProvider:
    del context
    return _V5RadioProvider()


def _preflight(context: AdapterBuildContext) -> CaptureHostGuard:
    del context
    return CaptureHostGuard(
        LOCK_PATH,
        (RECORDING_ROOT, CAS_ROOT),
        MINIMUM_FREE_BYTES,
    )


def _writer(context: AdapterBuildContext) -> SigMFRecordingWriter:
    del context
    return SigMFRecordingWriter()


def _spool(context: AdapterBuildContext) -> _SpoolSpec:
    del context
    return _SpoolSpec(SPOOL_DATABASE, RECORDING_ROOT)


def _publisher(context: AdapterBuildContext) -> _PostgresPublicationProvider:
    try:
        dsn = context.secrets[DATABASE_SECRET]
    except KeyError as error:
        raise ValueError("catalog database credential was not configured") from error
    return _PostgresPublicationProvider(dsn, CAS_ROOT)


def _build_capture(
    config: ServiceConfig,
    adapters: AdapterSet,
    diagnostics: DiagnosticSink,
) -> ServiceLoop:
    if not isinstance(config, CaptureServiceConfig):
        raise TypeError("V5 canary requires capture configuration")
    cycle = OneShotV5CanaryCycle(
        cast(CapturePlanSource, adapters[Capability.PLAN_SOURCE]),
        cast(_RadioProvider, adapters[Capability.RADIO]),
        cast(CaptureHostGuard, adapters[Capability.CAPTURE_PREFLIGHT]),
        cast(SigMFRecordingWriter, adapters[Capability.RECORDING_WRITER]),
        cast(_SpoolSpec, adapters[Capability.SPOOL]),
        cast(_PublicationProvider, adapters[Capability.RECORDING_PUBLISHER]),
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
