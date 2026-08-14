from __future__ import annotations

import psycopg

from leo_flow.deployments.v5_canary import _PostgresPublicationProvider
from leo_flow.storage.local_recording import RootedSigMFRecordingStore
from leo_flow.storage.recording_codec import SigMFRecordingWriter
from testkit import capture_plan, recording_manifest


def test_canary_publisher_catalogs_and_projects_exact_recording_pair(
    postgres_dsn: str, tmp_path
) -> None:
    root = tmp_path / "recordings"
    root.mkdir()
    plan = capture_plan()
    manifest = recording_manifest()
    session = SigMFRecordingWriter().begin(
        manifest.recording_id,
        plan,
        manifest.hardware_metadata_snapshot_id,
        str(root / str(manifest.recording_id)),
    )
    session.append_iq(manifest.segments[0].segment_id, bytes(range(64)))
    session.finish_segment(manifest.segments[0])
    completed = session.finalize(manifest)

    publisher = _PostgresPublicationProvider(postgres_dsn, tmp_path / "cas").build(
        RootedSigMFRecordingStore(root)
    )
    publisher.preflight()
    first = publisher.publish(completed, idempotency_key="canary-publish")
    assert publisher.publish(completed, idempotency_key="canary-publish") == first

    with psycopg.connect(postgres_dsn) as connection:
        recording = connection.execute(
            "SELECT recording_id FROM recording WHERE recording_id = %s",
            (str(manifest.recording_id),),
        ).fetchone()
        projection = connection.execute(
            """
            SELECT recording_id, recording_object_available
              FROM dashboard_recording_projection
             WHERE recording_id = %s
            """,
            (str(manifest.recording_id),),
        ).fetchall()
    assert recording == (str(manifest.recording_id),)
    assert projection == [(str(manifest.recording_id), True)]
