from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.contracts.core import JobId, SchemaRef
from leo_flow.deployments.offline_analysis_v1 import build_station_plugin
from leo_flow.jobs import JobPayload, JobType
from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
from leo_flow.services import assemble_service
from tests.services.test_offline_analysis_v1_deployment import (
    _Diagnostics,
    _scientific_factories,
    _station_config,
)


@pytest.mark.integration
def test_real_postgres_plugin_preflight_and_empty_cycle(
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "catalog-dsn").write_text(postgres_dsn, encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    cas_root = tmp_path / "cas"
    service = assemble_service(
        _station_config(),
        build_station_plugin(_scientific_factories(), cas_root=cas_root),
        diagnostics=_Diagnostics(),
    )

    assert not service.run_once()
    assert service.health().ready
    assert (cas_root / ".tmp").is_dir()
    assert not tuple((cas_root / ".tmp").iterdir())
    service.shutdown()
    assert service.health().state.value == "stopped"


@pytest.mark.integration
@pytest.mark.parametrize(
    "job_type",
    (JobType.RECORDING_ANALYSIS, JobType.MODEL_ANALYSIS),
)
def test_real_postgres_plugin_routes_each_lane_and_fences_bad_payload(
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    job_type: JobType,
) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "catalog-dsn").write_text(postgres_dsn, encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    jobs = PostgresJobLeaseRepository(
        lambda: psycopg.connect(postgres_dsn, row_factory=dict_row)
    )
    job_id = JobId(f"job_deployment_{job_type.value}")
    jobs.enqueue(
        job_id,
        job_type,
        JobPayload.create(SchemaRef("org.example.deliberately-wrong"), {}),
    )
    service = assemble_service(
        _station_config(),
        build_station_plugin(
            _scientific_factories(), cas_root=tmp_path / "analysis-cas"
        ),
        diagnostics=_Diagnostics(),
    )

    with pytest.raises(ValueError, match="unsupported .*analysis job schema"):
        service.run_once()
    with psycopg.connect(postgres_dsn) as connection:
        row = connection.execute(
            "SELECT state, last_error FROM job WHERE job_id = %s", (str(job_id),)
        ).fetchone()
    assert row is not None and row[0] == "failed"
    assert "failed" in row[1]
    service.shutdown()
