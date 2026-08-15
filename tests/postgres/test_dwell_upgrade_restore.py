from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from leo_flow.adapters.dwell_postgres import (
    PostgresDwellRequestIngress,
    PostgresDwellRequestQueue,
)
from leo_flow.contracts.capture import CapturePlanRef
from leo_flow.contracts.core import Digest, PlanId
from leo_flow.jobs.ports import StaleLeaseError
from leo_flow.maintenance import create_backup, restore_backup, verify_backup
from leo_flow.maintenance.postgres_backup import CommandResult
from leo_flow.qualification.offhost import (
    REQUIRED_MIGRATION_HEAD,
    CasExpectation,
    PostgresExpectation,
    QualificationConfig,
    inspect_database_audit,
    inspect_database_role,
)
from leo_flow.storage.postgres_migrations import apply_migrations
from tests.postgres.test_dwell_request_ingress import _request
from tests.postgres.test_feature_sets import _repository


def _database_dsn(postgres_dsn: str, database_name: str) -> str:
    parameters = conninfo_to_dict(postgres_dsn)
    parameters["dbname"] = database_name
    return make_conninfo(**parameters)


def _runtime_connect(dsn: str):
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _create_database(postgres_dsn: str, database_name: str) -> str:
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    return _database_dsn(postgres_dsn, database_name)


def _create_runtime_login(
    postgres_dsn: str,
    database_name: str,
    login_name: str,
    password: str,
    capability: str,
) -> str:
    with psycopg.connect(postgres_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(login_name), sql.Literal(password))
        )
        connection.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(capability), sql.Identifier(login_name)
            )
        )
    parameters = conninfo_to_dict(postgres_dsn)
    parameters.update(user=login_name, password=password, dbname=database_name)
    return make_conninfo(**parameters)


