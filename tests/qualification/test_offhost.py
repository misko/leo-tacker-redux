from __future__ import annotations

import grp
import io
import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.application.projection_writers import authoritative_identity
from leo_flow.contracts.core import (
    AnalysisRunId,
    Digest,
    FeatureId,
    FeatureSetId,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.features import (
    FeatureObservation,
    FeatureSetBundle,
    FeatureSetRef,
)
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.qualification import offhost
from leo_flow.qualification.offhost import (
    MAX_CONFIG_BYTES,
    REQUIRED_MIGRATION_HEAD,
    SCHEMA_ID,
    SCHEMA_VERSION,
    CasExpectation,
    Gate,
    HostReport,
    MountObservation,
    OffHostQualificationError,
    PipelineSelection,
    PostgresExpectation,
    QualificationConfig,
    _load_host_report,
    _PipelineRows,
    build_preflight_report,
    compare_host_reports,
    evaluate_pipeline,
    expected_migration_receipts,
    inspect_database_audit,
    inspect_database_role,
    inspect_host,
    load_config,
    main,
    read_probe,
    write_probe,
)


def _config(root: Path, *, digest: str = "sha256:config") -> QualificationConfig:
    group = grp.getgrgid(os.getegid())
    return QualificationConfig(
        station_id="station_offhost",
        cas=CasExpectation(root, "server:/leo-cas", "nfs4", group.gr_name),
        migration_directory=root.parent / "migrations",
        credential_names={
            "leo_capture": "capture-catalog-dsn",
            "leo_analysis": "analysis-catalog-dsn",
            "leo_dashboard": "dashboard-catalog-dsn",
            "postgres_audit": "postgres-audit-dsn",
        },
        pipeline=PipelineSelection("rec_offhost", "job_offhost"),
        config_digest=digest,
        postgres=PostgresExpectation(
            "leo_flow",
            "postgres",
            16,
            "1234567890123456789",
            REQUIRED_MIGRATION_HEAD,
            {
                "leo_capture": "capture_login",
                "leo_analysis": "analysis_login",
                "leo_dashboard": "dashboard_login",
                "postgres_audit": "audit_login",
            },
        ),
    )


def _mountinfo(root: Path, *, source: str = "server:/leo-cas") -> str:
    return f"42 31 0:51 / {root} rw,nosuid,nodev - nfs4 {source} rw,vers=4.2\n"


def _mounted_root(tmp_path: Path) -> Path:
    root = tmp_path / "objects"
    root.mkdir()
    root.chmod(stat.S_ISGID | stat.S_IRWXU | stat.S_IRWXG)
    return root


def _object(label: bytes, format_id: str) -> ObjectRef:
    digest = Digest.sha256(label)
    return ObjectRef(
        digest,
        len(label),
        "application/octet-stream",
        format_id,
        f"cas:sha256:{digest.value}",
    )


def _recording() -> RecordingObjectRef:
    return RecordingObjectRef(
        RecordingId("rec_offhost"),
        _object(b"data", "leo-recording-data-v1"),
        _object(b"metadata", "leo-recording-metadata-v1"),
        Digest.sha256(b"manifest"),
    )


def _bundle(recording: RecordingObjectRef) -> FeatureSetBundle:
    return FeatureSetBundle(
        SchemaRef(FeatureSetBundle.SCHEMA_ID),
        FeatureSetId("fset_offhost"),
        AnalysisRunId("arun_offhost"),
        recording.recording_id,
        recording.identity_digest(),
        Provenance(
            "qualification",
            "0.1",
            "test-commit",
            Digest.sha256(b"environment"),
            Digest.sha256(b"config"),
            (recording.identity_digest(),),
            (),
            UtcNs(1),
            UtcNs(2),
            "analysis-host",
        ),
        (
            FeatureObservation(
                FeatureId("feature_offhost"),
                recording.recording_id,
                SegmentId("seg_offhost"),
                "method-offhost",
                "0.1",
                0,
                1,
                1,
                UtcNs(1),
                "qualification-score",
                1.0,
                "unitless",
                receiver_chain_id=ReceiverChainId("rx_offhost"),
            ),
        ),
        (),
    )


def test_host_inspection_is_read_only_and_proves_exact_mount_and_group(
    tmp_path,
) -> None:
    root = _mounted_root(tmp_path)
    config = _config(root)
    before = set(root.iterdir())

    report = inspect_host(
        config,
        "capture",
        mountinfo=_mountinfo(root),
        access=lambda _path, _mode: True,
    )

    assert report.passed
    assert report.mount.source == "server:/leo-cas"
    assert set(root.iterdir()) == before


def test_host_inspection_fails_closed_on_wrong_backing_source(tmp_path) -> None:
    root = _mounted_root(tmp_path)
    report = inspect_host(
        _config(root),
        "analysis",
        mountinfo=_mountinfo(root, source="server:/wrong"),
        access=lambda _path, _mode: True,
    )

    assert not report.passed
    assert not next(g for g in report.gates if g.name == "cas.mount.source").passed


def test_host_inspection_fails_closed_on_wrong_export_root(tmp_path) -> None:
    root = _mounted_root(tmp_path)
    config = _config(root)
    report = inspect_host(
        replace(config, cas=replace(config.cas, mount_root="/different-export-root")),
        "analysis",
        mountinfo=_mountinfo(root),
        access=lambda _path, _mode: True,
    )

    assert not report.passed
    assert not next(g for g in report.gates if g.name == "cas.mount.root").passed


def test_config_requires_exact_fields_and_hashes_the_safe_document(tmp_path) -> None:
    root = tmp_path / "objects"
    document = {
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "station_id": "station_offhost",
        "cas": {
            "root": str(root),
            "mount_source": "server:/leo-cas",
            "filesystem_type": "nfs4",
            "group_name": grp.getgrgid(os.getegid()).gr_name,
            "mount_root": "/",
        },
        "migration_directory": str(tmp_path / "migrations"),
        "credential_names": {
            "leo_capture": "capture-catalog-dsn",
            "leo_analysis": "analysis-catalog-dsn",
            "leo_dashboard": "dashboard-catalog-dsn",
            "postgres_audit": "postgres-audit-dsn",
        },
        "postgres": {
            "database_name": "leo_flow",
            "database_owner": "postgres",
            "server_major": 16,
            "system_identifier": "1234567890123456789",
            "migration_head": REQUIRED_MIGRATION_HEAD,
            "login_names": {
                "leo_capture": "capture_login",
                "leo_analysis": "analysis_login",
                "leo_dashboard": "dashboard_login",
                "postgres_audit": "audit_login",
            },
        },
        "pipeline": {"recording_id": "rec_offhost", "job_id": "job_offhost"},
    }
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps(document))

    loaded = load_config(path)
    assert loaded.config_digest.startswith("sha256:")
    assert "postgresql://" not in json.dumps(document)

    document["unexpected"] = True
    path.write_text(json.dumps(document))
    with pytest.raises(OffHostQualificationError, match="fields are not exact"):
        load_config(path)

    document.pop("unexpected")
    document["credential_names"]["leo_dashboard"] = "analysis-catalog-dsn"
    path.write_text(json.dumps(document))
    with pytest.raises(OffHostQualificationError, match="must be distinct"):
        load_config(path)

    document["credential_names"]["leo_dashboard"] = "dashboard-catalog-dsn"
    document["postgres"]["login_names"]["leo_dashboard"] = "analysis_login"
    path.write_text(json.dumps(document))
    with pytest.raises(OffHostQualificationError, match="distinct login names"):
        load_config(path)


