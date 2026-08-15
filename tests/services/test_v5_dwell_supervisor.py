from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.application import DurableDwellRequestGate, DwellRequestGate
from leo_flow.capture.engine import CaptureIdentity, PlanCaptureEngine
from leo_flow.capture.plan_repository import SQLiteCapturePlanRepository
from leo_flow.capture.publication import PublicationReconciler
from leo_flow.capture.spool import SpoolState, SQLiteLocalSpool
from leo_flow.contracts.core import HardwareSnapshotId, UtcNs
from leo_flow.deployments.v5_canary import CanaryDeploymentError, CaptureHostGuard
from leo_flow.deployments.v5_dwell_request import (
    DwellCaptureScheduleError,
    OneShotDwellCaptureScheduler,
)
from leo_flow.deployments.v5_dwell_supervisor import (
    ClockAttestation,
    RetentionCapacityPolicy,
    SQLiteSupervisorState,
    SupervisorPreflightError,
    TrustedCaptureClock,
    V5DwellSupervisor,
    one_request_service,
)
from leo_flow.maintenance.capacity import (
    CapacityConfiguration,
    CapacityRoot,
    CapacityThresholds,
)
from leo_flow.storage.local_recording import RootedSigMFRecordingStore
from testkit import FakeClock
from tests.capture._helpers import FakeCleaner, FakePublisher, FakeRecordingWriter
from tests.services.test_v5_dwell_request import RadioProvider, policy, request


def capacity(root, *, critical_bytes: int = 1) -> CapacityConfiguration:
    return CapacityConfiguration(
        CapacityThresholds(critical_bytes, critical_bytes, 0.0, 0.0),
        (CapacityRoot("capture", root),),
        "critical",
    )


def build(
    tmp_path,
    *,
    provider: RadioProvider | None = None,
    publisher: FakePublisher | None = None,
    raw_clock: FakeClock | None = None,
    synchronized: bool = True,
    critical_bytes: int = 1,
    maximum_recordings: int = 10,
    maximum_bytes: int = 10_000,
    lock_path=None,
):
    raw = raw_clock or FakeClock(utc_ns=300, monotonic_ns=100)
    trust = lambda: ClockAttestation(
        "test-clock", synchronized, UtcNs(0), UtcNs(2_000_000_000), 10
    )
    clock = TrustedCaptureClock(raw, trust, maximum_uncertainty_ns=100)
    database = tmp_path / "capture.sqlite3"
    recordings_root = tmp_path / "recordings"
    spool = SQLiteLocalSpool(database, recordings_root)
    plans = SQLiteCapturePlanRepository(database)
    selected_provider = provider or RadioProvider()
    selected_publisher = publisher or FakePublisher()
    local = RootedSigMFRecordingStore(recordings_root)
    reconciler = PublicationReconciler(spool, selected_publisher, FakeCleaner())
    scheduler = OneShotDwellCaptureScheduler(
        DurableDwellRequestGate(DwellRequestGate(policy()), plans, plans),
        selected_provider,
        PlanCaptureEngine(
            CaptureIdentity(
                request().station_id,
                "serial-test",
                "attested-test-clock",
                HardwareSnapshotId("hw_dwell_supervisor"),
                "dwell-supervisor-test",
            ),
            clock=clock,
        ),
        FakeRecordingWriter(),
        spool,
        reconciler,
    )
    supervisor = V5DwellSupervisor(
        host_guard=CaptureHostGuard(
            lock_path or tmp_path / "run" / "capture.lock",
            (tmp_path,),
            1,
        ),
        clock=clock,
        spool=spool,
        local_recordings=local,
        reconciler=reconciler,
        scheduler=scheduler,
        capacity=capacity(tmp_path, critical_bytes=critical_bytes),
        retention=RetentionCapacityPolicy(maximum_recordings, maximum_bytes, 128),
        state=SQLiteSupervisorState(database),
    )
    return supervisor, selected_provider, raw, spool, database