def _docker(command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        pytest.fail(f"disposable PostgreSQL command failed: {completed.stderr.strip()}")
    return completed


class _DockerBackupRunner:
    def __init__(
        self,
        container: str,
        database_user: str,
        services: dict[str, tuple[str, str]],
        suffix: str,
    ) -> None:
        self.container = container
        self.database_user = database_user
        self.services = services
        self.archive = f"/tmp/wave7-production-api-{suffix}.dump"

    def __call__(self, command, _environment) -> CommandResult:
        if command[:2] == ["pg_dump", "--version"]:
            completed = _docker(
                ["docker", "exec", self.container, "pg_dump", "--version"]
            )
            return CommandResult(0, completed.stdout, completed.stderr)
        if command[0] == "psql":
            service = str(command[command.index("--dbname") + 1]).removeprefix(
                "service="
            )
            dsn, _database_name = self.services[service]
            with psycopg.connect(dsn) as connection:
                rows = connection.execute(
                    "SELECT name, sha256 FROM schema_migration ORDER BY name"
                ).fetchall()
            return CommandResult(
                0, "".join(f"{name}\t{digest}\n" for name, digest in rows)
            )
        if command[0] == "pg_dump":
            service = str(command[command.index("--dbname") + 1]).removeprefix(
                "service="
            )
            _dsn, database_name = self.services[service]
            destination = Path(command[command.index("--file") + 1])
            _docker(
                [
                    "docker",
                    "exec",
                    self.container,
                    "pg_dump",
                    "--username",
                    self.database_user,
                    "--dbname",
                    database_name,
                    "--format=custom",
                    "--file",
                    self.archive,
                ]
            )
            _docker(
                ["docker", "cp", f"{self.container}:{self.archive}", str(destination)]
            )
            return CommandResult(0)
        if command[:2] == ["pg_restore", "--list"]:
            source = Path(command[-1])
            _docker(["docker", "cp", str(source), f"{self.container}:{self.archive}"])
            completed = _docker(
                ["docker", "exec", self.container, "pg_restore", "--list", self.archive]
            )
            return CommandResult(0, completed.stdout, completed.stderr)
        if command[0] == "pg_restore":
            service = str(command[command.index("--dbname") + 1]).removeprefix(
                "service="
            )
            _dsn, database_name = self.services[service]
            _docker(
                [
                    "docker",
                    "exec",
                    self.container,
                    "pg_restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--username",
                    self.database_user,
                    "--dbname",
                    database_name,
                    self.archive,
                ]
            )
            return CommandResult(0)
        raise AssertionError(command)


def _service_file(path: Path, service_name: str) -> Path:
    path.write_text(f"[{service_name}]\nhost=disposable\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _database_fingerprint(dsn: str) -> str:
    statements = (
        "SELECT jsonb_agg(to_jsonb(r) ORDER BY recording_id)::text FROM recording AS r",
        "SELECT jsonb_agg(to_jsonb(f) ORDER BY feature_set_id)::text FROM feature_set AS f",
        "SELECT jsonb_agg(to_jsonb(d) ORDER BY request_id)::text FROM dwell_request_ingress AS d",
        "SELECT jsonb_agg(to_jsonb(j) ORDER BY job_id)::text FROM job AS j",
        "SELECT jsonb_agg(to_jsonb(m) ORDER BY name)::text FROM schema_migration AS m",
    )
    with psycopg.connect(dsn) as connection:
        values = [
            connection.execute(statement).fetchone()[0] for statement in statements
        ]
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@pytest.mark.integration
def test_0018_to_0019_backup_restore_preserves_dwell_and_restart_integrity(
    postgres_dsn: str, tmp_path: Path
) -> None:
    """Rehearse upgrade and owner/ACL-preserving restore on PostgreSQL 16."""

    suffix = uuid.uuid4().hex[:10]
    upgrade_name = f"wave7_upgrade_{suffix}"
    restore_name = f"wave7_restore_{suffix}"
    upgrade_dsn = _create_database(postgres_dsn, upgrade_name)

    migration_source = Path(__file__).resolve().parents[2] / "migrations"
    migrations_0018 = tmp_path / "migrations-0018"
    migrations_0018.mkdir()
    for path in sorted(migration_source.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        if path.name < "0019_":
            shutil.copy2(path, migrations_0018 / path.name)
    with psycopg.connect(upgrade_dsn) as connection:
        applied_0018 = apply_migrations(connection, migrations_0018)
    assert applied_0018[-1] == "0018_tracking_model_snapshot_catalog.sql"

    request = _request(upgrade_dsn, tmp_path)
    with psycopg.connect(upgrade_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM recording").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM feature_set").fetchone() == (1,)

    with psycopg.connect(upgrade_dsn) as connection:
        assert apply_migrations(connection, migration_source) == (
            "0019_dwell_request_ingress.sql",
        )

    with psycopg.connect(upgrade_dsn) as connection:
        system_identifier = connection.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()[0]
    parameters = conninfo_to_dict(postgres_dsn)
    database_user = parameters["user"]
    capture_login = f"wave7_capture_{suffix}"
    analysis_login = f"wave7_analysis_{suffix}"
    dashboard_login = f"wave7_dashboard_{suffix}"
    capture_dsn = _create_runtime_login(
        postgres_dsn,
        upgrade_name,
        capture_login,
        f"capture-{suffix}-password",
        "leo_capture",
    )
    analysis_dsn = _create_runtime_login(
        postgres_dsn,
        upgrade_name,
        analysis_login,
        f"analysis-{suffix}-password",
        "leo_analysis",
    )
    dashboard_dsn = _create_runtime_login(
        postgres_dsn,
        upgrade_name,
        dashboard_login,
        f"dashboard-{suffix}-password",
        "leo_dashboard",
    )
    qualification = QualificationConfig(
        station_id="station_ingress",
        cas=CasExpectation(tmp_path / "feature-cas", "temporary", "local", "unused"),
        migration_directory=migration_source,
        credential_names={
            "leo_capture": "capture-dsn",
            "leo_analysis": "analysis-dsn",
            "leo_dashboard": "dashboard-dsn",
            "postgres_audit": "audit-dsn",
        },
        pipeline=None,
        config_digest="sha256:disposable-wave7",
        postgres=PostgresExpectation(
            upgrade_name,
            database_user,
            16,
            system_identifier,
            REQUIRED_MIGRATION_HEAD,
            {
                "leo_capture": capture_login,
                "leo_analysis": analysis_login,
                "leo_dashboard": dashboard_login,
                "postgres_audit": database_user,
            },
        ),
    )

    class Credentials:
        def resolve(self, name: str) -> str:
            return {
                "capture-dsn": capture_dsn,
                "analysis-dsn": analysis_dsn,
                "dashboard-dsn": dashboard_dsn,
                "audit-dsn": upgrade_dsn,
            }[name]

    assert all(
        gate.passed
        for gate in inspect_database_audit(qualification, credentials=Credentials())
    )
    for role in ("leo_capture", "leo_analysis", "leo_dashboard"):
        assert all(
            gate.passed
            for gate in inspect_database_role(
                qualification, role, credentials=Credentials()
            )
        )

    ingress = PostgresDwellRequestIngress(_runtime_connect(analysis_dsn))
    publication = ingress.publish(request)
    queue = PostgresDwellRequestQueue(
        _runtime_connect(capture_dsn),
        token_factory=lambda: "lease_before_backup",
    )
    stale = queue.claim(request.station_id, request.radio_id, 0.05)
    assert stale is not None and stale.attempt == 1 and stale.lease_generation == 1
    before = _database_fingerprint(upgrade_dsn)

    port = parameters["port"]
    containers = _docker(
        ["docker", "ps", "--filter", f"publish={port}", "--format", "{{.ID}}"]
    ).stdout.splitlines()
    assert len(containers) == 1
    container = containers[0]
    restore_dsn = _create_database(postgres_dsn, restore_name)
    runner = _DockerBackupRunner(
        container,
        database_user,
        {
            "upgrade": (upgrade_dsn, upgrade_name),
            "restore": (restore_dsn, restore_name),
        },
        suffix,
    )
    manifest_path = create_backup(
        tmp_path / "backups",
        backup_id=f"wave7-{suffix}",
        created_utc_ns=time.time_ns(),
        service_name="upgrade",
        service_file=_service_file(tmp_path / "upgrade-service.conf", "upgrade"),
        runner=runner,
    )
    manifest = verify_backup(manifest_path)
    assert manifest.archive_policy == "preserve-owner-and-acl-v1"
    assert (
        restore_backup(
            manifest_path,
            service_name="restore",
            service_file=_service_file(tmp_path / "restore-service.conf", "restore"),
            runner=runner,
        )
        == manifest
    )
    dump_digest = manifest.dump_sha256
    dump_bytes = manifest.dump_byte_count

    assert _database_fingerprint(restore_dsn) == before
    features = _repository(restore_dsn, tmp_path / "feature-cas")
    with features.open(request.source.feature_set_ref) as view:
        assert view.ref == request.source.feature_set_ref

    with psycopg.connect(restore_dsn) as connection:
        owner_and_acl = connection.execute(
            """
            SELECT pg_catalog.pg_get_userbyid(p.proowner),
                   has_function_privilege(
                       'leo_capture',
                       'public.claim_dwell_request(text,text,text,interval)',
                       'EXECUTE'),
                   has_table_privilege(
                       'leo_capture', 'public.dwell_request_ingress', 'SELECT'),
                   has_function_privilege(
                       'leo_analysis',
                       'public.publish_dwell_request(jsonb)', 'EXECUTE'),
                   has_table_privilege(
                       'leo_analysis', 'public.dwell_request_ingress', 'INSERT')
              FROM pg_catalog.pg_proc AS p
             WHERE p.oid = 'public.claim_dwell_request(text,text,text,interval)'::regprocedure
            """
        ).fetchone()
    assert owner_and_acl == ("leo_routine_owner", True, False, True, False)

    time.sleep(0.06)
    restored_capture_dsn = _database_dsn(capture_dsn, restore_name)
    restored_queue = PostgresDwellRequestQueue(
        _runtime_connect(restored_capture_dsn),
        token_factory=lambda: "lease_after_restore",
    )
    restarted = restored_queue.claim(request.station_id, request.radio_id, 5)
    assert restarted is not None
    assert restarted.request == request
    assert restarted.request_digest == publication.request_digest
    assert restarted.attempt == 2
    assert restarted.lease_generation == stale.lease_generation + 1
    with pytest.raises(StaleLeaseError):
        restored_queue.complete(
            stale,
            CapturePlanRef(
                PlanId(f"plan_{request.request_id}"), Digest.sha256(b"stale")
            ),
        )

    print(
        "WAVE7_REHEARSAL_RECEIPT="
        + json.dumps(
            {
                "postgres_major": 16,
                "migration_from": "0018_tracking_model_snapshot_catalog.sql",
                "migration_to": "0019_dwell_request_ingress.sql",
                "dump_sha256": dump_digest,
                "dump_byte_count": dump_bytes,
                "database_fingerprint_sha256": before,
                "request_digest": str(publication.request_digest),
                "restored_attempt": restarted.attempt,
                "restored_lease_generation": restarted.lease_generation,
            },
            sort_keys=True,
        )
    )
