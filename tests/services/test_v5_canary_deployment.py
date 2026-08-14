from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leo_flow.capture.fake_radio import FakeV5PairedRadio, V5Refill
from leo_flow.contracts.capture import ActivityKind, CompletedLocalRecording
from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityPolicy,
    RefillMetadata,
)
from leo_flow.contracts.core import Digest
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.deployments import v5_canary
from leo_flow.deployments.v5_canary_dry_run import main as dry_run_main
from leo_flow.services import Capability, Process, assemble_service, load_service_config
from leo_flow.storage.local_recording import (
    LocalRecordingNotFinalizedError,
    RootedSigMFRecordingStore,
)


@dataclass(frozen=True)
class _Usage:
    free: int


class _FakePublisher:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.preflights = 0
        self.calls: list[tuple[CompletedLocalRecording, str]] = []

    def preflight(self) -> None:
        self.preflights += 1

    def publish(
        self, recording: CompletedLocalRecording, *, idempotency_key: str
    ) -> PublishedRecordingRef:
        self.calls.append((recording, idempotency_key))
        if self.failures:
            self.failures -= 1
            raise OSError("database unavailable")
        return PublishedRecordingRef(
            RecordingObjectRef(
                recording.recording_id,
                _remote(recording.data_object.digest, recording.data_object.byte_count),
                _remote(
                    recording.metadata_object.digest,
                    recording.metadata_object.byte_count,
                ),
                recording.manifest_digest,
            )
        )


class _PublicationProvider:
    def __init__(self, publisher: _FakePublisher) -> None:
        self.publisher = publisher

    def build(self, local: RootedSigMFRecordingStore) -> Any:
        del local
        return self.publisher


