from __future__ import annotations

import os
import secrets
import shutil
import struct
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from leo_flow.adapters.dwell_postgres import ConnectionFactory
from leo_flow.capture import FakeV5PairedRadio, V5Refill
from leo_flow.contracts.capture import CompletedLocalRecording
from leo_flow.contracts.continuity import (
    CaptureProvenance,
    ContinuityPolicy,
    RefillMetadata,
    SegmentContinuity,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    Digest,
    FeatureSetId,
    SegmentId,
)
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.ports import RadioDevice
from leo_flow.contracts.storage import (
    ObjectRef,
    PublishedRecordingRef,
    RecordingObjectRef,
)
from leo_flow.deployments import v5_production_path_e2e as production_path
from leo_flow.deployments.v5_dwell_e2e import (
    BLOCK_SAMPLES,
    RADIO_ID,
    RECEIVER_CHAINS,
)
from leo_flow.deployments.v5_production_path_e2e import (
    DWELL_REQUEST_ID,
    LIVE_DURATION_NS,
    LIVE_SAMPLE_COUNT,
    MAX_DSN_BYTES,
    SOURCE_IDENTITY,
    SOURCE_RECORDING,
    PostgresIdentity,
    ProductionPathQualificationError,
    QualificationInputs,
    QualificationProfile,
    _dwell_request,
    _require_empty_local_root,
    _require_non_nfs,
    _secret,
    main,
    run_qualification,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.recording_codec import SigMFRecordingWriter
from testkit import capture_plan, recording_manifest
from tests.capture._helpers import ci16

pytest_plugins = ("tests.postgres.conftest",)
ROOT = Path(__file__).resolve().parents[2]


def feature_ref() -> FeatureSetRef:
    payload = b"qualified-feature"
    return FeatureSetRef(
        FeatureSetId("fset_production_path_test"),
        AnalysisRunId("arun_production_path_test"),
        ObjectRef(
            Digest.sha256(payload),
            len(payload),
            "application/json",
            "feature-set-bundle-v0.1",
            f"cas:sha256:{Digest.sha256(payload).value}",
        ),
    )


def test_request_preserves_exact_catalog_lineage_and_bounded_live_profile() -> None:
    published = PublishedRecordingRef(SOURCE_RECORDING)
    feature = feature_ref()
    request = _dwell_request(published, feature, 2_000_000_000_000_000_000)

    assert request.request_id == DWELL_REQUEST_ID
    assert request.source.recording_id == SOURCE_RECORDING.recording_id
    assert request.source.recording_identity_digest == SOURCE_IDENTITY
    assert request.source.feature_set_ref == feature
    assert request.radio_id == RADIO_ID
    assert request.sample_count == LIVE_SAMPLE_COUNT
    assert request.duration_ns == LIVE_DURATION_NS
    assert request.expires_utc_ns - request.issued_utc_ns == 240_000_000_000


def test_local_evidence_root_gate_rejects_reuse_and_reports_non_nfs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    assert _require_empty_local_root(root) == root.resolve()
    evidence = _require_non_nfs(root)
    assert evidence["approved_local_filesystem"] is True
    assert evidence["network_filesystem_observed"] is False

    (root / "existing").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(ProductionPathQualificationError, match="absent or empty"):
        _require_empty_local_root(root)


def _mock_mount_observation(
    monkeypatch: pytest.MonkeyPatch, *, mount_id: int, mountinfo: str
) -> None:
    original = Path.read_text

    def observed(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path == Path("/proc/self/mountinfo"):
            return mountinfo
        if path.parent == Path("/proc/self/fdinfo"):
            return f"pos:\t0\nmnt_id:\t{mount_id}\n"
        return original(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", observed)


def test_filesystem_gate_rejects_unapproved_non_nfs_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _mock_mount_observation(
        monkeypatch,
        mount_id=41,
        mountinfo=f"41 0 0:1 / {root} rw - fuse.sshfs remote:/data rw\n",
    )
    with pytest.raises(ProductionPathQualificationError, match="approved local"):
        _require_non_nfs(root)


def test_absent_output_on_rejected_network_ancestor_remains_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "not-created" / "qualification"
    _mock_mount_observation(
        monkeypatch,
        mount_id=52,
        mountinfo=f"52 0 0:2 / {tmp_path} rw - nfs4 server:/unsafe rw\n",
    )
    with pytest.raises(ProductionPathQualificationError, match="approved local"):
        _require_empty_local_root(output)
    assert not output.exists()
    assert not output.parent.exists()


def test_created_output_is_removed_when_effective_mount_changes_on_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "created-then-rejected" / "qualification"
    mountinfo = (
        f"55 0 0:7 / {tmp_path} rw - ext4 /dev/approved rw\n"
        f"56 0 0:8 / {output} rw - nfs4 server:/late-overmount rw\n"
    )
    original = Path.read_text

    def observed(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path == Path("/proc/self/mountinfo"):
            return mountinfo
        return original(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", observed)
    monkeypatch.setattr(
        production_path,
        "_opened_mount_id",
        lambda path: "56" if path == output else "55",
    )
    with pytest.raises(ProductionPathQualificationError, match="approved local"):
        _require_empty_local_root(output)
    assert not output.exists()
    assert not output.parent.exists()


def test_opened_mount_id_selects_effective_same_path_overmount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "stacked"
    root.mkdir()
    mountinfo = (
        f"61 0 0:3 / {root} rw - ext4 /dev/underlay rw\n"
        f"62 0 0:4 / {root} rw - nfs4 server:/effective rw\n"
    )
    _mock_mount_observation(monkeypatch, mount_id=62, mountinfo=mountinfo)
    with pytest.raises(ProductionPathQualificationError, match="approved local"):
        _require_non_nfs(root)


def test_opened_mount_id_ignores_hidden_same_path_underlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "stacked-local"
    root.mkdir()
    mountinfo = (
        f"71 0 0:5 / {root} rw - nfs4 server:/hidden rw\n"
        f"72 0 0:6 / {root} rw - ext4 /dev/effective rw\n"
    )
    _mock_mount_observation(monkeypatch, mount_id=72, mountinfo=mountinfo)
    evidence = _require_non_nfs(root)
    assert evidence["mount_id"] == "72"
    assert evidence["mount_source"] == "/dev/effective"


def test_cli_requires_both_explicit_live_and_exact_hardware_confirmation(
    tmp_path: Path,
) -> None:
    arguments = [
        "--output-root",
        str(tmp_path / "out"),
        "--source-cas-root",
        str(tmp_path / "source"),
        "--capture-dsn-file",
        str(tmp_path / "capture"),
        "--analysis-dsn-file",
        str(tmp_path / "analysis"),
        "--audit-dsn-file",
        str(tmp_path / "audit"),
        "--migration-directory",
        str(ROOT / "migrations"),
        "--confirm-database-name",
        "wrong_database",
        "--confirm-database-owner",
        "wrong_owner",
        "--confirm-system-identifier",
        "1234567890123456789",
        "--confirm-radio-serial",
        "wrong-radio",
        "--confirm-clock-source",
        "wrong-clock",
    ]
    with pytest.raises(ProductionPathQualificationError, match="--live"):
        main(arguments)
    with pytest.raises(ProductionPathQualificationError, match="radio serial"):
        main(["--live", *arguments])


def test_dsn_file_must_be_absolute_bounded_private_and_regular(
    tmp_path: Path,
) -> None:
    dsn = tmp_path / "catalog.dsn"
    dsn.write_text("postgresql://user:secret@127.0.0.1/catalog\n", encoding="utf-8")
    dsn.chmod(0o600)
    assert _secret(dsn) == "postgresql://user:secret@127.0.0.1/catalog"

    dsn.chmod(0o640)
    with pytest.raises(ProductionPathQualificationError, match="private regular"):
        _secret(dsn)
    dsn.chmod(0o600)

    link = tmp_path / "catalog-link.dsn"
    link.symlink_to(dsn)
    with pytest.raises(ProductionPathQualificationError, match="opened safely"):
        _secret(link)

    oversized = tmp_path / "oversized.dsn"
    oversized.write_bytes(b"x" * (MAX_DSN_BYTES + 1))
    oversized.chmod(0o600)
    with pytest.raises(ProductionPathQualificationError, match="bounded"):
        _secret(oversized)

    relative = Path(os.path.relpath(dsn, Path.cwd()))
    with pytest.raises(ProductionPathQualificationError, match="must be absolute"):
        _secret(relative)


def _source_fixture(root: Path) -> RecordingObjectRef:
    plan = capture_plan()
    manifest = recording_manifest()
    segment = manifest.segments[0]
    writer = SigMFRecordingWriter().begin(
        manifest.recording_id,
        plan,
        manifest.hardware_metadata_snapshot_id,
        str(root / "local"),
    )
    payload = ci16(segment.sample_count)
    refill = RefillMetadata(
        0,
        0,
        segment.sample_count,
        11,
        21,
        31,
        100,
        200,
        int(segment.start_utc_ns),
        int(segment.start_utc_ns) + 100,
        10,
        (40.0, 41.0),
        (40.0, 41.0),
        (-50.0, -51.0),
        (-50.0, -51.0),
    )
    writer.append_refill(segment.segment_id, payload, refill)
    writer.record_continuity(
        segment.segment_id,
        SegmentContinuity.from_refills(
            segment.requested.receiver_chain_ids,
            CaptureProvenance("v5", "test", "0.25", "v3", "metadata=1"),
            (refill,),
        ),
    )
    writer.finish_segment(segment)
    completed: CompletedLocalRecording = writer.finalize(manifest)
    cas = FileSystemBlobStore(root / "cas")
    with open(completed.data_object.locator, "rb") as stream:
        data = cas.put(
            stream,
            expected_digest=completed.data_object.digest,
            expected_bytes=completed.data_object.byte_count,
            media_type="application/octet-stream",
            format_id="leo-recording-data-v1",
            idempotency_key="source:data",
        )
    with open(completed.metadata_object.locator, "rb") as stream:
        metadata = cas.put(
            stream,
            expected_digest=completed.metadata_object.digest,
            expected_bytes=completed.metadata_object.byte_count,
            media_type="application/json",
            format_id="leo-recording-metadata-v1",
            idempotency_key="source:metadata",
        )
    return RecordingObjectRef(
        completed.recording_id, data, metadata, completed.manifest_digest
    )


class _FakeLiveProvider:
    def open(self) -> RadioDevice:
        now = 1_800_000_000_000_000_000
        metadata = RefillMetadata(
            0,
            0,
            BLOCK_SAMPLES,
            100,
            200,
            300,
            1_000,
            2_000,
            now,
            now + 1_000,
            20,
            (40.0, 41.0),
            (40.0, 41.0),
            (-50.0, -51.0),
            (-50.0, -51.0),
        )
        return cast(
            RadioDevice,
            FakeV5PairedRadio(
                RADIO_ID,
                RECEIVER_CHAINS,
                {
                    SegmentId(f"seg_{DWELL_REQUEST_ID}"): (
                        V5Refill(
                            struct.pack("<hhhh", 1_000, 0, 500, 0) * BLOCK_SAMPLES,
                            metadata,
                        ),
                    )
                },
                CaptureProvenance("v5", "test", "0.25", "v3", "metadata=1"),
                continuity_policy=ContinuityPolicy.REQUIRE_CONTIGUOUS,
            ),
        )


@dataclass(frozen=True)
class _Connections:
    capture: ConnectionFactory
    analysis: ConnectionFactory
    audit: ConnectionFactory
    identity: PostgresIdentity


@contextmanager
def _scoped_connections(postgres_dsn: str) -> Iterator[_Connections]:
    suffix = uuid.uuid4().hex[:12]
    capture_login = f"wave7_capture_{suffix}"
    analysis_login = f"wave7_analysis_{suffix}"
    capture_password = secrets.token_urlsafe(24)
    analysis_password = secrets.token_urlsafe(24)
    with psycopg.connect(postgres_dsn, row_factory=dict_row) as connection:
        for login, password, capability in (
            (capture_login, capture_password, "leo_capture"),
            (analysis_login, analysis_password, "leo_analysis"),
        ):
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(login), sql.Literal(password))
            )
            connection.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(capability), sql.Identifier(login)
                )
            )
            connection.execute(
                sql.SQL(
                    "GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO {}"
                ).format(sql.Identifier(login))
            )
        identity_row = connection.execute(
            """
            SELECT current_database() AS database_name,
                   pg_get_userbyid(database.datdba) AS database_owner,
                   control.system_identifier::text AS system_identifier
              FROM pg_database AS database
              CROSS JOIN pg_control_system() AS control
             WHERE database.datname = current_database()
            """
        ).fetchone()
    assert identity_row is not None

    def factory(login: str, password: str) -> ConnectionFactory:
        dsn = make_conninfo(postgres_dsn, user=login, password=password)
        return lambda: psycopg.connect(dsn, row_factory=dict_row)

    identity = PostgresIdentity(
        str(identity_row["database_name"]),
        str(identity_row["database_owner"]),
        str(identity_row["system_identifier"]),
    )
    try:
        yield _Connections(
            capture=factory(capture_login, capture_password),
            analysis=factory(analysis_login, analysis_password),
            audit=lambda: psycopg.connect(postgres_dsn, row_factory=dict_row),
            identity=identity,
        )
    finally:
        with psycopg.connect(postgres_dsn, row_factory=dict_row) as cleanup:
            for login in (capture_login, analysis_login):
                cleanup.execute(
                    sql.SQL(
                        "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_control_system() "
                        "FROM {}"
                    ).format(sql.Identifier(login))
                )
                cleanup.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


def _test_profile() -> QualificationProfile:
    return QualificationProfile(
        1_000_000,
        800_000,
        1_825_000_000,
        1,
        BLOCK_SAMPLES,
        BLOCK_SAMPLES * 1_000,
    )


def _inputs(
    tmp_path: Path,
    source: RecordingObjectRef,
    connections: _Connections,
    tx_verifier: Callable[[], dict[str, object]],
    *,
    identity: PostgresIdentity | None = None,
) -> QualificationInputs:
    return QualificationInputs(
        output_root=tmp_path / "evidence",
        source_cas_root=tmp_path / "source" / "cas",
        capture_connect=connections.capture,
        analysis_connect=connections.analysis,
        audit_connect=connections.audit,
        radio_provider=_FakeLiveProvider(),
        tx_verifier=tx_verifier,
        clock_source="test-synchronized-clock",
        postgres_identity=identity or connections.identity,
        migration_directory=ROOT / "migrations",
        profile=_test_profile(),
        source_recording=source,
    )


def _forbidden_tx(checks: list[object]) -> Callable[[], dict[str, object]]:
    def verifier() -> dict[str, object]:
        checks.append("called")
        return {"muted": True}

    return verifier


@pytest.mark.integration
def test_composed_postgres_path_replays_without_capture_or_analysis_duplication(
    postgres_dsn: str, tmp_path: Path
) -> None:
    source = _source_fixture(tmp_path / "source")
    tx_checks: list[int] = []

    def tx_verifier() -> dict[str, object]:
        tx_checks.append(len(tx_checks) + 1)
        return {"muted": True, "check": len(tx_checks)}

    with _scoped_connections(postgres_dsn) as connections:
        report = run_qualification(_inputs(tmp_path, source, connections, tx_verifier))

    assert report["status"] == "pass"
    assert tx_checks == [1, 2]
    replay = cast(dict[str, Any], report["fresh_process_replay"])
    database = cast(dict[str, Any], report["database"])
    row_counts = cast(dict[str, int], database["row_counts"])
    postgres_identity = cast(dict[str, Any], report["postgres_identity"])
    preflight = cast(dict[str, Any], report["database_preflight"])
    roles = cast(dict[str, dict[str, Any]], report["roles"])
    assert replay["capture_reopened_radio"] is False
    assert replay["analysis_worker_claimed_duplicate"] is False
    assert row_counts["dwell_request_ingress"] == 1
    assert row_counts["recording"] == 2
    assert row_counts["feature_set"] == 2
    assert row_counts["feature_projection_work"] == 2
    assert row_counts["object_blob"] == 6
    assert row_counts["job"] == 3
    assert database["closure_exact"] is True
    assert database["terminal_job_states_exact"] is True
    assert postgres_identity["all_connections_match"] is True
    assert preflight["application_catalog_empty"] is True
    initial_counts = cast(dict[str, int], preflight["initial_table_counts"])
    assert set(initial_counts.values()) == {0}
    assert roles["capture"]["current_user"].startswith("wave7_capture_")
    assert roles["capture"]["current_user"] == roles["capture"]["session_user"]
    assert roles["analysis"]["current_user"].startswith("wave7_analysis_")


@pytest.mark.integration
def test_wrong_cluster_identity_fails_before_any_catalog_or_radio_write(
    postgres_dsn: str, tmp_path: Path
) -> None:
    source = _source_fixture(tmp_path / "source")
    tx_checks: list[object] = []
    with _scoped_connections(postgres_dsn) as connections:
        observed = connections.identity
        wrong = PostgresIdentity(
            observed.database_name,
            observed.database_owner,
            "9999999999999999999",
        )
        with pytest.raises(
            ProductionPathQualificationError, match="database identity differs"
        ):
            run_qualification(
                _inputs(
                    tmp_path,
                    source,
                    connections,
                    _forbidden_tx(tx_checks),
                    identity=wrong,
                )
            )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM object_blob").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM recording").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM job").fetchone() == (0,)
    assert tx_checks == []