def test_config_input_has_a_hard_size_limit(tmp_path) -> None:
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))

    with pytest.raises(OffHostQualificationError, match="hard limit"):
        load_config(path)


def test_preflight_is_pure_and_lists_role_specific_external_inputs(tmp_path) -> None:
    root = tmp_path / "objects"
    config = _config(root)

    report = build_preflight_report(config, "analysis")
    document = report.document()

    assert report.passed
    assert report.required_database_roles == ("leo_analysis", "leo_dashboard")
    assert report.required_credential_names == (
        "analysis-catalog-dsn",
        "dashboard-catalog-dsn",
        "postgres-audit-dsn",
    )
    assert document["external_access"] == {
        "radio": False,
        "cas": False,
        "postgresql": False,
        "credentials_resolved": False,
    }
    assert document["required_inputs"]["database_connections"] == [
        {
            "role": "leo_analysis",
            "systemd_credential": "analysis-catalog-dsn",
            "dsn": "not-resolved",
        },
        {
            "role": "leo_dashboard",
            "systemd_credential": "dashboard-catalog-dsn",
            "dsn": "not-resolved",
        },
        {
            "role": "postgres_audit",
            "systemd_credential": "postgres-audit-dsn",
            "dsn": "not-resolved",
        },
    ]
    assert not root.exists()


