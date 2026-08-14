from __future__ import annotations

import io
import threading
import time
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.tracking_model_analysis_postgres import (
    AtomicPostgresTrackingModelCommitter,
)
from leo_flow.adapters.tracking_model_postgres import (
    PostgresTrackingModelCatalog,
    TrackingModelConflictError,
    _parameters,
)
from leo_flow.analysis.model.tracking_model_codec import (
    TRACKING_MODEL_SNAPSHOT_FORMAT_ID,
    TRACKING_MODEL_SNAPSHOT_MEDIA_TYPE,
    encode_tracking_model_snapshot,
)
from leo_flow.analysis.model.tracking_model_persistence import (
    DurableTrackingModelRepository,
    TrackingModelIntegrityError,
    tracking_model_projection,
)
from leo_flow.contracts.core import Digest, JobId, SchemaRef
from leo_flow.contracts.tracking_input import TrackingInputSnapshotIdentity
from leo_flow.contracts.tracking_model import TrackingModelAnalysisRequest
from leo_flow.jobs import JobType, StaleLeaseError
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.maintenance.postgres_gc import PostgresGarbageCollectionCatalog
from leo_flow.services.model_analysis import tracking_model_analysis_payload
from leo_flow.storage.filesystem import FileSystemBlobStore
from tests.model_analysis.test_tracking_model_output_codec import _bundle, _evidence
from tests.postgres.test_tracking_inputs import _repository as _input_repository
from tests.postgres.test_tracking_inputs import _seed_authorities


def _connect(
    postgres_dsn: str, *, role: bool = False
) -> psycopg.Connection[dict[str, object]]:
    connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
    if role:
        connection.execute("SET ROLE leo_analysis")
    return connection


def _catalog(postgres_dsn: str, *, role: bool = False) -> PostgresTrackingModelCatalog:
    return PostgresTrackingModelCatalog(lambda: _connect(postgres_dsn, role=role))


def _output(postgres_dsn: str, root: Path):
    snapshot = _seed_authorities(postgres_dsn)
    input_ref = _input_repository(postgres_dsn, root / "input-cas", role=True).publish(
        snapshot, idempotency_key="tracking-model-input"
    )
    identity = input_ref.identity()
    evidence = replace(
        _evidence(),
        tracking_input_identity=identity,
        ordered_entry_count=1,
        ordered_entry_digest=identity.membership_digest,
    )
    return _bundle(evidence=evidence, rejected=())


def _repository(
    postgres_dsn: str, root: Path, *, role: bool = False
) -> DurableTrackingModelRepository:
    return DurableTrackingModelRepository(
        FileSystemBlobStore(root), _catalog(postgres_dsn, role=role)
    )


def _claimed(postgres_dsn: str, bundle, *, ttl_s: float = 5.0):
    request = TrackingModelAnalysisRequest(
        SchemaRef(TrackingModelAnalysisRequest.SCHEMA_ID),
        bundle.evidence.tracking_input_identity,
        bundle.evidence.config_ref,
        bundle.evidence.algorithm_ref,
    )
    jobs = PostgresJobLeaseRepository(
        lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)
    )
    jobs.enqueue(
        JobId("job_atomic_tracking_model"),
        JobType.MODEL_ANALYSIS,
        tracking_model_analysis_payload(request),
    )
    lease = jobs.claim((JobType.MODEL_ANALYSIS,), "tracking-worker", ttl_s)
    assert lease is not None
    return jobs, lease


def _committer(postgres_dsn: str, root: Path, *, role: bool = False):
    return AtomicPostgresTrackingModelCommitter(
        FileSystemBlobStore(root), lambda: _connect(postgres_dsn, role=role)
    )


