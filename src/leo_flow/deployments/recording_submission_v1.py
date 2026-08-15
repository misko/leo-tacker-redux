"""Analysis-side PostgreSQL operator for exact recording-job submission."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.services.bootstrap import SecretProvider
from leo_flow.services.recording_submission import SubmittedRecordingAnalysis
from leo_flow.services.recording_submission_operator import (
    RecordingAnalysisSubmissionOperator,
    RecordingSubmissionOperatorConfig,
    RecordingSubmissionOperatorError,
    load_recording_submission_config,
)

if TYPE_CHECKING:
    import psycopg


POSTGRES_TIMEOUT_S = 5
ANALYSIS_ROLE = "leo_analysis"


class RecordingSubmissionDeploymentError(RuntimeError):
    """The durable submission adapters could not be safely constructed or used."""


ConnectionFactory = Callable[[], "psycopg.Connection[dict[str, object]]"]


def submit_recording_analysis(
    config: RecordingSubmissionOperatorConfig,
    *,
    credentials: SecretProvider | None = None,
) -> SubmittedRecordingAnalysis:
    """Resolve one systemd credential and submit through analysis-role adapters."""

    try:
        dsn = (credentials or SystemdCredentialProvider()).resolve(
            config.dsn_credential_name
        )
        connect = analysis_connection_factory(dsn)

        # Server dependencies and connections are deployment concerns. Importing
        # the command parser remains safe on hosts without PostgreSQL support.
        from leo_flow.jobs.postgres_repository import PostgresJobLeaseRepository
        from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog

        operator = RecordingAnalysisSubmissionOperator(
            recordings=PostgresRecordingCatalog(connect),
            jobs=PostgresJobLeaseRepository(connect),
        )
        return operator.submit(config.selection)
    except RecordingSubmissionOperatorError:
        raise
    except Exception as error:
        raise RecordingSubmissionDeploymentError(
            "durable recording analysis submission failed"
        ) from error


def analysis_connection_factory(dsn: str) -> ConnectionFactory:
    """Build connections that prove membership and then assume only analysis role."""

    if not dsn:
        raise RecordingSubmissionDeploymentError("database DSN is empty")

    def connect() -> psycopg.Connection[dict[str, object]]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RecordingSubmissionDeploymentError(
                "recording submission requires the server dependency"
            ) from error

        connection = psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=POSTGRES_TIMEOUT_S,
            options=(
                f"-c statement_timeout={POSTGRES_TIMEOUT_S * 1000} "
                f"-c lock_timeout={POSTGRES_TIMEOUT_S * 1000}"
            ),
        )
        try:
            membership = connection.execute(
                "SELECT pg_has_role(current_user, %s, 'MEMBER') AS member",
                (ANALYSIS_ROLE,),
            ).fetchone()
            if membership is None or membership["member"] is not True:
                raise RecordingSubmissionDeploymentError(
                    "catalog credential is not a leo_analysis role member"
                )
            connection.execute("SET ROLE leo_analysis")
            return connection
        except Exception:
            connection.close()
            raise

    return connect


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(prog="leo-flow-recording-submit")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_recording_submission_config(args.config)
        submitted = submit_recording_analysis(config)
    except (RecordingSubmissionOperatorError, RecordingSubmissionDeploymentError):
        stderr.write('{"event":"recording_analysis_submission_failed"}\n')
        stderr.flush()
        return 3

    stdout.write(
        json.dumps(
            {
                "event": "recording_analysis_submitted",
                "job_id": str(submitted.job_id),
                "recording_id": str(submitted.request.recording_id),
                "request_schema_id": submitted.request.schema.schema_id,
                "request_schema_version": str(submitted.request.schema.version),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
