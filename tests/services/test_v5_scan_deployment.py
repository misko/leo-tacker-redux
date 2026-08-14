from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from leo_flow.capture.engine import PlanCaptureEngine
from leo_flow.capture.fake_radio import FakeV5PairedRadio, V5Refill
from leo_flow.contracts.capture import ActivityKind, CompletedLocalRecording
from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityPolicy,
    RefillMetadata,
)
from leo_flow.contracts.core import Digest, PlanId, canonical_digest
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.deployments import v5_scan
from leo_flow.deployments.v5_canary import (
    CaptureHostGuard,
    OneShotV5PlanCycle,
    V5SpoolSpec,
)
from leo_flow.services import Capability, Process, load_service_config
from leo_flow.storage.local_recording import RootedSigMFRecordingStore
from leo_flow.storage.recording_codec import SigMFRecordingWriter
from testkit import FakeClock


@dataclass(frozen=True)
class _Usage:
    free: int


class _Publisher:
    def __init__(self) -> None:
        self.preflights = 0
        self.calls: list[tuple[CompletedLocalRecording, str]] = []

    def preflight(self) -> None:
        self.preflights += 1

    def publish(
        self, recording: CompletedLocalRecording, *, idempotency_key: str
    ) -> PublishedRecordingRef:
        self.calls.append((recording, idempotency_key))
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
    def __init__(self, publisher: _Publisher) -> None:
        self.publisher = publisher

    def build(self, local: RootedSigMFRecordingStore) -> _Publisher:
        del local
        return self.publisher


class _RadioProvider:
    def __init__(self, radio: FakeV5PairedRadio | None) -> None:
        self.radio = radio
        self.opens = 0

    def open(self) -> FakeV5PairedRadio:
        self.opens += 1
        if self.radio is None:
            raise AssertionError("durable restart must not reopen the V5 radio")
        return self.radio


def _remote(digest: Digest, byte_count: int) -> ObjectRef:
    return ObjectRef(
        digest,
        byte_count,
        "application/octet-stream",
        "test-object-v1",
        f"cas:sha256:{digest.value}",
    )


def _radio(clock: FakeClock) -> FakeV5PairedRadio:
    sample_count = v5_scan.SCAN_PLAN.activities[0].segments[0].sample_count
    assert sample_count is not None
    paired_ci16 = bytes(sample_count * 2 * 2 * 2)
    scripts = {
        segment.segment_id: (
            V5Refill(
                paired_ci16,
                RefillMetadata(
                    refill_index=0,
                    segment_sample_offset=0,
                    sample_count=sample_count,
                    stream_id=index + 1,
                    buffer_sequence=index + 1,
                    first_sample_sequence=index * sample_count,
                    monotonic_start_ns=clock.monotonic_ns + index * 1_000_000,
                    monotonic_end_ns=clock.monotonic_ns + index * 1_000_000 + 1,
                    utc_start_ns=clock.utc_ns + index * 1_000_000,
                    utc_end_ns=clock.utc_ns + index * 1_000_000 + 1,
                    time_uncertainty_ns=1,
                    gain_db_start=(1.0, 1.0),
                    gain_db_end=(1.0, 1.0),
                    rssi_db_start=(-1.0, -1.0),
                    rssi_db_end=(-1.0, -1.0),
                ),
            ),
        )
        for index, segment in enumerate(v5_scan.SCAN_PLAN.activities[0].segments)
    }
    return FakeV5PairedRadio(
        v5_scan.RADIO_ID,
        v5_scan.RECEIVER_CHAINS,
        scripts,
        CaptureProvenance("v5", "commit", "0.25", "v3", "metadata=1"),
        continuity_policy=ContinuityPolicy.REQUIRE_CONTIGUOUS,
        clock=clock,
    )


def _cycle(
    tmp_path: Path,
    radio_provider: _RadioProvider,
    publisher: _Publisher,
    clock: FakeClock,
) -> OneShotV5PlanCycle:
    state = tmp_path / "state"
    recordings = state / "recordings"
    return OneShotV5PlanCycle(
        v5_scan.ExactV5ScanPlanSource(),
        radio_provider,
        CaptureHostGuard(
            tmp_path / "run" / "instance.lock",
            (state, recordings, state / "cas"),
            1,
            disk_usage=lambda _path: _Usage(10),
        ),
        SigMFRecordingWriter(),
        V5SpoolSpec(state / "capture-spool.sqlite3", recordings),
        _PublicationProvider(publisher),
        plan_id=v5_scan.PLAN_ID,
        exact_plan=v5_scan.SCAN_PLAN,
        exact_plan_digest=v5_scan.SCAN_PLAN_DIGEST,
        deployment_name="V5 edge scan test",
        engine=PlanCaptureEngine(v5_scan.CAPTURE_IDENTITY, clock=clock),
    )