@pytest.mark.integration
def test_nonempty_database_fails_before_import_capture_or_analysis(
    postgres_dsn: str, tmp_path: Path
) -> None:
    source = _source_fixture(tmp_path / "source")
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO object_blob(
                digest_algorithm, digest_value, byte_count, media_type,
                format_id, locator
            ) VALUES ('sha256', %s, 1, 'application/octet-stream', 'test', %s)
            """,
            ("a" * 64, "cas:sha256:" + "a" * 64),
        )
    tx_checks: list[object] = []
    with (
        _scoped_connections(postgres_dsn) as connections,
        pytest.raises(
            ProductionPathQualificationError, match="contains application rows"
        ),
    ):
        run_qualification(
            _inputs(tmp_path, source, connections, _forbidden_tx(tx_checks))
        )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM object_blob").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM recording").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM job").fetchone() == (0,)
    assert tx_checks == []


@pytest.mark.integration
def test_changed_migration_byte_fails_before_any_catalog_write(
    postgres_dsn: str, tmp_path: Path
) -> None:
    source = _source_fixture(tmp_path / "source")
    candidate = tmp_path / "candidate-migrations"
    shutil.copytree(ROOT / "migrations", candidate)
    head = candidate / "0032_campaign_online_analysis.sql"
    head.write_bytes(head.read_bytes() + b"\n-- changed candidate\n")
    tx_checks: list[object] = []
    with _scoped_connections(postgres_dsn) as connections:
        inputs = replace(
            _inputs(tmp_path, source, connections, _forbidden_tx(tx_checks)),
            migration_directory=candidate,
        )
        with pytest.raises(
            ProductionPathQualificationError, match="migration receipts"
        ):
            run_qualification(inputs)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM object_blob").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM job").fetchone() == (0,)
    assert tx_checks == []


@pytest.mark.integration
def test_unexpected_direct_runtime_grant_is_rejected_before_writes(
    postgres_dsn: str, tmp_path: Path
) -> None:
    source = _source_fixture(tmp_path / "source")
    tx_checks: list[object] = []
    with _scoped_connections(postgres_dsn) as connections:
        capture_connect = connections.capture
        with capture_connect() as capture_connection:
            login = capture_connection.info.user
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(
                sql.SQL("GRANT SELECT ON public.recording TO {}").format(
                    sql.Identifier(login)
                )
            )
        try:
            with pytest.raises(
                ProductionPathQualificationError, match="scoped exclusively"
            ):
                run_qualification(
                    _inputs(tmp_path, source, connections, _forbidden_tx(tx_checks))
                )
        finally:
            with psycopg.connect(postgres_dsn) as connection:
                connection.execute(
                    sql.SQL("REVOKE SELECT ON public.recording FROM {}").format(
                        sql.Identifier(login)
                    )
                )
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM recording").fetchone() == (0,)
    assert tx_checks == []
