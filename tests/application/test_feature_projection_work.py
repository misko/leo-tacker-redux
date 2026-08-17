from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace

import pytest

from leo_flow.application.feature_projection_work import (
    FEATURE_PROJECTION_WORK_SCHEMA,
    FeatureProjectionWork,
    FeatureProjectionWorker,
    FeatureProjectionWorkLease,
)
from leo_flow.application.projection_writers import (
    FeatureProjectionCommand,
    ProjectionReceipt,
)
from leo_flow.contracts.core import JobId, UtcNs
from leo_flow.contracts.features import FeatureSetBundle, FeatureSetRef
from leo_flow.contracts.storage import PublishedRecordingRef
from tests.projection_writer_fixtures import (
    feature_bundle_and_ref,
    published_recording,
    recording_manifest,
)


class _FeatureView:
    def __init__(self, ref: FeatureSetRef, bundle: FeatureSetBundle) -> None:
        self.ref = ref
        self._bundle = bundle

    def bundle(self) -> FeatureSetBundle:
        return self._bundle


class _Features:
    def __init__(self, ref: FeatureSetRef, bundle: FeatureSetBundle) -> None:
        self.ref = ref
        self.bundle = bundle

    def open(self, ref: FeatureSetRef) -> AbstractContextManager[_FeatureView]:
        assert ref == self.ref
        return nullcontext(_FeatureView(self.ref, self.bundle))


class _Recordings:
    def __init__(self, recording: PublishedRecordingRef | None) -> None:
        self.recording = recording

    def get(self, recording_id):
        if self.recording is None:
            return None
        assert recording_id == self.recording.recording_object.recording_id
        return self.recording


class _Writer:
    def __init__(self, failure: Exception | None = None) -> None:
        self.commands: list[FeatureProjectionCommand] = []
        self.failure = failure

    def project_features(self, command: FeatureProjectionCommand) -> ProjectionReceipt:
        self.commands.append(command)
        if self.failure is not None:
            raise self.failure
        return ProjectionReceipt((1,))

    def project_model(self, command):
        raise NotImplementedError

    def project_model_release(self, command):
        raise NotImplementedError

    def project_track(self, command):
        raise NotImplementedError

    def project_storage_health(self, health):
        raise NotImplementedError


class _Work:
    def __init__(self, lease: FeatureProjectionWorkLease) -> None:
        self.lease = lease
        self.completed: list[tuple[str, str, int]] = []
        self.retried: list[tuple[str, str, int, str, float]] = []
        self.parked: list[tuple[str, str, int, str]] = []

    def claim(self, worker_id: str, ttl_s: float):
        del worker_id, ttl_s
        lease, self.lease = self.lease, None
        return lease

    def heartbeat(self, work_id, lease_token, generation, ttl_s):
        del work_id, lease_token, generation, ttl_s
        raise NotImplementedError

    def complete(self, work_id: str, lease_token: str, generation: int) -> None:
        self.completed.append((work_id, lease_token, generation))

    def retry(
        self,
        work_id: str,
        lease_token: str,
        generation: int,
        reason: str,
        delay_s: float,
    ) -> None:
        self.retried.append((work_id, lease_token, generation, reason, delay_s))

    def park(
        self, work_id: str, lease_token: str, generation: int, reason: str
    ) -> None:
        self.parked.append((work_id, lease_token, generation, reason))


def _case(*, zero_observations: bool = False):
    manifest = recording_manifest(31)
    recording = published_recording(manifest)
    bundle, feature_ref = feature_bundle_and_ref(
        recording.recording_object, manifest, 31
    )
    if zero_observations:
        bundle = replace(bundle, observations=())
        from leo_flow.contracts.core import Digest, canonical_json_bytes

        payload = canonical_json_bytes(bundle)
        feature_ref = replace(
            feature_ref,
            bundle_ref=replace(
                feature_ref.bundle_ref,
                digest=Digest.sha256(payload),
                byte_count=len(payload),
            ),
        )
    item = FeatureProjectionWork(
        FEATURE_PROJECTION_WORK_SCHEMA,
        "fpwork_" + "a" * 64,
        JobId("job_projection_31"),
        feature_ref,
        manifest.recording_id,
        recording.recording_object.identity_digest(),
    )
    lease = FeatureProjectionWorkLease(item, 1, "lease_projection_31", 1, UtcNs(10_000))
    return recording, bundle, feature_ref, lease


def _worker(recording, bundle, feature_ref, work, writer):
    return FeatureProjectionWorker(
        work,
        _Features(feature_ref, bundle),
        _Recordings(recording),
        writer,
        worker_id="projection-worker",
        lease_ttl_s=10,
        retry_delay_s=2,
    )


@pytest.mark.parametrize("zero_observations", [False, True])
def test_worker_resolves_public_artifacts_and_completes(
    zero_observations: bool,
) -> None:
    recording, bundle, feature_ref, lease = _case(zero_observations=zero_observations)
    work = _Work(lease)
    writer = _Writer()
    assert _worker(recording, bundle, feature_ref, work, writer).process_one_work()
    assert writer.commands == [FeatureProjectionCommand(bundle, feature_ref, recording)]
    assert work.completed == [
        (lease.work.work_id, lease.lease_token, lease.lease_generation)
    ]
    assert work.parked == []
    assert work.retried == []


def test_recording_identity_mismatch_parks_without_projection() -> None:
    recording, bundle, feature_ref, lease = _case()
    work = _Work(
        replace(
            lease,
            work=replace(
                lease.work,
                recording_identity_digest=feature_ref.bundle_ref.digest,
            ),
        )
    )
    writer = _Writer()
    assert _worker(recording, bundle, feature_ref, work, writer).process_one_work()
    assert writer.commands == []
    assert work.completed == []
    assert work.parked[0][3] == "projection_identity_mismatch"


def test_transient_projection_failure_is_retried_and_raised() -> None:
    recording, bundle, feature_ref, lease = _case()
    work = _Work(lease)
    writer = _Writer(RuntimeError("database unavailable"))
    with pytest.raises(RuntimeError, match="unavailable"):
        _worker(recording, bundle, feature_ref, work, writer).process_one_work()
    assert work.retried == [
        (
            lease.work.work_id,
            lease.lease_token,
            lease.lease_generation,
            "projection_transient_failure",
            2,
        )
    ]


def test_transient_projection_failure_parks_at_attempt_limit() -> None:
    recording, bundle, feature_ref, lease = _case()
    lease = replace(lease, attempt=3)
    work = _Work(lease)
    writer = _Writer(RuntimeError("database unavailable"))

    assert _worker(recording, bundle, feature_ref, work, writer).process_one_work()

    assert work.retried == []
    assert work.parked == [
        (
            lease.work.work_id,
            lease.lease_token,
            lease.lease_generation,
            "projection_attempts_exhausted",
        )
    ]