def test_supervisor_receipt_and_health_survive_restart_without_recapture(
    tmp_path,
) -> None:
    first, first_provider, _clock, _spool, database = build(tmp_path)
    first.start()
    result = first.process(request())
    assert result.schedule.captured_now is True
    assert result.schedule.spool_state is SpoolState.CLEANED
    assert first_provider.opens == 1
    first.close(0.1)
    persisted = SQLiteSupervisorState(database).health()
    assert persisted is not None and persisted.state == "stopped"
    assert persisted.last_receipt_digest == result.durable_receipt.identity_digest()

    replay, replay_provider, _clock, _spool, _database = build(tmp_path)
    replay.start()
    repeated = replay.process(request())
    assert repeated.schedule.captured_now is False
    assert repeated.schedule.recording_id == result.schedule.recording_id
    assert repeated.durable_receipt == result.durable_receipt
    assert replay_provider.opens == 0
    replay.close(0.1)


def test_startup_reconciles_crashed_publication_before_rejecting_expiry(
    tmp_path,
) -> None:
    failing = FakePublisher(failures_remaining=1)
    first, first_provider, raw, _spool, _database = build(tmp_path, publisher=failing)
    first.start()
    with pytest.raises(DwellCaptureScheduleError, match="publication"):
        first.process(request())
    assert first_provider.opens == 1
    first.close(0.1)

    raw.utc_ns = int(request().expires_utc_ns)
    restarted, provider, _raw, spool, _database = build(
        tmp_path, publisher=failing, raw_clock=raw
    )
    restarted.start()
    assert restarted.health().startup_published == 1
    assert restarted.health().startup_cleaned == 1
    assert spool.pending_publication() == ()
    with pytest.raises(ValueError, match="currently valid"):
        restarted.process(request())
    assert provider.opens == 0
    restarted.close(0.1)


def test_cross_process_lock_contention_fails_before_radio(tmp_path) -> None:
    lock = tmp_path / "run" / "capture.lock"
    first, _provider, _clock, _spool, _database = build(tmp_path, lock_path=lock)
    second, second_provider, _clock, _spool, _database = build(tmp_path, lock_path=lock)
    first.start()
    with pytest.raises(CanaryDeploymentError, match="another capture"):
        second.start()
    assert second_provider.opens == 0
    first.close(0.1)
    replacement, _provider, _clock, _spool, _database = build(tmp_path, lock_path=lock)
    replacement.start()
    replacement.close(0.1)


def test_low_space_and_untrusted_clock_fail_before_radio(tmp_path) -> None:
    low, low_provider, _clock, _spool, _database = build(
        tmp_path, critical_bytes=10**30
    )
    with pytest.raises(SupervisorPreflightError, match="capacity"):
        low.start()
    assert low_provider.opens == 0

    untrusted_root = tmp_path / "untrusted"
    untrusted_root.mkdir()
    untrusted, provider, _clock, _spool, _database = build(
        untrusted_root, synchronized=False
    )
    with pytest.raises(SupervisorPreflightError, match="clock"):
        untrusted.start()
    assert provider.opens == 0


def test_backwards_clock_and_retention_budget_fail_before_second_radio_open(
    tmp_path,
) -> None:
    raw = FakeClock(utc_ns=300, monotonic_ns=100)
    supervisor, provider, _clock, _spool, _database = build(tmp_path, raw_clock=raw)
    supervisor.start()
    raw.utc_ns = 299
    with pytest.raises(SupervisorPreflightError, match="backwards"):
        supervisor.process(request())
    assert provider.opens == 0
    supervisor.close(0.1)

    retention_root = tmp_path / "retention"
    retention_root.mkdir()
    bounded, provider, _clock, _spool, _database = build(
        retention_root, maximum_recordings=1
    )
    bounded.start()
    bounded.process(request())
    second = replace(
        request(), request_id="dwell_scheduler_second", idempotency_key="dwell:second"
    )
    with pytest.raises(SupervisorPreflightError, match="retained"):
        bounded.process(second)
    assert provider.opens == 1
    bounded.close(0.1)


def test_signal_aware_service_drains_and_releases_lock(tmp_path) -> None:
    lock = tmp_path / "run" / "capture.lock"
    supervisor, _provider, _clock, _spool, database = build(tmp_path, lock_path=lock)
    service = one_request_service(
        supervisor, request(), instance_id="capture-test", shutdown_timeout_s=0.2
    )
    assert service.run_once()
    service.request_stop()
    assert not service.run_once()
    service.shutdown()
    health = SQLiteSupervisorState(database).health()
    assert health is not None and health.state == "stopped" and not health.ready

    replacement, _provider, _clock, _spool, _database = build(tmp_path, lock_path=lock)
    replacement.start()
    replacement.close(0.1)
