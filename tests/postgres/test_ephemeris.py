from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import psycopg
import pytest

from leo_flow.analysis.ephemeris.catalog import (
    ArchivedEphemerisSnapshot,
    EphemerisCatalogConflictError,
)
from leo_flow.analysis.ephemeris.postgres_catalog import (
    EphemerisObjectCollisionError,
    PostgresEphemerisSnapshotCatalog,
    PostgresFencedEphemerisCommitter,
    connection_factory,
)
from leo_flow.analysis.ephemeris.resolver import TemporalEphemerisResolver
from leo_flow.analysis.ephemeris.scheduling import EphemerisRetryPolicy
from leo_flow.analysis.ephemeris.worker import EphemerisRetrievalWorker
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    EphemerisRetrievalId,
    EphemerisSnapshotId,
    JobId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSelectionPolicy,
    EphemerisSnapshot,
    EphemerisSource,
    RecordingInterval,
    ValidationResult,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.jobs import JobPayload, JobType
from leo_flow.jobs.ports import StaleLeaseError
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from testkit import digest


def object_ref(name: str) -> ObjectRef:
    return ObjectRef(
        digest(name),
        len(name),
        "application/octet-stream",
        f"ephemeris-{name}-v1",
        f"cas:sha256:{digest(name).value}",
    )


def archived(
    suffix: str = "one",
    *,
    retrieved_at: int = 1_721_177_000_000_000_000,
) -> ArchivedEphemerisSnapshot:
    snapshot = EphemerisSnapshot(
        SchemaRef(EphemerisSnapshot.SCHEMA_ID, V0_1),
        EphemerisSnapshotId(f"eph_{suffix}"),
        EphemerisRetrievalId(f"ephret_{suffix}"),
        EphemerisSource.HUGGING_FACE,
        "starlink",
        UtcNs(retrieved_at),
        object_ref(f"raw-{suffix}"),
        object_ref(f"normalized-{suffix}"),
        ArtifactRef(
            "parser-v1",
            digest("parser"),
            SchemaRef("org.leo-flow.tle-parser"),
        ),
        42,
        digest(f"norad-{suffix}"),
        UtcNs(1_721_000_000_000_000_000),
        UtcNs(1_722_000_000_000_000_000),
        ValidationResult(
            True,
            ArtifactRef(
                "policy-v1",
                digest("policy"),
                SchemaRef("org.leo-flow.tle-policy"),
            ),
            ("accepted",),
        ),
        "provider attribution",
    )
    return ArchivedEphemerisSnapshot(
        snapshot,
        object_ref(f"provenance-{suffix}"),
        digest(f"request-{suffix}").value,
    )


@pytest.mark.integration
def test_catalog_round_trips_and_idempotently_publishes_atomic_triple(
    postgres_dsn: str,
) -> None:
    catalog = PostgresEphemerisSnapshotCatalog(connection_factory(postgres_dsn))
    value = archived()
    catalog.publish(value)
    catalog.publish(value)

    assert catalog.get(value.snapshot.snapshot_id) == value
    assert catalog.get_by_retrieval(value.snapshot.retrieval_id) == value
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM ephemeris_snapshot"
        ).fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM object_blob").fetchone() == (3,)


@pytest.mark.integration
def test_catalog_conflicts_on_snapshot_or_retrieval_identity_reuse(
    postgres_dsn: str,
) -> None:
    catalog = PostgresEphemerisSnapshotCatalog(connection_factory(postgres_dsn))
    value = archived()
    catalog.publish(value)
    with pytest.raises(EphemerisCatalogConflictError):
        catalog.publish(replace(value, request_spec_digest=digest("changed").value))
    with pytest.raises(EphemerisCatalogConflictError):
        catalog.publish(
            replace(
                archived("other"),
                snapshot=replace(
                    archived("other").snapshot,
                    retrieval_id=value.snapshot.retrieval_id,
                ),
            )
        )


@pytest.mark.integration
def test_concurrent_identical_publication_has_one_visibility_point(
    postgres_dsn: str,
) -> None:
    value = archived()

    def publish(_: int) -> None:
        PostgresEphemerisSnapshotCatalog(connection_factory(postgres_dsn)).publish(
            value
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(publish, range(8)))
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM ephemeris_snapshot"
        ).fetchone() == (1,)


@pytest.mark.integration
def test_history_order_is_exact_and_deterministic(postgres_dsn: str) -> None:
    catalog = PostgresEphemerisSnapshotCatalog(connection_factory(postgres_dsn))
    late_id = archived("z", retrieved_at=200)
    early = archived("middle", retrieved_at=100)
    early_id = archived("a", retrieved_at=200)
    for value in (late_id, early, early_id):
        catalog.publish(value)
    history = catalog.history(EphemerisSource.HUGGING_FACE, "starlink")
    assert [str(item.snapshot_ref.snapshot_id) for item in history] == [
        "eph_middle",
        "eph_a",
        "eph_z",
    ]
    interval = RecordingInterval(UtcNs(150), UtcNs(175))
    policy_ref = ArtifactRef("temporal-v1", digest("temporal"))
    available = TemporalEphemerisResolver(
        history, EphemerisSelectionPolicy.AVAILABLE_THEN
    ).resolve(
        EphemerisSource.HUGGING_FACE,
        interval,
        policy_ref,
        UtcNs(300),
    )
    first_after = TemporalEphemerisResolver(
        history, EphemerisSelectionPolicy.FIRST_AFTER
    ).resolve(
        EphemerisSource.HUGGING_FACE,
        interval,
        policy_ref,
        UtcNs(300),
    )
    assert available.snapshot_ref.snapshot_id == EphemerisSnapshotId("eph_middle")
    assert first_after.snapshot_ref.snapshot_id == EphemerisSnapshotId("eph_a")


