"""Integration-owned one-shot import of the frozen RETRO QAM recording."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.contracts.core import Digest, DigestAlgorithm, canonical_json_bytes
from leo_flow.hardware.linkage import RecordingHardwareLinker
from leo_flow.hardware.persistence import DurableHardwareMetadataRepository
from leo_flow.recording_import import (
    RetroQamImportSpecification,
    import_retro_qam_recording,
    prepare_retro_qam_recording,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.local_recording import RootedSigMFRecordingStore
from leo_flow.storage.postgres_catalog import (
    PostgresRecordingCatalog,
    PostgresRecordingPublisher,
)
from leo_flow.storage.recording_codec import (
    SigMFRecordingObjectReader,
    SigMFRecordingWriter,
)

if TYPE_CHECKING:
    import psycopg

_CAPTURE_ROLE = "leo_capture"
_ANALYSIS_ROLE = "leo_analysis"
_TIMEOUT_S = 30
_DEFAULT_CREDENTIAL_NAME = "catalog-dsn"

ConnectionFactory = Callable[[], "psycopg.Connection[dict[str, object]]"]


class RetroQamImportDeploymentError(RuntimeError):
    """One-shot adapter composition could not complete safely."""


def execute_import(args: argparse.Namespace) -> dict[str, object]:
    prepared = prepare_retro_qam_recording(
        RetroQamImportSpecification(
            args.corpus_manifest,
            args.archive_root,
            Digest(DigestAlgorithm.SHA256, args.expected_manifest_sha256),
        )
    )
    capture_dsn = SystemdCredentialProvider(args.capture_credential_directory).resolve(
        args.credential_name
    )
    analysis_dsn = SystemdCredentialProvider(
        args.analysis_credential_directory
    ).resolve(args.credential_name)
    capture_connect = _role_connection_factory(capture_dsn, _CAPTURE_ROLE)
    analysis_connect = _role_connection_factory(analysis_dsn, _ANALYSIS_ROLE)

    # Server adapters stay lazy so importing the parser has no PostgreSQL runtime
    # requirement.  This composition is the only owner of concrete persistence.
    from leo_flow.adapters.dashboard_recording_postgres import (
        PostgresRecordingCaptureDetailProjectionWriter,
        recording_capture_detail_view_v0_1,
    )
    from leo_flow.adapters.hardware_link_postgres import (
        PostgresRecordingHardwareLinkCatalog,
    )
    from leo_flow.adapters.hardware_postgres_catalog import (
        PostgresHardwareSnapshotCatalog,
    )

    args.staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    blobs = FileSystemBlobStore(args.cas_root)
    capture_catalog = PostgresRecordingCatalog(capture_connect)
    with tempfile.TemporaryDirectory(
        prefix="retro-qam-import-", dir=args.staging_root
    ) as temporary:
        temporary_root = Path(temporary)
        local = RootedSigMFRecordingStore(temporary_root)
        published, completed = import_retro_qam_recording(
            prepared,
            SigMFRecordingWriter(),
            PostgresRecordingPublisher(local, blobs, capture_catalog),
            destination=str(temporary_root / "recording"),
        )

        hardware = DurableHardwareMetadataRepository(
            blobs, PostgresHardwareSnapshotCatalog(analysis_connect)
        )
        hardware.publish(
            prepared.hardware_snapshot,
            idempotency_key=(
                f"retro-qam-hardware:{prepared.corpus_manifest_digest.value}"
            ),
        )
        linker = RecordingHardwareLinker(
            PostgresRecordingCatalog(analysis_connect),
            SigMFRecordingObjectReader(blobs),
            hardware,
            hardware,
            PostgresRecordingHardwareLinkCatalog(analysis_connect),
        )
        link = linker.link(prepared.manifest.recording_id)
        detail = recording_capture_detail_view_v0_1(
            prepared.manifest,
            published,
            analysis_state="pending",
            recording_object_available=True,
        )
        PostgresRecordingCaptureDetailProjectionWriter(capture_connect).publish(detail)
        # Cleanup is deliberately last; a failure before here leaves no public
        # ambiguity and TemporaryDirectory removes only this invocation's staging.
        local.cleanup(completed)

    recording_id = str(published.recording_id)
    base = args.dashboard_base_url.rstrip("/")
    return {
        "event": "retro_qam_recording_imported",
        "recording_id": recording_id,
        "recording_identity_digest": str(
            published.recording_object.identity_digest()
        ),
        "recording_url": f"{base}/recordings/{recording_id}",
        "corpus_manifest_digest": str(prepared.corpus_manifest_digest),
        "iq_digest": str(prepared.source_iq_digest),
        "selected_window_digest": str(prepared.selected_window_digest),
        "hardware_link_id": link.link_id,
        "historical_capture": True,
        "conditioned_canary": True,
        "calibrated_detection": False,
        "calibration_eligible": False,
    }


def _role_connection_factory(dsn: str, role: str) -> ConnectionFactory:
    if not dsn:
        raise RetroQamImportDeploymentError("catalog DSN is empty")

    def connect() -> psycopg.Connection[dict[str, object]]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RetroQamImportDeploymentError(
                "RETRO import requires the PostgreSQL server dependency"
            ) from error
        connection = psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=_TIMEOUT_S,
            options=(
                f"-c statement_timeout={_TIMEOUT_S * 1000} "
                f"-c lock_timeout={_TIMEOUT_S * 1000}"
            ),
        )
        try:
            row = connection.execute(
                "SELECT pg_has_role(current_user, %s, 'MEMBER') AS member", (role,)
            ).fetchone()
            if row is None or row["member"] is not True:
                raise RetroQamImportDeploymentError(
                    f"catalog credential is not a {role} role member"
                )
            connection.execute(f"SET ROLE {role}")
            return connection
        except Exception:
            connection.close()
            raise

    return connect


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leo-retro-qam-recording-import",
        description="Verify and publish the frozen historical RETRO QAM recording",
    )
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--cas-root", type=Path, required=True)
    parser.add_argument("--capture-credential-directory", type=Path, required=True)
    parser.add_argument("--analysis-credential-directory", type=Path, required=True)
    parser.add_argument("--credential-name", default=_DEFAULT_CREDENTIAL_NAME)
    parser.add_argument("--dashboard-base-url", default="http://gauss:8090")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    try:
        args = _parser().parse_args(argv)
        stdout.write(canonical_json_bytes(execute_import(args)).decode("utf-8") + "\n")
        return 0
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        stderr.write(
            json.dumps(
                {
                    "event": "retro_qam_recording_import_failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