def test_preflight_fails_closed_and_names_unresolved_template_fields(tmp_path) -> None:
    config = replace(
        _config(tmp_path / "objects"),
        station_id="station_REPLACE_WITH_STATION_ID",
        cas=CasExpectation(
            tmp_path / "objects",
            "REPLACE_WITH_EXACT_MOUNTINFO_SOURCE",
            "nfs4",
            grp.getgrgid(os.getegid()).gr_name,
        ),
    )

    report = build_preflight_report(config, "capture")

    assert not report.passed
    gate = next(
        gate for gate in report.gates if gate.name == "preflight.inputs.resolved"
    )
    assert gate.detail == "cas.mount_source,station_id"


def test_deployment_template_matches_loader_and_remains_deliberately_unresolved() -> (
    None
):
    repository = Path(__file__).resolve().parents[2]
    template = (
        repository / "deploy" / "offhost-qualification" / "qualification.example.json"
    )

    report = build_preflight_report(load_config(template), "capture")

    assert not report.passed
    unresolved = next(
        gate for gate in report.gates if gate.name == "preflight.inputs.resolved"
    )
    assert unresolved.detail == (
        "cas.filesystem_type,cas.mount_source,postgres.database_name,"
        "postgres.database_owner,postgres.login_names.leo_analysis,"
        "postgres.login_names.leo_capture,postgres.login_names.leo_dashboard,"
        "postgres.login_names.postgres_audit,postgres.system_identifier,station_id"
    )