def test_exact_plan_is_one_scan_with_eight_explicit_paired_rx_segments() -> None:
    plan = v5_scan.SCAN_PLAN
    assert [activity.kind for activity in plan.activities] == [ActivityKind.SCAN]
    assert len(plan.activities[0].segments) == 8
    assert plan.receiver_chain_ids == v5_scan.RECEIVER_CHAINS
    assert all(
        segment.receiver_chain_ids == v5_scan.RECEIVER_CHAINS
        and segment.sample_count == 262_144
        and segment.sample_rate_hz == 2_083_332.0
        for segment in plan.activities[0].segments
    )
    assert [
        (dict(segment.tags)["channel"], dict(segment.tags)["edge"])
        for segment in plan.activities[0].segments
    ] == [
        (1, "lower"),
        (1, "upper"),
        (2, "lower"),
        (2, "upper"),
        (3, "lower"),
        (3, "upper"),
        (4, "lower"),
        (4, "upper"),
    ]
    tags = dict(plan.experiment_tags)
    assert tags["analysis_on_capture_host"] is False
    assert tags["automatic_dwell"] is False


def test_exact_plan_source_is_pinned_to_the_immutable_plan_digest() -> None:
    assert str(v5_scan.SCAN_PLAN_DIGEST) == (
        "sha256:bf6947c46dbe06eaf9efcd2039785a1f432015610080c6e32965f1a58a560ab6"
    )
    assert canonical_digest(v5_scan.SCAN_PLAN) == v5_scan.SCAN_PLAN_DIGEST
    assert v5_scan.ExactV5ScanPlanSource().get(v5_scan.PLAN_ID) is v5_scan.SCAN_PLAN
    with pytest.raises(KeyError, match="unavailable"):
        v5_scan.ExactV5ScanPlanSource().get(PlanId("plan_different"))


def test_capture_deployment_has_no_analysis_scheduler_or_dwell_logic() -> None:
    source = Path(v5_scan.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
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
    prohibited = ("analysis", "jobs", "scheduler", "dashboard")
    assert not any(
        term in module.casefold() for module in imports for term in prohibited
    )
    assert "ActivityKind.DWELL" not in source
    assert "ActivityKind.TEST" not in source


def test_plugin_config_and_hardened_systemd_unit_are_exact_and_one_shot() -> None:
    config = load_service_config(Path("deploy/v5-scan/capture.json"))
    assert config.process == "capture"
    assert (
        config.plan_source_ref,
        config.radio_ref,
        config.preflight_ref,
        config.recording_writer_ref,
        config.spool_ref,
        config.recording_publisher_ref,
    ) == (
        v5_scan.PLAN_SOURCE_REF,
        v5_scan.RADIO_REF,
        v5_scan.PREFLIGHT_REF,
        v5_scan.RECORDING_WRITER_REF,
        v5_scan.SPOOL_REF,
        v5_scan.RECORDING_PUBLISHER_REF,
    )
    assert set(v5_scan.PLUGIN.builders) == {Process.CAPTURE}
    for capability, reference in (
        (Capability.PLAN_SOURCE, v5_scan.PLAN_SOURCE_REF),
        (Capability.RADIO, v5_scan.RADIO_REF),
        (Capability.CAPTURE_PREFLIGHT, v5_scan.PREFLIGHT_REF),
        (Capability.RECORDING_WRITER, v5_scan.RECORDING_WRITER_REF),
        (Capability.SPOOL, v5_scan.SPOOL_REF),
        (Capability.RECORDING_PUBLISHER, v5_scan.RECORDING_PUBLISHER_REF),
    ):
        v5_scan.PLUGIN.manifest.factory(Process.CAPTURE, capability, reference)

    unit = Path("deploy/v5-scan/leo-v5-scan.service").read_text(encoding="utf-8")
    assert "--once" in unit
    assert "/opt/leo-v5/bin/runtime-entrypoint /usr/bin/python3" in unit
    assert "LoadCredential=catalog-dsn:" in unit
    assert "DynamicUser=yes" in unit
    assert "StateDirectory=leo-flow-v5-scan" in unit
    assert "Restart=on-failure" in unit
    assert "RequiresMountsFor=/var/lib/leo-flow/objects" in unit
    assert "ConditionPathIsMountPoint=/var/lib/leo-flow/objects" in unit
    assert "SupplementaryGroups=leo-flow-cas" in unit
    assert "ReadWritePaths=/var/lib/leo-flow/objects" in unit
    assert "UMask=0007" in unit
    assert "After=network-online.target time-sync.target" in unit
    assert not any(token in unit for token in ("analysis", "dwell", "NFS", ".done"))


def test_scan_capture_is_one_shot_and_durable_restart_does_not_reopen_radio(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    first_provider = _RadioProvider(_radio(clock))
    publisher = _Publisher()
    first = _cycle(tmp_path, first_provider, publisher, clock)
    first.preflight()
    assert first.capture_and_publish_once()
    first.close(0.1)
    assert first_provider.opens == 1
    assert len(publisher.calls) == 1
    assert [item.kind for item in publisher.calls[0][0].manifest.activities] == [
        ActivityKind.SCAN
    ]
    assert len(publisher.calls[0][0].manifest.segments) == 8

    restart_provider = _RadioProvider(None)
    restart = _cycle(tmp_path, restart_provider, _Publisher(), clock)
    restart.preflight()
    assert not restart.capture_and_publish_once()
    restart.close(0.1)
    assert restart_provider.opens == 0


def test_operator_guide_contains_explicit_live_e2e_arm_and_output_contract() -> None:
    guide = Path("docs/operations/v5-scan.md").read_text(encoding="utf-8")
    assert "python3 -m leo_flow.deployments.v5_scan_e2e" in guide
    assert "--live" in guide
    assert "--confirm-radio-serial 104000b29905000e17000800065934759d" in guide
    assert "e2e-report.json" in guide