@pytest.mark.integration
def test_cas_first_publish_and_exact_verified_read_as_analysis_role(
    postgres_dsn: str, tmp_path: Path
) -> None:
    bundle = _output(postgres_dsn, tmp_path)
    repository = _repository(postgres_dsn, tmp_path / "model-cas", role=True)

    ref = repository.publish(bundle, idempotency_key="tracking-model-output")

    assert repository.publish(bundle, idempotency_key="tracking-model-output") == ref
    assert repository.get(ref) == bundle
    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute(
            """
            SELECT model_snapshot_id, model_run_id, output_digest_value,
                   evidence_digest_value, tracking_input_snapshot_id,
                   bundle_byte_count, bundle_media_type, bundle_format_id
              FROM tracking_model_snapshot
            """
        ).fetchone()
        references = connection.execute(
            """
            SELECT reference_kind, owner_id FROM object_blob_live_reference
             WHERE digest_value = %s
            """,
            (ref.bundle_ref.digest.value,),
        ).fetchall()
    assert row == (
        str(bundle.model_snapshot_id),
        str(bundle.model_run_id),
        ref.output_digest.value,
        bundle.evidence.evidence_digest().value,
        bundle.evidence.tracking_input_identity.snapshot_id,
        ref.bundle_ref.byte_count,
        TRACKING_MODEL_SNAPSHOT_MEDIA_TYPE,
        TRACKING_MODEL_SNAPSHOT_FORMAT_ID,
    )
    assert references == [("tracking_model_snapshot.bundle", str(bundle.model_run_id))]


@pytest.mark.integration
def test_read_fails_closed_on_catalog_projection_substitution(
    postgres_dsn: str, tmp_path: Path
) -> None:
    bundle = _output(postgres_dsn, tmp_path)
    repository = _repository(postgres_dsn, tmp_path / "model-cas")
    ref = repository.publish(bundle, idempotency_key="tracking-model-corruption")
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            "UPDATE tracking_model_snapshot SET evidence_digest_value = %s",
            (Digest.sha256(b"substituted-evidence").value,),
        )

    with pytest.raises(TrackingModelIntegrityError, match="projection"):
        repository.get(ref)


@pytest.mark.integration
def test_atomic_committer_fences_stale_lease_and_rolls_back_registration(
    postgres_dsn: str, tmp_path: Path
) -> None:
    bundle = _output(postgres_dsn, tmp_path)
    jobs, stale = _claimed(postgres_dsn, bundle, ttl_s=0.03)
    time.sleep(0.05)
    current = jobs.claim((JobType.MODEL_ANALYSIS,), "replacement", 5.0)
    assert current is not None

    with pytest.raises(StaleLeaseError):
        _committer(postgres_dsn, tmp_path / "model-cas", role=True).commit(
            stale, bundle
        )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM tracking_model_snapshot"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM object_blob WHERE format_id = %s",
            (TRACKING_MODEL_SNAPSHOT_FORMAT_ID,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT state, lease_generation FROM job WHERE job_id = %s",
            (str(stale.job_id),),
        ).fetchone() == ("leased", current.lease_generation)


@pytest.mark.integration
@pytest.mark.parametrize("substitution", ["input", "config", "algorithm"])
def test_committer_rejects_leased_request_substitution_before_cas_write(
    postgres_dsn: str, tmp_path: Path, substitution: str
) -> None:
    bundle = _output(postgres_dsn, tmp_path)
    _, lease = _claimed(postgres_dsn, bundle)
    evidence = bundle.evidence
    if substitution == "input":
        identity = evidence.tracking_input_identity
        changed_identity = TrackingInputSnapshotIdentity(
            identity.snapshot_id,
            identity.snapshot_digest,
            identity.membership_digest,
            Digest.sha256(b"substituted-tracking-input-bundle"),
            identity.bundle_byte_count,
            identity.bundle_media_type,
            identity.bundle_format_id,
        )
        changed_evidence = replace(evidence, tracking_input_identity=changed_identity)
    elif substitution == "config":
        changed_evidence = replace(
            evidence,
            config_ref=replace(
                evidence.config_ref, digest=Digest.sha256(b"substituted-config")
            ),
        )
    else:
        changed_evidence = replace(
            evidence,
            algorithm_ref=replace(
                evidence.algorithm_ref,
                digest=Digest.sha256(b"substituted-algorithm"),
            ),
        )
    substituted = _bundle(evidence=changed_evidence, rejected=())
    cas_root = tmp_path / f"model-cas-{substitution}"

    with pytest.raises(TrackingModelIntegrityError, match="leased request"):
        _committer(postgres_dsn, cas_root).commit(lease, substituted)
    assert not any(path.is_file() for path in cas_root.rglob("*"))
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM tracking_model_snapshot"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM object_blob WHERE format_id = %s",
            (TRACKING_MODEL_SNAPSHOT_FORMAT_ID,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT state FROM job WHERE job_id = %s", (str(lease.job_id),)
        ).fetchone() == ("leased",)