def retrieval_payload(suffix: str) -> JobPayload:
    return JobPayload.create(
        SchemaRef("org.leo-flow.ephemeris-retrieval-job"),
        {
            "retrieval_id": f"ephret_{suffix}",
            "source": "huggingface",
            "scope": "starlink",
            "request_spec": "hf-starlink-v1",
            "slot_utc_ns": 1,
        },
    )


class PreparedSnapshot:
    def __init__(self, value: ArchivedEphemerisSnapshot) -> None:
        self.value = value

    def prepare(self, request):
        assert request.retrieval_id == self.value.snapshot.retrieval_id
        return self.value


@pytest.mark.integration
def test_fenced_commit_atomically_publishes_then_completes_job(
    postgres_dsn: str,
) -> None:
    connect = connection_factory(postgres_dsn)
    jobs = PostgresJobLeaseRepository(connect)
    jobs.enqueue(
        JobId("job_one"), JobType.EPHEMERIS_RETRIEVAL, retrieval_payload("one")
    )
    lease = jobs.claim((JobType.EPHEMERIS_RETRIEVAL,), "worker", 2.0)
    assert lease is not None
    value = archived()
    completed = EphemerisRetrievalWorker(
        PreparedSnapshot(value),
        PostgresFencedEphemerisCommitter(connect),
        jobs,
        EphemerisRetryPolicy(),
    ).execute(lease)
    assert completed == value

    with psycopg.connect(postgres_dsn) as connection:
        state, artifact_id = connection.execute(
            "SELECT state, result_ref->>'artifact_id' FROM job WHERE job_id = 'job_one'"
        ).fetchone()
        snapshot_count = connection.execute(
            "SELECT count(*) FROM ephemeris_snapshot"
        ).fetchone()[0]
    assert (state, artifact_id, snapshot_count) == ("succeeded", "eph_one", 1)


@pytest.mark.integration
def test_expired_worker_cannot_publish_or_complete(postgres_dsn: str) -> None:
    connect = connection_factory(postgres_dsn)
    jobs = PostgresJobLeaseRepository(connect)
    jobs.enqueue(
        JobId("job_one"), JobType.EPHEMERIS_RETRIEVAL, retrieval_payload("one")
    )
    stale = jobs.claim((JobType.EPHEMERIS_RETRIEVAL,), "old", 0.05)
    assert stale is not None
    time.sleep(0.08)
    current = jobs.claim((JobType.EPHEMERIS_RETRIEVAL,), "new", 2.0)
    assert current is not None
    value = archived()
    result = ArtifactRef(
        str(value.snapshot.snapshot_id),
        value.provenance_object_ref.digest,
        SchemaRef(EphemerisSnapshot.SCHEMA_ID, V0_1),
    )
    committer = PostgresFencedEphemerisCommitter(connect)
    with pytest.raises(StaleLeaseError):
        committer.publish_and_complete(stale, value, result)
    assert (
        PostgresEphemerisSnapshotCatalog(connect).get(value.snapshot.snapshot_id)
        is None
    )
    committer.publish_and_complete(current, value, result)


@pytest.mark.integration
def test_lease_cannot_publish_another_retrieval(postgres_dsn: str) -> None:
    connect = connection_factory(postgres_dsn)
    jobs = PostgresJobLeaseRepository(connect)
    jobs.enqueue(
        JobId("job_one"), JobType.EPHEMERIS_RETRIEVAL, retrieval_payload("one")
    )
    lease = jobs.claim((JobType.EPHEMERIS_RETRIEVAL,), "worker", 2.0)
    assert lease is not None
    wrong = archived("other")
    result = ArtifactRef(
        str(wrong.snapshot.snapshot_id),
        wrong.provenance_object_ref.digest,
        SchemaRef(EphemerisSnapshot.SCHEMA_ID, V0_1),
    )

    with pytest.raises(ValueError, match="lease payload"):
        PostgresFencedEphemerisCommitter(connect).publish_and_complete(
            lease, wrong, result
        )
    assert (
        PostgresEphemerisSnapshotCatalog(connect).get(wrong.snapshot.snapshot_id)
        is None
    )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT state FROM job WHERE job_id = 'job_one'"
        ).fetchone() == ("leased",)


@pytest.mark.integration
def test_publication_failure_rolls_back_catalog_and_job_completion(
    postgres_dsn: str,
) -> None:
    connect = connection_factory(postgres_dsn)
    jobs = PostgresJobLeaseRepository(connect)
    jobs.enqueue(
        JobId("job_one"), JobType.EPHEMERIS_RETRIEVAL, retrieval_payload("one")
    )
    lease = jobs.claim((JobType.EPHEMERIS_RETRIEVAL,), "worker", 2.0)
    assert lease is not None
    value = archived()
    raw = value.snapshot.raw_object_ref
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO object_blob
                (digest_algorithm, digest_value, byte_count, media_type,
                 format_id, locator)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                raw.digest.algorithm.value,
                raw.digest.value,
                raw.byte_count + 1,
                raw.media_type,
                raw.format_id,
                "cas:conflicting-locator",
            ),
        )
    result = ArtifactRef(
        str(value.snapshot.snapshot_id),
        value.provenance_object_ref.digest,
        SchemaRef(EphemerisSnapshot.SCHEMA_ID, V0_1),
    )

    with pytest.raises(EphemerisObjectCollisionError):
        PostgresFencedEphemerisCommitter(connect).publish_and_complete(
            lease, value, result
        )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT state FROM job WHERE job_id = 'job_one'"
        ).fetchone() == ("leased",)
        assert connection.execute(
            "SELECT count(*) FROM ephemeris_snapshot"
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM object_blob").fetchone() == (1,)