def test_expected_migration_receipts_bind_names_to_exact_bytes(tmp_path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_first.sql").write_text("BEGIN;\nCOMMIT;\n")

    first = expected_migration_receipts(migrations)
    (migrations / "0001_first.sql").write_text("BEGIN;\nSELECT 1;\nCOMMIT;\n")
    second = expected_migration_receipts(migrations)

    assert first.keys() == {"0001_first.sql"}
    assert first != second


def test_database_inspection_sets_read_only_and_proves_receipts_and_role(
    tmp_path, monkeypatch
) -> None:
    root = _mounted_root(tmp_path)
    config = _config(root)
    config.migration_directory.mkdir()
    migration = config.migration_directory / REQUIRED_MIGRATION_HEAD
    migration.write_text("BEGIN;\nCOMMIT;\n")
    receipts = expected_migration_receipts(config.migration_directory)
    forbidden = set(offhost._FORBIDDEN_PRIVILEGES["leo_analysis"])

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return self.rows

    class Connection:
        def __init__(self, kind: str) -> None:
            self.kind = kind
            self.statements: list[str] = []
            self.extra_migration = False
            self.elevated_session = False

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.statements.append(normalized)
            if "pg_has_role" in normalized:
                assert params is not None
                return Result([{"member": params[0] == "leo_analysis"}])
            if "pg_control_system" in normalized:
                return Result(
                    [
                        {
                            "database_name": "leo_flow",
                            "database_owner": "postgres",
                            "server_version_num": "160004",
                            "system_identifier": "1234567890123456789",
                            "current_user_name": "audit_login",
                            "session_user_name": "audit_login",
                        }
                    ]
                )
            if normalized == "SHOW transaction_read_only":
                return Result([{"transaction_read_only": "on"}])
            if "FROM pg_catalog.pg_roles AS r" in normalized:
                return Result(
                    [
                        {
                            "current_user_name": "analysis_login",
                            "session_user_name": (
                                "postgres"
                                if self.elevated_session
                                else "analysis_login"
                            ),
                            "rolcanlogin": True,
                            "rolsuper": self.elevated_session,
                            "rolcreatedb": False,
                            "rolcreaterole": False,
                            "rolreplication": False,
                            "rolbypassrls": False,
                        }
                    ]
                )
            if "direct_acl_count" in normalized:
                return Result([{"direct_acl_count": 0, "owned_object_count": 0}])
            if "current_user AS role" in normalized:
                return Result(
                    [
                        {
                            "role": "leo_analysis",
                            "login": (
                                "postgres"
                                if self.elevated_session
                                else "analysis_login"
                            ),
                        }
                    ]
                )
            if "schema_migration" in normalized:
                rows = [
                    {"name": name, "sha256": digest}
                    for name, digest in receipts.items()
                ]
                if self.extra_migration:
                    rows.append({"name": "0020_extra.sql", "sha256": "f" * 64})
                return Result(rows)
            if "has_table_privilege" in normalized:
                assert params is not None
                table, privilege = params
                return Result(
                    [
                        {
                            "allowed": (str(table).removeprefix("public."), privilege)
                            not in forbidden
                        }
                    ]
                )
            if "has_function_privilege" in normalized:
                return Result([{"allowed": True}])
            if "has_sequence_privilege" in normalized:
                return Result([{"allowed": True}])
            return Result([])

    runtime_connection = Connection("runtime")
    audit_connection = Connection("audit")
    monkeypatch.setattr(
        offhost,
        "_connection_factory",
        lambda dsn: (
            lambda: (
                audit_connection if dsn == "redacted-audit-dsn" else runtime_connection
            )
        ),
    )

    class Credentials:
        def resolve(self, name: str) -> str:
            if name == "postgres-audit-dsn":
                return "redacted-audit-dsn"
            assert name == "analysis-catalog-dsn"
            return "redacted-runtime-dsn"

    assert all(
        gate.passed
        for gate in inspect_database_audit(config, credentials=Credentials())
    )
    gates = inspect_database_role(config, "leo_analysis", credentials=Credentials())

    assert all(gate.passed for gate in gates)
    assert runtime_connection.statements[0] == "SET TRANSACTION READ ONLY"
    assert "SET ROLE leo_analysis" in runtime_connection.statements
    assert not any(
        "pg_control_system" in statement or "schema_migration" in statement
        for statement in runtime_connection.statements
    )

    runtime_connection.elevated_session = True
    elevated = inspect_database_role(config, "leo_analysis", credentials=Credentials())
    assert not next(
        gate for gate in elevated if gate.name == "postgres.leo_analysis.session_login"
    ).passed
    runtime_connection.elevated_session = False

    audit_connection.extra_migration = True
    extra = inspect_database_audit(config, credentials=Credentials())
    assert not next(
        gate for gate in extra if gate.name == "postgres.audit.migration_receipts"
    ).passed
    audit_connection.extra_migration = False

    assert config.postgres is not None
    wrong_identity = replace(
        config,
        postgres=replace(config.postgres, system_identifier="9999999999999999999"),
    )
    failed = inspect_database_audit(wrong_identity, credentials=Credentials())
    assert not next(
        gate for gate in failed if gate.name == "postgres.audit.cluster_identity"
    ).passed


def test_host_comparison_requires_same_config_and_backing_store(tmp_path) -> None:
    root = _mounted_root(tmp_path)
    base = inspect_host(
        _config(root),
        "capture",
        mountinfo=_mountinfo(root),
        access=lambda _path, _mode: True,
    )
    analysis = HostReport(
        base.schema_id,
        base.schema_version,
        base.station_id,
        "sha256:different",
        "analysis",
        MountObservation(**{**base.mount.__dict__, "source": "server:/other"}),
        base.gates,
    )

    gates = compare_host_reports(_config(root), base, analysis)
    assert not next(g for g in gates if g.name == "reports.config_digest").passed
    assert not next(g for g in gates if g.name == "reports.mount_source").passed


def test_probe_requires_dual_arm_then_cross_role_reads_exact_cas_ref(
    tmp_path, monkeypatch
) -> None:
    root = _mounted_root(tmp_path)
    config = _config(root)
    monkeypatch.setattr(offhost, "_require_writable_mount", lambda *_args: None)

    with pytest.raises(OffHostQualificationError, match="not armed"):
        write_probe(
            config,
            writer_role="capture",
            probe_id="offhost_probe_a",
            arm_writes=False,
            confirmed_cas_root=root,
        )
    assert not (root / ".tmp").exists()

    receipt = write_probe(
        config,
        writer_role="capture",
        probe_id="offhost_probe_a",
        arm_writes=True,
        confirmed_cas_root=root,
    )
    gates = read_probe(config, receipt, reader_role="analysis")

    assert all(gate.passed for gate in gates)
    assert receipt.object_ref.byte_count <= 4096
    assert receipt.object_ref.locator == f"cas:sha256:{receipt.object_ref.digest.value}"


def test_armed_probe_revalidates_mount_before_creating_store(
    tmp_path, monkeypatch
) -> None:
    root = _mounted_root(tmp_path)
    config = _config(root)

    def reject_mount(*_args) -> None:
        raise OffHostQualificationError("unsafe mount")

    monkeypatch.setattr(offhost, "_require_writable_mount", reject_mount)
    with pytest.raises(OffHostQualificationError, match="unsafe mount"):
        write_probe(
            config,
            writer_role="capture",
            probe_id="offhost_probe_a",
            arm_writes=True,
            confirmed_cas_root=root,
        )
    assert not (root / ".tmp").exists()


def test_host_report_loader_rejects_missing_gate_evidence(tmp_path) -> None:
    root = _mounted_root(tmp_path)
    report = inspect_host(
        _config(root),
        "capture",
        mountinfo=_mountinfo(root),
        access=lambda _path, _mode: True,
    ).document()
    report["gates"] = []
    report["status"] = "pass"
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))

    with pytest.raises(OffHostQualificationError, match="gates are not exact"):
        _load_host_report(path)