class _ClosableRadio:
    def __init__(self) -> None:
        request = v5_canary.CANARY_PLAN.activities[0].segments[0]
        target_samples = request.sample_count or 0
        block_samples = v5_canary.RADIO_CONFIG.block_samples
        payload = bytes(block_samples * 8)
        refills = tuple(
            V5Refill(
                payload,
                RefillMetadata(
                    refill_index=index,
                    segment_sample_offset=index * block_samples,
                    sample_count=block_samples,
                    stream_id=1,
                    buffer_sequence=index + 1,
                    first_sample_sequence=1 + index * block_samples,
                    monotonic_start_ns=1 + index * 125_829_181,
                    monotonic_end_ns=2 + index * 125_829_181,
                    utc_start_ns=1_700_000_000_000_000_000 + index * 125_829_181,
                    utc_end_ns=1_700_000_000_000_000_001 + index * 125_829_181,
                    time_uncertainty_ns=1,
                    gain_db_start=(1.0, 1.0),
                    gain_db_end=(1.0, 1.0),
                    rssi_db_start=(-1.0, -1.0),
                    rssi_db_end=(-1.0, -1.0),
                ),
            )
            for index in range(target_samples // block_samples)
        )
        self._inner = FakeV5PairedRadio(
            v5_canary.RADIO_ID,
            v5_canary.RECEIVER_CHAINS,
            {request.segment_id: refills},
            CaptureProvenance("v5", "commit", "0.25", "v3", "metadata=1"),
            continuity_policy=ContinuityPolicy.REQUIRE_CONTIGUOUS,
        )
        self.closed = 0

    @property
    def radio_id(self):
        return self._inner.radio_id

    @property
    def continuity_policy(self):
        return self._inner.continuity_policy

    @property
    def capture_provenance(self):
        return self._inner.capture_provenance

    def acquire_segment_with_metadata(self, request, write_refill):
        return self._inner.acquire_segment_with_metadata(request, write_refill)

    def close(self) -> None:
        self.closed += 1


class _RadioProvider:
    def __init__(self, radio: _ClosableRadio | None) -> None:
        self.radio = radio
        self.opens = 0

    def open(self):
        self.opens += 1
        if self.radio is None:
            raise AssertionError("durable retry must not contact the radio")
        return self.radio


def _remote(digest: Digest, byte_count: int) -> ObjectRef:
    return ObjectRef(
        digest,
        byte_count,
        "application/octet-stream",
        "test-object-v1",
        f"cas:sha256:{digest.value}",
    )


def _cycle(tmp_path: Path, radio_provider, publisher: _FakePublisher):
    state = tmp_path / "state"
    recordings = state / "recordings"
    guard = v5_canary.CaptureHostGuard(
        tmp_path / "run" / "instance.lock",
        (state, recordings, state / "cas"),
        1,
        disk_usage=lambda _path: _Usage(10),
    )
    return v5_canary.OneShotV5CanaryCycle(
        v5_canary.ExactCanaryPlanSource(),
        radio_provider,
        guard,
        v5_canary.SigMFRecordingWriter(),
        v5_canary._SpoolSpec(state / "spool.sqlite3", recordings),
        _PublicationProvider(publisher),
    )


def test_exact_plan_is_passive_multirefill_contiguous_test() -> None:
    assert str(v5_canary.CANARY_PLAN_DIGEST) == (
        "sha256:823f00e447bb1c1a2e68f81b07461e1e21c5c783831080c40399bf9850f2cdae"
    )
    assert [item.kind for item in v5_canary.CANARY_PLAN.activities] == [
        ActivityKind.TEST
    ]
    assert v5_canary.RADIO_CONFIG.continuity_policy is (
        ContinuityPolicy.REQUIRE_CONTIGUOUS
    )
    assert v5_canary.CANARY_PLAN.activities[0].segments[0].sample_count == (
        30 * v5_canary.RADIO_CONFIG.block_samples
    )
    assert v5_canary.RADIO_CONFIG.frequency_tolerance_hz == 2.0
    assert all(
        key != "tx" or value == "prohibited"
        for activity in v5_canary.CANARY_PLAN.activities
        for segment in activity.segments
        for key, value in segment.tags
    )


def test_cycle_captures_once_and_restart_does_not_reopen_radio(tmp_path) -> None:
    radio = _ClosableRadio()
    first_provider = _RadioProvider(radio)
    publisher = _FakePublisher()
    first = _cycle(tmp_path, first_provider, publisher)
    first.preflight()
    assert first.capture_and_publish_once()
    first.close(0.1)
    assert first_provider.opens == 1
    assert radio.closed == 1
    assert len(publisher.calls) == 1

    retry_provider = _RadioProvider(None)
    retry = _cycle(tmp_path, retry_provider, _FakePublisher())
    retry.preflight()
    assert not retry.capture_and_publish_once()
    retry.close(0.1)
    assert retry_provider.opens == 0


def test_publication_failure_retries_same_local_recording_without_recapture(
    tmp_path,
) -> None:
    radio = _ClosableRadio()
    first_publisher = _FakePublisher(failures=1)
    first = _cycle(tmp_path, _RadioProvider(radio), first_publisher)
    first.preflight()
    with pytest.raises(v5_canary.CanaryDeploymentError, match="deferred"):
        first.capture_and_publish_once()
    first.close(0.1)
    first_key = first_publisher.calls[0][1]

    retry_radio = _RadioProvider(None)
    second_publisher = _FakePublisher()
    second = _cycle(tmp_path, retry_radio, second_publisher)
    second.preflight()
    assert second.capture_and_publish_once()
    second.close(0.1)
    assert retry_radio.opens == 0
    assert second_publisher.calls[0][1] == first_key
    assert second_publisher.calls[0][0].recording_id == (
        first_publisher.calls[0][0].recording_id
    )


def test_capacity_and_single_instance_gates_fail_before_adapter_io(tmp_path) -> None:
    roots = (tmp_path / "state",)
    low = v5_canary.CaptureHostGuard(
        tmp_path / "run-low" / "lock", roots, 10, disk_usage=lambda _path: _Usage(9)
    )
    with pytest.raises(v5_canary.CanaryDeploymentError, match="capacity"):
        low.acquire()

    lock = tmp_path / "run" / "lock"
    first = v5_canary.CaptureHostGuard(
        lock, roots, 1, disk_usage=lambda _path: _Usage(10)
    )
    second = v5_canary.CaptureHostGuard(
        lock, roots, 1, disk_usage=lambda _path: _Usage(10)
    )
    first.acquire()
    with pytest.raises(v5_canary.CanaryDeploymentError, match="another"):
        second.acquire()
    first.close()
    second.acquire()
    second.close()


def test_spool_rejects_database_or_sqlite_sidecar_symlink(tmp_path) -> None:
    state = tmp_path / "state"
    recordings = state / "recordings"
    recordings.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("owned", encoding="utf-8")
    database = state / "spool.sqlite3"
    database.symlink_to(outside)
    spec = v5_canary._SpoolSpec(database, recordings)
    with pytest.raises(v5_canary.CanaryDeploymentError, match="regular file"):
        spec.validate_local_paths()
    database.unlink()
    Path(f"{database}-wal").symlink_to(outside)
    with pytest.raises(v5_canary.CanaryDeploymentError, match="regular file"):
        spec.validate_local_paths()
    assert outside.read_text(encoding="utf-8") == "owned"


def test_recovery_promotes_finalized_pair_and_fails_only_missing_allocation() -> None:
    recovered = object()
    complete_entry = SimpleNamespace(
        recording_id="rec_complete", plan_id="plan_canary", destination="/root/slot"
    )

    class Spool:
        def __init__(self) -> None:
            self.completed: list[object] = []
            self.failed: list[tuple[object, str]] = []

        def incomplete_allocations(self):
            return (complete_entry,)

        def record_complete(self, recording):
            self.completed.append(recording)

        def record_failure(self, recording_id, reason):
            self.failed.append((recording_id, reason))

    class Local:
        def recover_finalized(self, recording_id, plan_id, destination):
            assert (recording_id, plan_id, destination) == (
                "rec_complete",
                "plan_canary",
                "/root/slot",
            )
            return recovered

    spool = Spool()
    v5_canary.OneShotV5CanaryCycle._recover(spool, Local())  # type: ignore[arg-type]
    assert spool.completed == [recovered]
    assert spool.failed == []

    class Missing(Local):
        quarantined = False

        def recover_finalized(self, recording_id, plan_id, destination):
            raise LocalRecordingNotFinalizedError("missing")

        def quarantine_incomplete(self, recording_id, destination):
            self.quarantined = True

    missing = Missing()
    spool = Spool()
    v5_canary.OneShotV5CanaryCycle._recover(spool, missing)  # type: ignore[arg-type]
    assert missing.quarantined
    assert spool.failed == [("rec_complete", "capture process restarted")]


def test_plugin_config_and_systemd_unit_are_exact_and_one_shot() -> None:
    config = load_service_config(Path("deploy/v5-canary/capture.json"))
    assert config.process == "capture"
    assert config.plan_source_ref == v5_canary.PLAN_SOURCE_REF
    assert set(v5_canary.PLUGIN.builders) == {Process.CAPTURE}
    for capability, reference in (
        (Capability.PLAN_SOURCE, v5_canary.PLAN_SOURCE_REF),
        (Capability.RADIO, v5_canary.RADIO_REF),
        (Capability.CAPTURE_PREFLIGHT, v5_canary.PREFLIGHT_REF),
        (Capability.RECORDING_WRITER, v5_canary.RECORDING_WRITER_REF),
        (Capability.SPOOL, v5_canary.SPOOL_REF),
        (Capability.RECORDING_PUBLISHER, v5_canary.RECORDING_PUBLISHER_REF),
    ):
        v5_canary.PLUGIN.manifest.factory(Process.CAPTURE, capability, reference)

    unit = Path("deploy/v5-canary/leo-v5-canary.service").read_text()
    assert "--once" in unit
    assert "/opt/leo-v5/bin/runtime-entrypoint /usr/bin/python3" in unit
    assert "LoadCredential=catalog-dsn:" in unit
    assert "DynamicUser=yes" in unit
    assert "After=network-online.target time-sync.target" in unit
    assert "Wants=network-online.target time-sync.target" in unit
    assert "StateDirectory=leo-flow-v5-canary" in unit
    assert "Restart=on-failure" in unit
    assert not any(token in unit for token in ("NFS", ".done", ".running"))


def test_plugin_assembly_resolves_only_credential_without_runtime_io(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "catalog-dsn").write_text("must-not-connect", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", os.fspath(tmp_path))
    service = assemble_service(
        load_service_config(Path("deploy/v5-canary/capture.json")),
        v5_canary.PLUGIN,
        diagnostics=lambda _event: None,
    )
    assert service.health().state.value == "stopped"
    assert "adi" not in sys.modules


def test_v5_device_factory_selects_dual_rx_ad9361(monkeypatch) -> None:
    opened: list[str] = []
    device = object()

    def ad9361(*, uri: str):
        opened.append(uri)
        return device

    module = SimpleNamespace(
        ad9361=ad9361,
        Pluto=lambda **_kwargs: pytest.fail("single-RX Pluto class must not be used"),
    )
    monkeypatch.setattr(v5_canary.importlib, "import_module", lambda name: module)

    assert v5_canary._open_pyadi_ad9361("ip:192.168.1.15") is device
    assert opened == ["ip:192.168.1.15"]


def test_plugin_import_does_not_load_hardware_database_or_touch_network() -> None:
    script = """
import json, socket, sys
before = set(sys.modules)
import leo_flow.deployments.v5_canary
loaded = set(sys.modules) - before
print(json.dumps(sorted(name for name in loaded if name in {'adi', 'psycopg'})))
"""
    environment = os.environ.copy()
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == []

    tree = ast.parse(Path(v5_canary.__file__).read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        term in module.casefold()
        for module in imports
        for term in ("analysis", "jobs", "scheduler", "inbox")
    )
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert not any(
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "socket"
        for call in calls
    )


def test_hardware_free_dry_run_rehearses_capture_and_restart(tmp_path) -> None:
    output = io.StringIO()
    errors = io.StringIO()
    assert (
        dry_run_main(["--root", str(tmp_path / "dry")], stdout=output, stderr=errors)
        == 0
    )
    result = json.loads(output.getvalue())
    assert errors.getvalue() == ""
    assert result == {
        "activity_kind": "test",
        "capture_admissions": 1,
        "continuity_policy": "require_verified",
        "event": "v5_canary_dry_run",
        "plan_digest": str(v5_canary.CANARY_PLAN_DIGEST),
        "publications": 1,
        "restart_capture_admissions": 0,
        "status": "pass",
    }
    assert "adi" not in sys.modules


def test_dry_run_refuses_a_nonempty_scratch_root(tmp_path) -> None:
    root = tmp_path / "not-empty"
    root.mkdir()
    (root / "keep").write_text("owned", encoding="utf-8")
    errors = io.StringIO()
    assert dry_run_main(["--root", str(root)], stdout=io.StringIO(), stderr=errors) == 1
    assert json.loads(errors.getvalue())["detail"] == "ValueError"
    assert (root / "keep").read_text(encoding="utf-8") == "owned"
