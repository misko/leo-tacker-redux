from __future__ import annotations

import os
from dataclasses import replace

import pytest

from leo_flow.capture.spool import SpoolState, SQLiteLocalSpool
from leo_flow.contracts.capture import LocalObjectRef
from leo_flow.contracts.core import PlanId, canonical_digest
from leo_flow.storage.local_recording import (
    LocalRecordingNotFinalizedError,
    LocalRecordingSecurityError,
    RootedSigMFRecordingStore,
)
from leo_flow.storage.recording_codec import (
    MalformedRecordingError,
    SigMFRecordingWriter,
)
from testkit import capture_plan, recording_manifest


def _finalize_without_spool_acknowledgement(root, destination):
    plan = capture_plan()
    manifest = recording_manifest()
    session = SigMFRecordingWriter().begin(
        manifest.recording_id,
        plan,
        manifest.hardware_metadata_snapshot_id,
        str(destination),
    )
    session.append_iq(manifest.segments[0].segment_id, bytes(range(64)))
    session.finish_segment(manifest.segments[0])
    return plan, manifest, session.finalize(manifest)


def test_finalized_pair_is_recovered_into_allocated_spool_row(tmp_path) -> None:
    root = tmp_path / "recordings"
    spool = SQLiteLocalSpool(
        tmp_path / "spool.sqlite3",
        root,
        id_factory=lambda: recording_manifest().recording_id,
    )
    plan = capture_plan()
    recording_id, destination = spool.allocate(plan.plan_id)
    _, manifest, finalized = _finalize_without_spool_acknowledgement(root, destination)
    assert finalized.recording_id == recording_id == manifest.recording_id

    restarted = SQLiteLocalSpool(tmp_path / "spool.sqlite3", root)
    allocations = restarted.incomplete_allocations()
    assert len(allocations) == 1
    recovered = RootedSigMFRecordingStore(root).recover_finalized(
        allocations[0].recording_id,
        allocations[0].plan_id,
        allocations[0].destination,
    )
    assert recovered == finalized
    restarted.record_complete(recovered)
    assert restarted.get(recording_id).state is SpoolState.COMPLETE
    assert restarted.pending_publication() == (finalized,)


def test_spool_refuses_completed_recording_from_another_plan(tmp_path) -> None:
    root = tmp_path / "recordings"
    spool = SQLiteLocalSpool(
        tmp_path / "spool.sqlite3",
        root,
        id_factory=lambda: recording_manifest().recording_id,
    )
    plan = capture_plan()
    _, destination = spool.allocate(plan.plan_id)
    _, _, finalized = _finalize_without_spool_acknowledgement(root, destination)
    wrong_manifest = replace(finalized.manifest, plan_id=PlanId("plan_wrong"))
    wrong = replace(
        finalized,
        manifest=wrong_manifest,
        manifest_digest=canonical_digest(wrong_manifest),
    )
    with pytest.raises(RuntimeError, match="different plan"):
        spool.record_complete(wrong)
    assert spool.get(finalized.recording_id).state is SpoolState.ALLOCATED


def test_recovery_rejects_wrong_plan_and_tampered_data(tmp_path) -> None:
    root = tmp_path / "recordings"
    root.mkdir()
    destination = root / "slot"
    plan, manifest, _ = _finalize_without_spool_acknowledgement(root, destination)
    store = RootedSigMFRecordingStore(root)
    with pytest.raises(MalformedRecordingError, match="plan ID differs"):
        store.recover_finalized(
            manifest.recording_id, PlanId("plan_wrong"), str(destination)
        )

    with (destination / "recording.data").open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(MalformedRecordingError, match="trailing bytes"):
        store.recover_finalized(manifest.recording_id, plan.plan_id, str(destination))


def test_partial_directory_is_quarantined_but_final_directory_is_never_moved(
    tmp_path,
) -> None:
    root = tmp_path / "recordings"
    root.mkdir()
    destination = root / "slot"
    partial = root / "slot.partial"
    partial.mkdir()
    (partial / "recording.data").write_bytes(b"incomplete")
    store = RootedSigMFRecordingStore(root)
    quarantined = store.quarantine_incomplete(
        recording_manifest().recording_id, str(destination)
    )
    assert quarantined is not None and quarantined.is_dir()
    assert not partial.exists()
    assert (
        store.quarantine_incomplete(recording_manifest().recording_id, str(destination))
        is None
    )

    destination.mkdir()
    with pytest.raises(LocalRecordingSecurityError, match="final recording"):
        store.quarantine_incomplete(recording_manifest().recording_id, str(destination))


def test_missing_final_pair_is_not_recoverable(tmp_path) -> None:
    root = tmp_path / "recordings"
    root.mkdir()
    with pytest.raises(LocalRecordingNotFinalizedError):
        RootedSigMFRecordingStore(root).recover_finalized(
            recording_manifest().recording_id,
            capture_plan().plan_id,
            str(root / "missing"),
        )


def test_publication_open_rejects_escape_traversal_and_symlink(tmp_path) -> None:
    root = tmp_path / "recordings"
    root.mkdir()
    destination = root / "slot"
    _, _, completed = _finalize_without_spool_acknowledgement(root, destination)
    store = RootedSigMFRecordingStore(root)

    escaped = replace(
        completed,
        data_object=LocalObjectRef(
            str(tmp_path / "outside" / "recording.data"),
            completed.data_object.digest,
            completed.data_object.byte_count,
        ),
    )
    with (
        pytest.raises(LocalRecordingSecurityError, match="exact SigMF pair"),
        store.open_data(escaped),
    ):
        pass

    data_path = destination / "recording.data"
    real_path = destination / "real.data"
    data_path.rename(real_path)
    data_path.symlink_to(real_path)
    with (
        pytest.raises(LocalRecordingSecurityError, match="no-follow"),
        store.open_data(completed),
    ):
        pass

    data_path.unlink()
    os.mkfifo(data_path)
    with (
        pytest.raises(LocalRecordingSecurityError, match="regular file"),
        store.open_data(completed),
    ):
        pass


def test_cleanup_is_exact_and_idempotent(tmp_path) -> None:
    root = tmp_path / "recordings"
    root.mkdir()
    destination = root / "slot"
    _, _, completed = _finalize_without_spool_acknowledgement(root, destination)
    store = RootedSigMFRecordingStore(root)
    unexpected = destination / "unrelated"
    unexpected.write_bytes(b"keep")
    with pytest.raises(LocalRecordingSecurityError, match="unexpected entries"):
        store.cleanup(completed)
    assert unexpected.read_bytes() == b"keep"

    unexpected.unlink()
    store.cleanup(completed)
    assert not destination.exists()
    store.cleanup(completed)


def test_recording_root_symlink_is_rejected(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(LocalRecordingSecurityError, match="cannot be a symlink"):
        RootedSigMFRecordingStore(link)