def test_v03_inspect_report_load_and_compare_round_trip(tmp_path) -> None:
    root = _mounted_root(tmp_path)
    config = _config(root)

    def report(host_role: str) -> HostReport:
        database_names = offhost._expected_host_report_gates(host_role) - set(
            offhost._HOST_GATE_NAMES
        )
        return inspect_host(
            config,
            host_role,
            mountinfo=_mountinfo(root),
            access=lambda _path, _mode: True,
            database_gates=tuple(
                Gate(name, True, "round-trip") for name in sorted(database_names)
            ),
        )

    capture_path = tmp_path / "capture.json"
    analysis_path = tmp_path / "analysis.json"
    capture_path.write_text(json.dumps(report("capture").document()))
    analysis_path.write_text(json.dumps(report("analysis").document()))

    gates = compare_host_reports(
        config,
        _load_host_report(capture_path),
        _load_host_report(analysis_path),
    )
    assert all(gate.passed for gate in gates)


def test_pipeline_gate_binds_every_stage_to_exact_contract_identity(tmp_path) -> None:
    recording = _recording()
    bundle = _bundle(recording)
    bundle_ref = _object(b"feature-bundle", "feature-set-bundle-v0.1")
    feature_identity = authoritative_identity(
        "feature",
        "feature_offhost",
        {
            "feature_set_id": str(bundle.feature_set_id),
            "analysis_run_id": str(bundle.analysis_run_id),
            "bundle_ref": {
                "digest": str(bundle_ref.digest),
                "byte_count": bundle_ref.byte_count,
                "media_type": bundle_ref.media_type,
                "format_id": bundle_ref.format_id,
            },
        },
    )
    rows = _PipelineRows(
        recording,
        "succeeded",
        recording,
        FeatureSetRef(bundle.feature_set_id, bundle.analysis_run_id, bundle_ref),
        str(recording.recording_id),
        recording.identity_digest(),
        1,
        0,
        ("feature_offhost",),
        (("feature_offhost", feature_identity.digest),),
    )

    report = evaluate_pipeline(
        PipelineSelection("rec_offhost", "job_offhost"),
        rows,
        "rec_offhost",
        bundle,
    )
    assert report.passed

    wrong_rows = _PipelineRows(
        rows.recording,
        rows.job_state,
        rows.submitted_recording,
        rows.feature_ref,
        rows.feature_recording_id,
        rows.feature_input_recording_digest,
        rows.feature_observation_count,
        rows.feature_method_score_count,
        (),
        rows.dashboard_feature_identity_digests,
    )
    failed = evaluate_pipeline(
        PipelineSelection("rec_offhost", "job_offhost"),
        wrong_rows,
        "rec_offhost",
        bundle,
    )
    assert not next(
        gate for gate in failed.gates if gate.name == "pipeline.dashboard_reference"
    ).passed

    wrong_input = evaluate_pipeline(
        PipelineSelection("rec_offhost", "job_offhost"),
        replace(rows, feature_input_recording_digest=Digest.sha256(b"wrong")),
        "rec_offhost",
        bundle,
    )
    assert not next(
        gate
        for gate in wrong_input.gates
        if gate.name == "pipeline.feature_input_identity"
    ).passed

    empty_bundle = replace(bundle, observations=())
    empty_rows = replace(
        rows,
        feature_observation_count=0,
        dashboard_feature_ids=(),
        dashboard_feature_identity_digests=(),
    )
    assert evaluate_pipeline(
        PipelineSelection("rec_offhost", "job_offhost"),
        empty_rows,
        "rec_offhost",
        empty_bundle,
    ).passed


