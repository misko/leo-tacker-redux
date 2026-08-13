from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from leo_flow.capture.fake_radio import FakePairedRadio, Refill
from leo_flow.capture.publication import PublicationReconciler
from leo_flow.capture.spool import SpoolState, SQLiteLocalSpool
from leo_flow.contracts.storage import PublishedRecordingRef, RecordingObjectRef
from testkit import FakeClock

from ._helpers import (
    RADIO_ID,
    RECEIVERS,
    RECORDING_ID,
    FakeCleaner,
    FakePublisher,
    FakeRecordingWriter,
    ci16,
    engine,
    plan_with_activities,
    spool,
)


def completed_in_spool(tmp_path: Path):
    plan = plan_with_activities()
    request = plan.activities[0].segments[0]
    local_spool = spool(tmp_path)
    completed = engine(FakeClock()).execute(
        plan,
        FakePairedRadio(RADIO_ID, RECEIVERS, {request.segment_id: (Refill(ci16(4)),)}),
        FakeRecordingWriter(),
        local_spool,
    )
    return local_spool, completed


def test_offline_retry_uses_stable_key_and_deletes_only_after_ack(tmp_path) -> None:
    local_spool, completed = completed_in_spool(tmp_path)
    publisher = FakePublisher(failures_remaining=1)
    cleaner = FakeCleaner()
    reconciler = PublicationReconciler(local_spool, publisher, cleaner)

    first = reconciler.reconcile()
    assert (first.published, first.cleaned, first.deferred) == (0, 0, 1)
    assert cleaner.calls == []
    assert local_spool.get(completed.recording_id).state is SpoolState.COMPLETE

    second = reconciler.reconcile()
    assert (second.published, second.cleaned, second.deferred) == (1, 1, 0)
    assert publisher.calls[0][1] == publisher.calls[1][1]
    entry = local_spool.get(completed.recording_id)
    assert entry.state is SpoolState.CLEANED
    assert entry.publish_attempts == 2


def test_cleanup_failure_is_recovered_without_republishing(tmp_path) -> None:
    local_spool, completed = completed_in_spool(tmp_path)
    publisher = FakePublisher()
    cleaner = FakeCleaner(fail=True)
    reconciler = PublicationReconciler(local_spool, publisher, cleaner)

    first = reconciler.reconcile()
    assert (first.published, first.cleaned, first.deferred) == (1, 0, 1)
    assert local_spool.get(completed.recording_id).state is SpoolState.ACKNOWLEDGED
    cleaner.fail = False
    second = reconciler.reconcile()
    assert (second.published, second.cleaned, second.deferred) == (0, 1, 0)
    assert len(publisher.calls) == 1


def test_spool_payload_round_trips_across_process_restart(tmp_path) -> None:
    local_spool, completed = completed_in_spool(tmp_path)
    restarted = SQLiteLocalSpool(
        local_spool.database_path,
        local_spool.recording_root,
        id_factory=lambda: RECORDING_ID,
    )
    restored = restarted.pending_publication()
    assert restored == (completed,)


def test_abrupt_process_allocation_is_marked_failed_on_restart(tmp_path) -> None:
    database = tmp_path / "subprocess.sqlite3"
    root = tmp_path / "recordings"
    script = """
import os, sys
from pathlib import Path
from leo_flow.capture.spool import SQLiteLocalSpool
from leo_flow.contracts.core import PlanId, RecordingId
spool = SQLiteLocalSpool(Path(sys.argv[1]), Path(sys.argv[2]), id_factory=lambda: RecordingId('rec_crashed'))
spool.allocate(PlanId('plan_crash'))
os._exit(73)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(database), str(root)],
        env=environment,
        check=False,
    )
    assert completed.returncode == 73
    restarted = SQLiteLocalSpool(database, root)
    assert restarted.fail_incomplete_allocations() == 1
    entry = restarted.get(RECORDING_ID.__class__("rec_crashed"))
    assert entry.state is SpoolState.FAILED
    assert "restarted" in entry.last_error


def test_spool_refuses_to_clean_unacknowledged_recording(tmp_path) -> None:
    local_spool, completed = completed_in_spool(tmp_path)
    with pytest.raises(RuntimeError, match="requires acknowledgement"):
        local_spool.mark_cleaned(completed.recording_id)


def test_mismatched_publication_identity_is_not_acknowledged_or_cleaned(
    tmp_path,
) -> None:
    local_spool, completed = completed_in_spool(tmp_path)
    honest = FakePublisher().publish(completed, idempotency_key="construct-fixture")

    class WrongPublisher:
        def publish(self, recording, *, idempotency_key):
            wrong = RecordingObjectRef(
                honest.recording_id,
                honest.recording_object.metadata_object,
                honest.recording_object.data_object,
                honest.recording_object.manifest_digest,
            )
            return PublishedRecordingRef(wrong)

    cleaner = FakeCleaner()
    result = PublicationReconciler(local_spool, WrongPublisher(), cleaner).reconcile()
    assert result.deferred == 1
    assert local_spool.get(completed.recording_id).state is SpoolState.COMPLETE
    assert cleaner.calls == []