@pytest.mark.integration
def test_completion_fault_rolls_back_output_and_object_registration(
    postgres_dsn: str, tmp_path: Path
) -> None:
    bundle = _output(postgres_dsn, tmp_path)
    _, lease = _claimed(postgres_dsn, bundle)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            CREATE FUNCTION reject_tracking_model_completion() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.state = 'succeeded' THEN
                    RAISE EXCEPTION 'injected tracking model completion failure';
                END IF;
                RETURN NEW;
            END
            $$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER reject_tracking_model_completion
            BEFORE UPDATE ON job FOR EACH ROW
            EXECUTE FUNCTION reject_tracking_model_completion()
            """
        )
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="injected"):
            _committer(postgres_dsn, tmp_path / "model-cas").commit(lease, bundle)
        with psycopg.connect(postgres_dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM tracking_model_snapshot"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM object_blob WHERE format_id = %s",
                (TRACKING_MODEL_SNAPSHOT_FORMAT_ID,),
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT state FROM job WHERE job_id = %s", (str(lease.job_id),)
            ).fetchone() == ("leased",)
    finally:
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute("DROP TRIGGER reject_tracking_model_completion ON job")
            connection.execute("DROP FUNCTION reject_tracking_model_completion()")

    result = _committer(postgres_dsn, tmp_path / "model-cas").commit(lease, bundle)
    with psycopg.connect(postgres_dsn) as connection:
        job = connection.execute(
            "SELECT state, result_ref FROM job WHERE job_id = %s",
            (str(lease.job_id),),
        ).fetchone()
        assert connection.execute(
            "SELECT count(*) FROM tracking_model_snapshot"
        ).fetchone() == (1,)
    assert job[0] == "succeeded"
    assert job[1]["digest_value"] == result.digest.value


@pytest.mark.integration
def test_direct_definer_rejects_input_and_object_substitution(
    postgres_dsn: str, tmp_path: Path
) -> None:
    bundle = _output(postgres_dsn, tmp_path)
    payload = encode_tracking_model_snapshot(bundle)
    store = FileSystemBlobStore(tmp_path / "model-cas")
    ref = store.put(
        io.BytesIO(payload),
        expected_digest=Digest.sha256(payload),
        expected_bytes=len(payload),
        media_type=TRACKING_MODEL_SNAPSHOT_MEDIA_TYPE,
        format_id=TRACKING_MODEL_SNAPSHOT_FORMAT_ID,
        idempotency_key="direct-definer-object",
    )
    projection = tracking_model_projection(bundle, ref)
    parameters = _parameters(projection, "direct-definer")

    for changed in (
        {**parameters["publication"].obj, "bundle_byte_count": len(payload) + 1},
        {
            **parameters["publication"].obj,
            "tracking_input_snapshot_digest_value": Digest.sha256(
                b"substituted-input"
            ).value,
        },
        {
            **parameters["publication"].obj,
            "model_run_id": "mrun_00000000000000000000000000000000",
        },
    ):
        with _connect(postgres_dsn, role=True) as connection:
            connection.execute(
                "SELECT register_live_object_blob(%s, %s, %s, %s, %s, %s)",
                (
                    ref.digest.algorithm.value,
                    ref.digest.value,
                    ref.byte_count,
                    ref.media_type,
                    ref.format_id,
                    ref.locator,
                ),
            )
            with pytest.raises(psycopg.Error):
                connection.execute(
                    "SELECT publish_tracking_model_snapshot(%s::jsonb)",
                    (psycopg.types.json.Jsonb(changed),),
                )
        with psycopg.connect(postgres_dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM object_blob WHERE digest_value = %s",
                (ref.digest.value,),
            ).fetchone() == (0,)


@pytest.mark.integration
def test_crossed_concurrent_conflicts_fail_and_rollback_losing_objects(
    postgres_dsn: str, tmp_path: Path
) -> None:
    first = _output(postgres_dsn, tmp_path)
    repository = _repository(postgres_dsn, tmp_path / "model-cas")
    first_ref = repository.publish(first, idempotency_key="identity-a")
    second_evidence = replace(
        first.evidence,
        error_policy_ref=replace(
            first.evidence.error_policy_ref,
            digest=Digest.sha256(b"second-error-policy"),
        ),
    )
    second = _bundle(evidence=second_evidence, rejected=())
    second_ref = repository.publish(second, idempotency_key="identity-b")

    third_evidence = replace(
        first.evidence,
        error_policy_ref=replace(
            first.evidence.error_policy_ref,
            digest=Digest.sha256(b"third-error-policy"),
        ),
    )
    third = _bundle(evidence=third_evidence, rejected=())
    store = FileSystemBlobStore(tmp_path / "model-cas")

    def projection(bundle):
        encoded = encode_tracking_model_snapshot(bundle)
        object_ref = store.put(
            io.BytesIO(encoded),
            expected_digest=Digest.sha256(encoded),
            expected_bytes=len(encoded),
            media_type=TRACKING_MODEL_SNAPSHOT_MEDIA_TYPE,
            format_id=TRACKING_MODEL_SNAPSHOT_FORMAT_ID,
            idempotency_key=f"crossed:{bundle.model_run_id}",
        )
        return tracking_model_projection(bundle, object_ref)

    candidates = (
        (tracking_model_projection(first, first_ref.bundle_ref), "identity-b"),
        (tracking_model_projection(second, second_ref.bundle_ref), "identity-a"),
    )
    barrier = threading.Barrier(2)
    failures: list[Exception] = []

    def publish(candidate, key: str) -> None:
        try:
            barrier.wait()
            _catalog(postgres_dsn).publish(candidate, idempotency_key=key)
        except (TrackingModelConflictError, psycopg.Error) as error:
            failures.append(error)

    threads = [
        threading.Thread(target=publish, args=item, daemon=True) for item in candidates
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert len(failures) == 2
    assert all(isinstance(error, TrackingModelConflictError) for error in failures)
    losing = projection(third)
    with pytest.raises(TrackingModelConflictError):
        _catalog(postgres_dsn).publish(losing, idempotency_key="identity-a")
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT count(*) FROM tracking_model_snapshot"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM object_blob WHERE digest_value = %s",
            (losing.bundle_ref.digest.value,),
        ).fetchone() == (0,)


@pytest.mark.integration
def test_live_tracking_model_reference_prevents_gc_and_roles_are_narrow(
    postgres_dsn: str, tmp_path: Path
) -> None:
    bundle = _output(postgres_dsn, tmp_path)
    ref = _repository(postgres_dsn, tmp_path / "model-cas").publish(
        bundle, idempotency_key="tracking-model-retained"
    )
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO object_retention_policy
                (policy_id, retain_for_seconds, grace_period_seconds,
                 allow_remote_delete, rationale)
            VALUES ('tracking-model-expire', 0, 1, true, 'retention test')
            """
        )
        connection.execute(
            """
            INSERT INTO object_retention_assignment
                (digest_algorithm, digest_value, policy_id, assigned_at, assigned_by)
            VALUES ('sha256', %s, 'tracking-model-expire',
                    clock_timestamp() - interval '2 seconds', 'test')
            """,
            (ref.bundle_ref.digest.value,),
        )
        privileges = connection.execute(
            """
            SELECT has_table_privilege('leo_analysis', 'tracking_model_snapshot', 'SELECT'),
                   has_table_privilege('leo_analysis', 'tracking_model_snapshot', 'INSERT'),
                   has_function_privilege('leo_analysis',
                       'publish_tracking_model_snapshot(jsonb)', 'EXECUTE'),
                   has_table_privilege('leo_dashboard', 'tracking_model_snapshot', 'SELECT'),
                   has_table_privilege('leo_dashboard', 'tracking_model_snapshot', 'INSERT'),
                   has_function_privilege('leo_dashboard',
                       'publish_tracking_model_snapshot(jsonb)', 'EXECUTE'),
                   has_table_privilege('leo_capture', 'tracking_model_snapshot', 'SELECT')
            """
        ).fetchone()
        routine = connection.execute(
            """
            SELECT owner.rolname, p.proconfig
              FROM pg_proc p JOIN pg_roles owner ON owner.oid = p.proowner
             WHERE p.oid = 'publish_tracking_model_snapshot(jsonb)'::regprocedure
            """
        ).fetchone()
    gc = PostgresGarbageCollectionCatalog(
        lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)
    )
    assert gc.candidates(as_of_utc_ns=time.time_ns() + 10_000_000_000, limit=10) == ()
    assert privileges == (True, False, True, True, False, False, False)
    assert routine == ("leo_routine_owner", ["search_path=pg_catalog, pg_temp"])