def test_cli_unarmed_probe_fails_without_touching_cas(tmp_path) -> None:
    root = _mounted_root(tmp_path)
    config = {
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "station_id": "station_offhost",
        "cas": {
            "root": str(root),
            "mount_source": "server:/leo-cas",
            "filesystem_type": "nfs4",
            "group_name": grp.getgrgid(os.getegid()).gr_name,
            "mount_root": "/",
        },
        "migration_directory": str(tmp_path / "migrations"),
        "credential_names": {
            "leo_capture": "capture-catalog-dsn",
            "leo_analysis": "analysis-catalog-dsn",
            "leo_dashboard": "dashboard-catalog-dsn",
            "postgres_audit": "postgres-audit-dsn",
        },
        "postgres": {
            "database_name": "leo_flow",
            "database_owner": "postgres",
            "server_major": 16,
            "system_identifier": "1234567890123456789",
            "migration_head": REQUIRED_MIGRATION_HEAD,
            "login_names": {
                "leo_capture": "capture_login",
                "leo_analysis": "analysis_login",
                "leo_dashboard": "dashboard_login",
                "postgres_audit": "audit_login",
            },
        },
        "pipeline": None,
    }
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps(config))

    code = main(
        [
            "--config",
            str(path),
            "write-probe",
            "--host-role",
            "capture",
            "--probe-id",
            "offhost_probe_a",
            "--confirm-cas-root",
            str(root),
        ]
    )
    assert code == 3
    assert not (root / ".tmp").exists()


def test_cli_preflight_never_inspects_mount_database_or_credentials(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "objects"
    config = {
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "station_id": "station_offhost",
        "cas": {
            "root": str(root),
            "mount_source": "server:/leo-cas",
            "filesystem_type": "nfs4",
            "group_name": grp.getgrgid(os.getegid()).gr_name,
            "mount_root": "/",
        },
        "migration_directory": str(tmp_path / "migrations"),
        "credential_names": {
            "leo_capture": "capture-catalog-dsn",
            "leo_analysis": "analysis-catalog-dsn",
            "leo_dashboard": "dashboard-catalog-dsn",
            "postgres_audit": "postgres-audit-dsn",
        },
        "postgres": {
            "database_name": "leo_flow",
            "database_owner": "postgres",
            "server_major": 16,
            "system_identifier": "1234567890123456789",
            "migration_head": REQUIRED_MIGRATION_HEAD,
            "login_names": {
                "leo_capture": "capture_login",
                "leo_analysis": "analysis_login",
                "leo_dashboard": "dashboard_login",
                "postgres_audit": "audit_login",
            },
        },
        "pipeline": None,
    }
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps(config))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("preflight performed external inspection")

    monkeypatch.setattr(offhost, "inspect_host", forbidden)
    monkeypatch.setattr(offhost, "inspect_database_audit", forbidden)
    monkeypatch.setattr(offhost, "inspect_database_role", forbidden)
    monkeypatch.setattr(offhost, "_connection_factory", forbidden)
    stdout = io.StringIO()

    code = main(
        ["--config", str(path), "preflight", "--host-role", "capture"],
        stdout=stdout,
    )

    assert code == 0
    document = json.loads(stdout.getvalue())
    assert document["mode"] == "dry-run"
    assert document["required_inputs"]["systemd_credentials"] == [
        "capture-catalog-dsn",
        "postgres-audit-dsn",
    ]
    assert not root.exists()
