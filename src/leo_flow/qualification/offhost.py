"""Bounded, dry-run-first qualification of the off-host analysis boundary.

The default commands perform read-only filesystem and PostgreSQL inspection.
The only write command is ``write-probe``; it requires two explicit operator
arms and writes one small immutable object through the normal CAS adapter.
Nothing in this module talks to a radio, mutates PostgreSQL, or treats a file as
queue state.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import io
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, TextIO, cast

from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.analysis.recording.codec import MAX_FEATURE_SET_BYTES, decode_feature_set
from leo_flow.application.projection_writers import authoritative_identity
from leo_flow.contracts.core import (
    AnalysisRunId,
    Digest,
    DigestAlgorithm,
    FeatureSetId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.features import FeatureSetBundle, FeatureSetRef
from leo_flow.contracts.storage import ObjectRef, RecordingObjectRef
from leo_flow.jobs.contracts import JobPayload
from leo_flow.services.recording_analysis import decode_recording_analysis_payload
from leo_flow.storage.filesystem import FileSystemBlobReader, FileSystemBlobStore
from leo_flow.storage.postgres_catalog import PostgresRecordingCatalog
from leo_flow.storage.recording_codec import SigMFRecordingObjectReader

if TYPE_CHECKING:
    import psycopg


SCHEMA_ID: Final = "org.leo-flow.offhost-qualification"
SCHEMA_VERSION: Final = "0.4"
REQUIRED_MIGRATION_HEAD: Final = "0038_dashboard_surrogate_score_distributions.sql"
PROBE_FORMAT_ID: Final = "offhost-qualification-probe-v1"
PROBE_MEDIA_TYPE: Final = "application/octet-stream"
MAX_PROBE_BYTES: Final = 4096
MAX_RECORDING_DATA_BYTES: Final = 64 * 1024 * 1024
MAX_RECORDING_METADATA_BYTES: Final = 16 * 1024 * 1024
MAX_DASHBOARD_FEATURE_IDS: Final = 100_000
MAX_CONFIG_BYTES: Final = 64 * 1024
MAX_REPORT_BYTES: Final = 1024 * 1024
MAX_MIGRATION_BYTES: Final = 4 * 1024 * 1024
MAX_MOUNTINFO_BYTES: Final = 4 * 1024 * 1024
POSTGRES_TIMEOUT_S: Final = 5
_PROBE_ID = re.compile(r"^offhost_[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_MIGRATION_NAME = re.compile(r"^[0-9]{4}_[A-Za-z0-9_]+\.sql$")
_ROLES = ("leo_capture", "leo_analysis", "leo_dashboard")
_AUDIT_ROLE = "postgres_audit"
_CREDENTIAL_KEYS = (*_ROLES, _AUDIT_ROLE)
_CAPABILITY_ROLES = (
    *_ROLES,
    "leo_maintenance",
    "leo_routine_owner",
    "pg_monitor",
)
_HOST_GATE_NAMES = (
    "cas.root.exact_mountpoint",
    "cas.root.not_symlink",
    "cas.mount.source",
    "cas.mount.filesystem_type",
    "cas.mount.root",
    "cas.mount.read_write",
    "cas.group.identity",
    "cas.group.membership",
    "cas.group.setgid",
    "cas.group.permissions",
    "cas.process.readable",
    "cas.process.writable",
)
_PLACEHOLDER_MARKERS: Final = ("REPLACE_WITH_", "<REPLACE_")


class OffHostQualificationError(RuntimeError):
    """Configuration or observed infrastructure failed closed."""


class CredentialProvider(Protocol):
    def resolve(self, name: str) -> str: ...


ConnectionFactory = Callable[[], "psycopg.Connection[dict[str, object]]"]


@dataclass(frozen=True)
class CasExpectation:
    root: Path
    mount_source: str
    filesystem_type: str
    group_name: str
    mount_root: str = "/"

    def __post_init__(self) -> None:
        normalized = Path(os.path.abspath(self.root))
        if (
            not self.root.is_absolute()
            or self.root == Path("/")
            or self.root != normalized
            or not self.mount_source
            or not self.filesystem_type
            or not self.group_name
            or not self.mount_root.startswith("/")
            or "\x00" in self.mount_root
        ):
            raise OffHostQualificationError(
                "CAS root, source, filesystem type, and group must be exact"
            )


@dataclass(frozen=True)
class PostgresExpectation:
    database_name: str
    database_owner: str
    server_major: int
    system_identifier: str
    migration_head: str
    login_names: Mapping[str, str]

    def __post_init__(self) -> None:
        if (
            not self.database_name
            or not self.database_owner
            or self.server_major != 16
            or not self.system_identifier
            or not self.migration_head
            or set(self.login_names) != set(_CREDENTIAL_KEYS)
            or any(not name for name in self.login_names.values())
            or len(set(self.login_names.values())) != len(self.login_names)
        ):
            raise OffHostQualificationError(
                "PostgreSQL database, owner, major, system identifier, and migration "
                "head, and distinct login names must be explicit"
            )


@dataclass(frozen=True)
class PipelineSelection:
    recording_id: str
    job_id: str

    def __post_init__(self) -> None:
        if not self.recording_id.startswith("rec_") or not self.job_id.startswith(
            "job_"
        ):
            raise OffHostQualificationError(
                "pipeline recording_id and job_id must be explicit contract IDs"
            )


@dataclass(frozen=True)
class QualificationConfig:
    station_id: str
    cas: CasExpectation
    migration_directory: Path
    credential_names: Mapping[str, str]
    pipeline: PipelineSelection | None
    config_digest: str
    postgres: PostgresExpectation | None = None


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class MountObservation:
    root: str
    source: str
    filesystem_type: str
    mount_root: str
    device: str
    options: tuple[str, ...]
    owner_uid: int
    owner_gid: int
    mode: str
    effective_uid: int
    effective_gid: int
    supplementary_gids: tuple[int, ...]


@dataclass(frozen=True)
class HostReport:
    schema_id: str
    schema_version: str
    station_id: str
    config_digest: str
    host_role: str
    mount: MountObservation
    gates: tuple[Gate, ...]

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    def document(self) -> dict[str, object]:
        value = asdict(self)
        value["status"] = "pass" if self.passed else "fail"
        return value


@dataclass(frozen=True)
class PreflightReport:
    """Pure plan for one host; constructing it performs no external access."""

    schema_id: str
    schema_version: str
    station_id: str
    config_digest: str
    host_role: str
    required_database_roles: tuple[str, ...]
    required_credential_names: tuple[str, ...]
    cas: CasExpectation
    migration_directory: str
    pipeline: PipelineSelection | None
    postgres: PostgresExpectation | None
    gates: tuple[Gate, ...]

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    def document(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "event": "offhost_preflight",
            "mode": "dry-run",
            "status": "pass" if self.passed else "fail",
            "station_id": self.station_id,
            "config_digest": self.config_digest,
            "host_role": self.host_role,
            "required_inputs": {
                "cas": {
                    "root": str(self.cas.root),
                    "mount_source": self.cas.mount_source,
                    "filesystem_type": self.cas.filesystem_type,
                    "group_name": self.cas.group_name,
                    "mount_root": self.cas.mount_root,
                },
                "migration_directory": self.migration_directory,
                "postgres": (
                    None
                    if self.postgres is None
                    else {
                        "database_name": self.postgres.database_name,
                        "database_owner": self.postgres.database_owner,
                        "server_major": self.postgres.server_major,
                        "system_identifier": self.postgres.system_identifier,
                        "migration_head": self.postgres.migration_head,
                        "login_names": dict(self.postgres.login_names),
                    }
                ),
                "database_roles": list(self.required_database_roles),
                "systemd_credentials": list(self.required_credential_names),
                "database_connections": [
                    {
                        "role": role,
                        "systemd_credential": credential,
                        "dsn": "not-resolved",
                    }
                    for role, credential in zip(
                        self.required_database_roles,
                        self.required_credential_names[
                            : len(self.required_database_roles)
                        ],
                        strict=True,
                    )
                ]
                + (
                    []
                    if self.postgres is None
                    else [
                        {
                            "role": _AUDIT_ROLE,
                            "systemd_credential": self.required_credential_names[-1],
                            "dsn": "not-resolved",
                        }
                    ]
                ),
                "pipeline": (
                    None
                    if self.pipeline is None
                    else {
                        "recording_id": self.pipeline.recording_id,
                        "job_id": self.pipeline.job_id,
                    }
                ),
            },
            "external_access": {
                "radio": False,
                "cas": False,
                "postgresql": False,
                "credentials_resolved": False,
            },
            "gates": [asdict(gate) for gate in self.gates],
        }


@dataclass(frozen=True)
class ProbeReceipt:
    schema_id: str
    schema_version: str
    station_id: str
    config_digest: str
    writer_role: str
    probe_id: str
    object_ref: ObjectRef

    def document(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "station_id": self.station_id,
            "config_digest": self.config_digest,
            "writer_role": self.writer_role,
            "probe_id": self.probe_id,
            "object_ref": _object_document(self.object_ref),
        }


@dataclass(frozen=True)
class PipelineReport:
    recording_id: str
    job_id: str
    feature_set_id: str
    analysis_run_id: str
    feature_ids: tuple[str, ...]
    gates: tuple[Gate, ...]

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    def document(self) -> dict[str, object]:
        value = asdict(self)
        value["status"] = "pass" if self.passed else "fail"
        return value


@dataclass(frozen=True)
class _MountInfo:
    mount_point: str
    mount_root: str
    device: str
    options: tuple[str, ...]
    filesystem_type: str
    source: str


@dataclass(frozen=True)
class _PipelineRows:
    recording: RecordingObjectRef
    job_state: str
    submitted_recording: RecordingObjectRef
    feature_ref: FeatureSetRef
    feature_recording_id: str
    feature_input_recording_digest: Digest
    feature_observation_count: int
    feature_method_score_count: int
    dashboard_feature_ids: tuple[str, ...]
    dashboard_feature_identity_digests: tuple[tuple[str, str], ...]


_REQUIRED_PRIVILEGES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "leo_capture": (
        ("object_blob", "SELECT"),
        ("recording", "SELECT"),
        ("recording", "INSERT"),
        ("dashboard_recording_projection", "SELECT"),
        ("dashboard_recording_projection", "INSERT"),
        ("dashboard_activity_projection", "SELECT"),
        ("dashboard_activity_projection", "INSERT"),
        ("dashboard_capture_projection_identity", "SELECT"),
        ("dashboard_capture_projection_identity", "INSERT"),
    ),
    "leo_analysis": (
        ("object_blob", "SELECT"),
        ("recording", "SELECT"),
        ("recording_waterfall", "SELECT"),
        ("recording_starlink_detector_suite", "SELECT"),
        ("job", "SELECT"),
        ("feature_set", "SELECT"),
        ("feature_set", "INSERT"),
        ("dashboard_recording_projection", "SELECT"),
        ("dashboard_recording_projection", "INSERT"),
        ("dashboard_feature_projection", "SELECT"),
        ("dashboard_feature_projection", "INSERT"),
        ("dashboard_analysis_projection_identity", "SELECT"),
        ("dashboard_analysis_projection_identity", "INSERT"),
    ),
    "leo_dashboard": (
        ("recording", "SELECT"),
        ("feature_set", "SELECT"),
        ("dashboard_recording_projection", "SELECT"),
        ("dashboard_activity_projection", "SELECT"),
        ("dashboard_feature_projection", "SELECT"),
        ("dashboard_model_projection", "SELECT"),
        ("dashboard_track_projection", "SELECT"),
        ("dashboard_storage_health_projection", "SELECT"),
        ("dashboard_capture_batch_projection", "SELECT"),
        ("dashboard_capture_attempt_projection", "SELECT"),
        ("dashboard_recording_detail_projection", "SELECT"),
        ("dashboard_recording_waterfall_projection", "SELECT"),
        ("dashboard_recording_starlink_detector_suite_projection", "SELECT"),
    ),
}

_FORBIDDEN_PRIVILEGES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "leo_capture": (
        ("object_blob", "INSERT"),
        ("job", "SELECT"),
        ("job", "INSERT"),
        ("feature_set", "SELECT"),
        ("feature_set", "INSERT"),
        ("feature_projection_work", "SELECT"),
        ("feature_projection_work", "INSERT"),
        ("dashboard_capture_batch_projection", "SELECT"),
        ("dashboard_capture_batch_projection", "INSERT"),
        ("dashboard_capture_attempt_projection", "SELECT"),
        ("dashboard_capture_attempt_projection", "INSERT"),
        ("recording_waterfall", "SELECT"),
        ("waterfall_projection_work", "SELECT"),
        ("dashboard_recording_detail_projection", "SELECT"),
        ("dashboard_recording_waterfall_projection", "SELECT"),
        ("recording_starlink_detector_suite", "SELECT"),
        ("starlink_detector_suite_projection_work", "SELECT"),
        ("dashboard_recording_starlink_detector_suite_projection", "SELECT"),
    ),
    "leo_analysis": (
        ("object_blob", "INSERT"),
        ("recording", "INSERT"),
        ("recording", "UPDATE"),
        ("job", "INSERT"),
        ("job", "UPDATE"),
        ("feature_set", "UPDATE"),
        ("feature_set", "DELETE"),
        ("dwell_request_ingress", "SELECT"),
        ("dwell_request_ingress", "INSERT"),
        ("feature_projection_work", "SELECT"),
        ("feature_projection_work", "INSERT"),
        ("dashboard_capture_batch_projection", "SELECT"),
        ("dashboard_capture_batch_projection", "INSERT"),
        ("dashboard_capture_attempt_projection", "SELECT"),
        ("dashboard_capture_attempt_projection", "INSERT"),
        ("waterfall_projection_work", "SELECT"),
        ("waterfall_projection_work", "INSERT"),
        ("dashboard_recording_detail_projection", "SELECT"),
        ("dashboard_recording_waterfall_projection", "SELECT"),
        ("starlink_detector_suite_projection_work", "SELECT"),
        ("starlink_detector_suite_projection_work", "INSERT"),
        ("dashboard_recording_starlink_detector_suite_projection", "SELECT"),
    ),
    "leo_dashboard": (
        ("recording", "INSERT"),
        ("job", "SELECT"),
        ("job", "INSERT"),
        ("feature_set", "INSERT"),
        ("dwell_request_ingress", "SELECT"),
        ("dwell_request_ingress", "INSERT"),
        ("feature_projection_work", "SELECT"),
        ("feature_projection_work", "INSERT"),
        ("dashboard_feature_projection", "INSERT"),
        ("dashboard_capture_batch_projection", "INSERT"),
        ("dashboard_capture_attempt_projection", "INSERT"),
        ("recording_waterfall", "SELECT"),
        ("waterfall_projection_work", "SELECT"),
        ("dashboard_recording_detail_projection", "INSERT"),
        ("dashboard_recording_waterfall_projection", "INSERT"),
        ("recording_starlink_detector_suite", "SELECT"),
        ("starlink_detector_suite_projection_work", "SELECT"),
        ("dashboard_recording_starlink_detector_suite_projection", "INSERT"),
    ),
}

_REQUIRED_FUNCTION_PRIVILEGES: Final[dict[str, tuple[str, ...]]] = {
    "leo_capture": (
        "register_live_object_blob(text,text,bigint,text,text,text)",
        "claim_dwell_request(text,text,text,interval)",
        "heartbeat_dwell_request(text,text,bigint,interval)",
        "complete_dwell_request(text,text,bigint,jsonb)",
        "fail_dwell_request(text,text,bigint,text,timestamptz)",
        "park_dwell_request(text,text,bigint,text)",
        "publish_dashboard_capture_batch(jsonb)",
        "capture_analysis_drain_ready()",
        "capture_analysis_inactive()",
        "capture_campaign_analysis_safe_v1(text)",
        "capture_registered_analysis_safe_v2(text)",
        "publish_dashboard_recording_detail(jsonb)",
        "read_waterfall_analysis_receipt(text)",
    ),
    "leo_analysis": (
        "register_live_object_blob(text,text,bigint,text,text,text)",
        "enqueue_job(text,text,text,text,jsonb,timestamptz)",
        "claim_job(text[],text,interval)",
        "heartbeat_job(text,text,bigint,interval)",
        "lock_active_job_lease(text,text,text,bigint)",
        "complete_job(text,text,bigint,jsonb)",
        "fail_job(text,text,bigint,text,timestamptz)",
        "park_job(text,text,bigint,text)",
        "publish_dwell_request(jsonb)",
        "publish_feature_projection_work(text,text,text,bigint,text,text,text,text,text,text,text)",
        "claim_feature_projection_work(text,interval)",
        "heartbeat_feature_projection_work(text,text,bigint,interval)",
        "complete_feature_projection_work(text,text,bigint)",
        "retry_feature_projection_work(text,text,bigint,text,interval)",
        "park_feature_projection_work(text,text,bigint,text)",
        "publish_dashboard_capture_batch(jsonb)",
        "resolve_dashboard_capture_batches_for_recording(text)",
        "publish_recording_waterfall(text,text,text,text,text,text,text,text,text,integer,integer,text)",
        "publish_waterfall_projection_work(text,text,text,text,text,text,text)",
        "claim_waterfall_projection_work(text,interval)",
        "heartbeat_waterfall_projection_work(text,text,bigint,interval)",
        "complete_waterfall_projection_work(text,text,bigint)",
        "retry_waterfall_projection_work(text,text,bigint,text,interval)",
        "park_waterfall_projection_work(text,text,bigint,text)",
        "read_waterfall_analysis_receipt(text)",
        "publish_dashboard_recording_waterfall(jsonb)",
        "publish_recording_starlink_detector_suite(text,text,text,text,text,text,text,text,text,integer,integer,text)",
        "publish_starlink_detector_suite_projection_work(text,text,text,bigint,text,text,text,text)",
        "claim_starlink_detector_suite_projection_work(text,interval)",
        "complete_starlink_detector_suite_projection_work(text,text,bigint)",
        "retry_starlink_detector_suite_projection_work(text,text,bigint,text,interval)",
        "park_starlink_detector_suite_projection_work(text,text,bigint,text)",
        "read_starlink_detector_suite_receipt(text)",
        "publish_dashboard_recording_starlink_detector_suite(jsonb,text,text,bigint)",
        "claim_campaign_analysis_job(text[],text,text,interval)",
        "claim_campaign_feature_projection(text[],text,interval)",
        "claim_campaign_waterfall_projection(text[],text,interval)",
        "claim_campaign_starlink_suite_projection(text[],text,interval)",
        "read_campaign_analysis_lane_status(text,text[])",
        "register_campaign_analysis_window_scope_v1(text,text,integer,text[],text[],text[],text[],text[],text[])",
    ),
    "leo_dashboard": (),
}

_REQUIRED_SEQUENCE_PRIVILEGES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "leo_capture": (("dashboard_projection_sequence", "USAGE"),),
    "leo_analysis": (("dashboard_projection_sequence", "USAGE"),),
    "leo_dashboard": (),
}


def load_config(path: Path) -> QualificationConfig:
    try:
        raw = _read_bounded(path, MAX_CONFIG_BYTES, "qualification config")
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise OffHostQualificationError("qualification config is unreadable") from error
    root = _mapping(document, "config")
    _exact_keys(
        root,
        {
            "schema_id",
            "schema_version",
            "station_id",
            "cas",
            "migration_directory",
            "credential_names",
            "pipeline",
            "postgres",
        },
        "config",
    )
    if root["schema_id"] != SCHEMA_ID or root["schema_version"] != SCHEMA_VERSION:
        raise OffHostQualificationError("qualification schema is unsupported")
    cas = _mapping(root["cas"], "cas")
    _exact_keys(
        cas,
        {"root", "mount_source", "filesystem_type", "group_name", "mount_root"},
        "cas",
    )
    postgres = _mapping(root["postgres"], "postgres")
    _exact_keys(
        postgres,
        {
            "database_name",
            "database_owner",
            "server_major",
            "system_identifier",
            "migration_head",
            "login_names",
        },
        "postgres",
    )
    logins = _mapping(postgres["login_names"], "postgres.login_names")
    _exact_keys(logins, set(_CREDENTIAL_KEYS), "postgres.login_names")
    login_names = {
        role: _nonempty(logins[role], f"postgres.login_names.{role}")
        for role in _CREDENTIAL_KEYS
    }
    credentials = _mapping(root["credential_names"], "credential_names")
    _exact_keys(credentials, set(_CREDENTIAL_KEYS), "credential_names")
    credential_names = {
        role: _nonempty(credentials[role], f"credential_names.{role}")
        for role in _CREDENTIAL_KEYS
    }
    if len(set(credential_names.values())) != len(credential_names):
        raise OffHostQualificationError("database credential names must be distinct")
    pipeline_value = root["pipeline"]
    pipeline = None
    if pipeline_value is not None:
        selected = _mapping(pipeline_value, "pipeline")
        _exact_keys(selected, {"recording_id", "job_id"}, "pipeline")
        pipeline = PipelineSelection(
            _nonempty(selected["recording_id"], "pipeline.recording_id"),
            _nonempty(selected["job_id"], "pipeline.job_id"),
        )
    digest = hashlib.sha256(_canonical_json(root)).hexdigest()
    return QualificationConfig(
        station_id=_nonempty(root["station_id"], "station_id"),
        cas=CasExpectation(
            Path(_nonempty(cas["root"], "cas.root")),
            _nonempty(cas["mount_source"], "cas.mount_source"),
            _nonempty(cas["filesystem_type"], "cas.filesystem_type"),
            _nonempty(cas["group_name"], "cas.group_name"),
            _nonempty(cas["mount_root"], "cas.mount_root"),
        ),
        migration_directory=Path(
            _nonempty(root["migration_directory"], "migration_directory")
        ),
        credential_names=credential_names,
        pipeline=pipeline,
        config_digest=f"sha256:{digest}",
        postgres=PostgresExpectation(
            _nonempty(postgres["database_name"], "postgres.database_name"),
            _nonempty(postgres["database_owner"], "postgres.database_owner"),
            _integer(postgres["server_major"], "postgres.server_major"),
            _nonempty(postgres["system_identifier"], "postgres.system_identifier"),
            _nonempty(postgres["migration_head"], "postgres.migration_head"),
            login_names,
        ),
    )


def build_preflight_report(
    config: QualificationConfig, host_role: str
) -> PreflightReport:
    """Describe exact host inputs without inspecting or resolving any of them."""

    roles = _database_roles_for_host(host_role)
    credential_names = tuple(config.credential_names[role] for role in roles) + (
        config.credential_names[_AUDIT_ROLE],
    )
    values = {
        "station_id": config.station_id,
        "cas.root": str(config.cas.root),
        "cas.mount_source": config.cas.mount_source,
        "cas.filesystem_type": config.cas.filesystem_type,
        "cas.group_name": config.cas.group_name,
        "cas.mount_root": config.cas.mount_root,
        "migration_directory": str(config.migration_directory),
        **{
            f"credential_names.{role}": config.credential_names[role]
            for role in _CREDENTIAL_KEYS
        },
    }
    if config.postgres is not None:
        values.update(
            {
                "postgres.database_name": config.postgres.database_name,
                "postgres.database_owner": config.postgres.database_owner,
                "postgres.system_identifier": config.postgres.system_identifier,
                "postgres.migration_head": config.postgres.migration_head,
                **{
                    f"postgres.login_names.{role}": login
                    for role, login in config.postgres.login_names.items()
                },
            }
        )
    if config.pipeline is not None:
        values.update(
            {
                "pipeline.recording_id": config.pipeline.recording_id,
                "pipeline.job_id": config.pipeline.job_id,
            }
        )
    unresolved = tuple(
        sorted(
            name
            for name, value in values.items()
            if any(marker in value.upper() for marker in _PLACEHOLDER_MARKERS)
        )
    )
    migration_directory = config.migration_directory
    migration_is_exact = (
        migration_directory.is_absolute()
        and migration_directory != Path("/")
        and migration_directory == Path(os.path.abspath(migration_directory))
    )
    unsafe_credentials = tuple(
        f"{role}:{name}"
        for role, name in config.credential_names.items()
        if not name or name in {".", ".."} or "/" in name or "\x00" in name
    )
    postgres_exact = (
        config.postgres is not None
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", config.postgres.database_name)
        is not None
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", config.postgres.database_owner)
        is not None
        and re.fullmatch(r"[0-9]{10,20}", config.postgres.system_identifier) is not None
        and config.postgres.migration_head == REQUIRED_MIGRATION_HEAD
        and all(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", login) is not None
            for login in config.postgres.login_names.values()
        )
    )
    gates = (
        Gate(
            "preflight.inputs.resolved",
            not unresolved,
            ",".join(unresolved) or "complete",
        ),
        Gate(
            "preflight.migration_directory.exact",
            migration_is_exact,
            str(migration_directory),
        ),
        Gate(
            "preflight.credential_names.safe",
            not unsafe_credentials,
            ",".join(unsafe_credentials) or "complete",
        ),
        Gate(
            "preflight.postgres.identity.exact",
            postgres_exact,
            (
                "missing"
                if config.postgres is None
                else (
                    f"database={config.postgres.database_name};"
                    f"owner={config.postgres.database_owner};"
                    f"major={config.postgres.server_major};"
                    f"system_identifier={config.postgres.system_identifier};"
                    f"migration_head={config.postgres.migration_head}"
                )
            ),
        ),
        Gate(
            "preflight.external_access.disabled",
            True,
            "radio=false;cas=false;postgresql=false;credentials_resolved=false",
        ),
    )
    return PreflightReport(
        SCHEMA_ID,
        SCHEMA_VERSION,
        config.station_id,
        config.config_digest,
        host_role,
        roles,
        credential_names,
        config.cas,
        str(config.migration_directory),
        config.pipeline,
        config.postgres,
        gates,
    )


def inspect_host(
    config: QualificationConfig,
    host_role: str,
    *,
    mountinfo: str | None = None,
    stat_result: os.stat_result | None = None,
    access: Callable[[Path, int], bool] = os.access,
    group: grp.struct_group | None = None,
    database_gates: Sequence[Gate] = (),
) -> HostReport:
    """Inspect one host without creating, changing, or deleting anything."""

    if host_role not in ("capture", "analysis", "dashboard"):
        raise OffHostQualificationError("host role is unsupported")
    root = config.cas.root
    text = (
        _read_bounded(
            Path("/proc/self/mountinfo"), MAX_MOUNTINFO_BYTES, "mountinfo"
        ).decode()
        if mountinfo is None
        else mountinfo
    )
    mount = _find_exact_mount(root, text)
    observed_stat = root.stat() if stat_result is None else stat_result
    expected_group = grp.getgrnam(config.cas.group_name) if group is None else group
    gids = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    observation = MountObservation(
        root=str(root),
        source=mount.source,
        filesystem_type=mount.filesystem_type,
        mount_root=mount.mount_root,
        device=mount.device,
        options=mount.options,
        owner_uid=observed_stat.st_uid,
        owner_gid=observed_stat.st_gid,
        mode=oct(stat.S_IMODE(observed_stat.st_mode)),
        effective_uid=os.geteuid(),
        effective_gid=os.getegid(),
        supplementary_gids=gids,
    )
    should_write = host_role in ("capture", "analysis")
    gates = (
        Gate(
            "cas.root.exact_mountpoint",
            mount.mount_point == str(root),
            mount.mount_point,
        ),
        Gate("cas.root.not_symlink", not root.is_symlink(), str(root)),
        Gate(
            "cas.mount.source",
            mount.source == config.cas.mount_source,
            mount.source,
        ),
        Gate(
            "cas.mount.filesystem_type",
            mount.filesystem_type == config.cas.filesystem_type,
            mount.filesystem_type,
        ),
        Gate(
            "cas.mount.root",
            mount.mount_root == config.cas.mount_root,
            mount.mount_root,
        ),
        Gate("cas.mount.read_write", "rw" in mount.options, ",".join(mount.options)),
        Gate(
            "cas.group.identity",
            observed_stat.st_gid == expected_group.gr_gid,
            f"configured={expected_group.gr_gid},observed={observed_stat.st_gid}",
        ),
        Gate(
            "cas.group.membership",
            expected_group.gr_gid in gids,
            f"gid={expected_group.gr_gid}",
        ),
        Gate(
            "cas.group.setgid",
            bool(observed_stat.st_mode & stat.S_ISGID),
            observation.mode,
        ),
        Gate(
            "cas.group.permissions",
            (stat.S_IMODE(observed_stat.st_mode) & stat.S_IRWXG) == stat.S_IRWXG,
            observation.mode,
        ),
        Gate("cas.process.readable", access(root, os.R_OK | os.X_OK), str(root)),
        Gate(
            "cas.process.writable",
            (not should_write) or access(root, os.W_OK | os.X_OK),
            "required" if should_write else "not-required",
        ),
        *tuple(database_gates),
    )
    return HostReport(
        SCHEMA_ID,
        SCHEMA_VERSION,
        config.station_id,
        config.config_digest,
        host_role,
        observation,
        gates,
    )


def compare_host_reports(
    config: QualificationConfig, capture: HostReport, analysis: HostReport
) -> tuple[Gate, ...]:
    """Compare signed-out host observations; reports are evidence, never signals."""

    return (
        Gate(
            "reports.schema",
            capture.schema_id == SCHEMA_ID
            and capture.schema_version == SCHEMA_VERSION
            and analysis.schema_id == SCHEMA_ID
            and analysis.schema_version == SCHEMA_VERSION,
            f"{capture.schema_id}/{capture.schema_version}",
        ),
        Gate(
            "reports.roles",
            capture.host_role == "capture" and analysis.host_role == "analysis",
            f"{capture.host_role},{analysis.host_role}",
        ),
        Gate(
            "reports.station",
            capture.station_id == analysis.station_id == config.station_id,
            capture.station_id,
        ),
        Gate(
            "reports.config_digest",
            capture.config_digest == analysis.config_digest == config.config_digest,
            capture.config_digest,
        ),
        Gate(
            "reports.cas_root",
            capture.mount.root == analysis.mount.root == str(config.cas.root),
            capture.mount.root,
        ),
        Gate(
            "reports.mount_source",
            capture.mount.source == analysis.mount.source == config.cas.mount_source,
            capture.mount.source,
        ),
        Gate(
            "reports.filesystem_type",
            capture.mount.filesystem_type
            == analysis.mount.filesystem_type
            == config.cas.filesystem_type,
            capture.mount.filesystem_type,
        ),
        Gate(
            "reports.mount_root",
            capture.mount.mount_root == analysis.mount.mount_root,
            capture.mount.mount_root,
        ),
        Gate("reports.capture_passed", capture.passed, "capture"),
        Gate("reports.analysis_passed", analysis.passed, "analysis"),
    )


def expected_migration_receipts(directory: Path) -> dict[str, str]:
    if not directory.is_absolute() or not directory.is_dir():
        raise OffHostQualificationError(
            "migration_directory must be an absolute directory"
        )
    receipts: dict[str, str] = {}
    for path in sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        if not _MIGRATION_NAME.fullmatch(path.name):
            continue
        raw = _read_bounded(path, MAX_MIGRATION_BYTES, f"migration {path.name}")
        receipts[path.name] = hashlib.sha256(raw).hexdigest()
    if not receipts:
        raise OffHostQualificationError("migration_directory has no ordered migrations")
    return receipts


def inspect_database_audit(
    config: QualificationConfig,
    *,
    credentials: CredentialProvider | None = None,
) -> tuple[Gate, ...]:
    """Prove cluster identity and the exact migration inventory via audit access."""

    if config.postgres is None:
        raise OffHostQualificationError("PostgreSQL identity expectation is required")
    dsn = (credentials or SystemdCredentialProvider()).resolve(
        config.credential_names[_AUDIT_ROLE]
    )
    expected = expected_migration_receipts(config.migration_directory)
    with _connection_factory(dsn)() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL search_path = pg_catalog, public")
        cluster = connection.execute(
            """
            SELECT current_database() AS database_name,
                   pg_catalog.pg_get_userbyid(d.datdba) AS database_owner,
                   current_setting('server_version_num') AS server_version_num,
                   c.system_identifier::text AS system_identifier,
                   current_user AS current_user_name,
                   session_user AS session_user_name
              FROM pg_catalog.pg_database AS d
              CROSS JOIN pg_catalog.pg_control_system() AS c
             WHERE d.datname = current_database()
            """
        ).fetchone()
        read_only_row = connection.execute("SHOW transaction_read_only").fetchone()
        rows = connection.execute(
            """
            SELECT name, sha256
              FROM public.schema_migration
             ORDER BY name
             LIMIT %s
            """,
            (len(expected) + 1,),
        ).fetchall()
    actual = {str(row["name"]): str(row["sha256"]) for row in rows}
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        name
        for name, digest in expected.items()
        if actual.get(name) not in (None, digest)
    )
    expected_login = config.postgres.login_names[_AUDIT_ROLE]
    login_matches = (
        cluster is not None
        and cluster["current_user_name"] == expected_login
        and cluster["session_user_name"] == expected_login
    )
    cluster_matches = (
        cluster is not None
        and cluster["database_name"] == config.postgres.database_name
        and cluster["database_owner"] == config.postgres.database_owner
        and int(str(cluster["server_version_num"])) // 10_000
        == config.postgres.server_major
        and cluster["system_identifier"] == config.postgres.system_identifier
    )
    inventory_matches = (
        not missing
        and not extra
        and not changed
        and tuple(actual) == tuple(expected)
        and config.postgres.migration_head == REQUIRED_MIGRATION_HEAD
        and tuple(expected)[-1] == config.postgres.migration_head
    )
    return (
        Gate(
            "postgres.audit.read_only",
            read_only_row is not None
            and read_only_row["transaction_read_only"] == "on",
            "transaction_read_only",
        ),
        Gate(
            "postgres.audit.session_login",
            login_matches,
            "missing"
            if cluster is None
            else (
                f"expected={expected_login};current={cluster['current_user_name']};"
                f"session={cluster['session_user_name']}"
            ),
        ),
        Gate(
            "postgres.audit.cluster_identity",
            cluster_matches,
            "missing"
            if cluster is None
            else (
                f"database={cluster['database_name']};"
                f"owner={cluster['database_owner']};"
                f"server_version_num={cluster['server_version_num']};"
                f"system_identifier={cluster['system_identifier']}"
            ),
        ),
        Gate(
            "postgres.audit.migration_receipts",
            inventory_matches,
            (
                f"head={config.postgres.migration_head};"
                f"missing={','.join(missing) or '-'};"
                f"extra={','.join(extra) or '-'};"
                f"changed={','.join(changed) or '-'}"
            ),
        ),
    )


def inspect_database_role(
    config: QualificationConfig,
    role: str,
    *,
    credentials: CredentialProvider | None = None,
) -> tuple[Gate, ...]:
    """Prove one exact runtime login and its least-privilege capability role."""

    if role not in _ROLES:
        raise OffHostQualificationError("database role is unsupported")
    if config.postgres is None:
        raise OffHostQualificationError("PostgreSQL identity expectation is required")
    dsn = (credentials or SystemdCredentialProvider()).resolve(
        config.credential_names[role]
    )
    expected_login = config.postgres.login_names[role]
    with _connection_factory(dsn)() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL search_path = pg_catalog, public")
        read_only_row = connection.execute("SHOW transaction_read_only").fetchone()
        login = connection.execute(
            """
            SELECT current_user AS current_user_name,
                   session_user AS session_user_name,
                   r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole,
                   r.rolreplication, r.rolbypassrls
              FROM pg_catalog.pg_roles AS r
             WHERE r.rolname = session_user
            """
        ).fetchone()
        memberships = {}
        for capability in _CAPABILITY_ROLES:
            membership = connection.execute(
                "SELECT pg_has_role(session_user, %s, 'MEMBER') AS member",
                (capability,),
            ).fetchone()
            memberships[capability] = (
                membership is not None and membership["member"] is True
            )
        direct = connection.execute(
            """
            WITH login AS (
                SELECT oid FROM pg_catalog.pg_roles WHERE rolname = session_user
            ), direct_acl AS (
                SELECT a.grantee FROM pg_catalog.pg_database AS d
                CROSS JOIN LATERAL pg_catalog.aclexplode(d.datacl) AS a
                UNION ALL
                SELECT a.grantee FROM pg_catalog.pg_namespace AS n
                CROSS JOIN LATERAL pg_catalog.aclexplode(n.nspacl) AS a
                UNION ALL
                SELECT a.grantee FROM pg_catalog.pg_class AS c
                CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) AS a
                UNION ALL
                SELECT a.grantee FROM pg_catalog.pg_proc AS p
                CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) AS a
            ), owned AS (
                SELECT d.datdba AS owner FROM pg_catalog.pg_database AS d
                 WHERE d.datname = current_database()
                UNION ALL
                SELECT n.nspowner FROM pg_catalog.pg_namespace AS n
                 WHERE n.nspname NOT LIKE 'pg_temp_%'
                   AND n.nspname NOT LIKE 'pg_toast_temp_%'
                UNION ALL
                SELECT c.relowner FROM pg_catalog.pg_class AS c
                UNION ALL
                SELECT p.proowner FROM pg_catalog.pg_proc AS p
            )
            SELECT (SELECT count(*) FROM direct_acl, login
                     WHERE direct_acl.grantee = login.oid) AS direct_acl_count,
                   (SELECT count(*) FROM owned, login
                     WHERE owned.owner = login.oid) AS owned_object_count
            """
        ).fetchone()
        member = memberships[role]
        if member:
            connection.execute(f"SET ROLE {role}")
        assumed = connection.execute(
            "SELECT current_user AS role, session_user AS login"
        ).fetchone()
        privileges = {
            (table, privilege): _table_privilege(connection, table, privilege)
            for table, privilege in (
                _REQUIRED_PRIVILEGES[role] + _FORBIDDEN_PRIVILEGES[role]
            )
        }
        functions = {
            function: _function_privilege(connection, function)
            for function in _REQUIRED_FUNCTION_PRIVILEGES[role]
        }
        sequences = {
            (sequence, privilege): _sequence_privilege(connection, sequence, privilege)
            for sequence, privilege in _REQUIRED_SEQUENCE_PRIVILEGES[role]
        }
    login_matches = (
        login is not None
        and login["current_user_name"] == expected_login
        and login["session_user_name"] == expected_login
        and login["rolcanlogin"] is True
        and not any(
            bool(login[field])
            for field in (
                "rolsuper",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
            )
        )
    )
    membership_exact = member and sum(memberships.values()) == 1
    direct_access_absent = (
        direct is not None
        and direct["direct_acl_count"] == 0
        and direct["owned_object_count"] == 0
    )
    required_missing = sorted(
        f"{table}:{privilege}"
        for table, privilege in _REQUIRED_PRIVILEGES[role]
        if not privileges[(table, privilege)]
    )
    forbidden_present = sorted(
        f"{table}:{privilege}"
        for table, privilege in _FORBIDDEN_PRIVILEGES[role]
        if privileges[(table, privilege)]
    )
    functions_missing = sorted(
        function for function, allowed in functions.items() if not allowed
    )
    sequences_missing = sorted(
        f"{sequence}:{privilege}"
        for (sequence, privilege), allowed in sequences.items()
        if not allowed
    )
    return (
        Gate(
            f"postgres.{role}.read_only",
            read_only_row is not None
            and read_only_row["transaction_read_only"] == "on",
            "transaction_read_only",
        ),
        Gate(
            f"postgres.{role}.session_login",
            login_matches,
            "missing"
            if login is None
            else (
                f"expected={expected_login};current={login['current_user_name']};"
                f"session={login['session_user_name']}"
            ),
        ),
        Gate(
            f"postgres.{role}.membership",
            membership_exact,
            ",".join(
                capability for capability, present in memberships.items() if present
            )
            or "none",
        ),
        Gate(
            f"postgres.{role}.direct_authority",
            direct_access_absent,
            "missing"
            if direct is None
            else (
                f"direct_acl_count={direct['direct_acl_count']};"
                f"owned_object_count={direct['owned_object_count']}"
            ),
        ),
        Gate(
            f"postgres.{role}.assumed_role",
            assumed is not None
            and assumed["role"] == role
            and assumed["login"] == expected_login,
            role,
        ),
        Gate(
            f"postgres.{role}.required_privileges",
            not required_missing,
            ",".join(required_missing) or "complete",
        ),
        Gate(
            f"postgres.{role}.forbidden_privileges",
            not forbidden_present,
            ",".join(forbidden_present) or "absent",
        ),
        Gate(
            f"postgres.{role}.required_functions",
            not functions_missing,
            ",".join(functions_missing) or "complete",
        ),
        Gate(
            f"postgres.{role}.required_sequences",
            not sequences_missing,
            ",".join(sequences_missing) or "complete",
        ),
    )


def verify_pipeline(
    config: QualificationConfig,
    *,
    credentials: CredentialProvider | None = None,
) -> PipelineReport:
    """Verify one exact already-published pipeline without mutating it."""

    if config.pipeline is None:
        raise OffHostQualificationError("pipeline selection is required")
    provider = credentials or SystemdCredentialProvider()
    rows = _load_pipeline_rows(config, provider)
    if not 0 < rows.recording.data_object.byte_count <= MAX_RECORDING_DATA_BYTES:
        raise OffHostQualificationError("recording data exceeds qualification bound")
    if not (
        0 < rows.recording.metadata_object.byte_count <= MAX_RECORDING_METADATA_BYTES
    ):
        raise OffHostQualificationError(
            "recording metadata exceeds qualification bound"
        )
    if not 0 < rows.feature_ref.bundle_ref.byte_count <= MAX_FEATURE_SET_BYTES:
        raise OffHostQualificationError("FeatureSet exceeds qualification bound")
    blobs = FileSystemBlobReader(config.cas.root)

    remote_reader = SigMFRecordingObjectReader(blobs)
    with remote_reader.open(rows.recording) as recording_view:
        remote_recording_id = str(recording_view.manifest.recording_id)
    with blobs.open(rows.feature_ref.bundle_ref) as stream:
        bundle = decode_feature_set(stream.read(MAX_FEATURE_SET_BYTES + 1))
    return evaluate_pipeline(config.pipeline, rows, remote_recording_id, bundle)


def evaluate_pipeline(
    selection: PipelineSelection,
    rows: _PipelineRows,
    remote_recording_id: str,
    bundle: FeatureSetBundle,
) -> PipelineReport:
    """Evaluate exact identities after adapters have read their authorities."""

    feature_ids = tuple(str(item.feature_id) for item in bundle.observations)
    recording_identity = rows.recording.identity_digest()
    projection_identity = authoritative_identity(
        "feature",
        "unused",
        {
            "feature_set_id": str(rows.feature_ref.feature_set_id),
            "analysis_run_id": str(rows.feature_ref.analysis_run_id),
            "bundle_ref": {
                "digest": str(rows.feature_ref.bundle_ref.digest),
                "byte_count": rows.feature_ref.bundle_ref.byte_count,
                "media_type": rows.feature_ref.bundle_ref.media_type,
                "format_id": rows.feature_ref.bundle_ref.format_id,
            },
        },
    )
    dashboard_identities = dict(rows.dashboard_feature_identity_digests)
    gates = (
        Gate(
            "pipeline.capture_publication",
            str(rows.recording.recording_id) == selection.recording_id,
            selection.recording_id,
        ),
        Gate(
            "pipeline.analysis_submission",
            rows.submitted_recording == rows.recording,
            str(rows.submitted_recording.recording_id),
        ),
        Gate("pipeline.job_succeeded", rows.job_state == "succeeded", rows.job_state),
        Gate(
            "pipeline.remote_recording_reader",
            remote_recording_id == selection.recording_id,
            remote_recording_id,
        ),
        Gate(
            "pipeline.feature_recording",
            rows.feature_recording_id == selection.recording_id
            and str(bundle.recording_id) == selection.recording_id,
            rows.feature_recording_id,
        ),
        Gate(
            "pipeline.feature_input_identity",
            rows.feature_input_recording_digest
            == bundle.input_recording_identity_digest
            == recording_identity,
            str(rows.feature_input_recording_digest),
        ),
        Gate(
            "pipeline.feature_identity",
            str(bundle.feature_set_id) == str(rows.feature_ref.feature_set_id)
            and str(bundle.analysis_run_id) == str(rows.feature_ref.analysis_run_id),
            str(rows.feature_ref.feature_set_id),
        ),
        Gate(
            "pipeline.feature_counts",
            rows.feature_observation_count == len(bundle.observations)
            and rows.feature_method_score_count == len(bundle.method_scores),
            (
                f"catalog={rows.feature_observation_count}/"
                f"{rows.feature_method_score_count};bundle="
                f"{len(bundle.observations)}/{len(bundle.method_scores)}"
            ),
        ),
        Gate(
            "pipeline.dashboard_reference",
            len(feature_ids) == len(set(feature_ids))
            and set(rows.dashboard_feature_ids) == set(feature_ids),
            f"projected={len(rows.dashboard_feature_ids)};bundle={len(feature_ids)}",
        ),
        Gate(
            "pipeline.dashboard_identity",
            set(dashboard_identities) == set(feature_ids)
            and all(
                digest == projection_identity.digest
                for digest in dashboard_identities.values()
            ),
            f"identified={len(dashboard_identities)};bundle={len(feature_ids)}",
        ),
    )
    return PipelineReport(
        selection.recording_id,
        selection.job_id,
        str(rows.feature_ref.feature_set_id),
        str(rows.feature_ref.analysis_run_id),
        feature_ids,
        gates,
    )


def write_probe(
    config: QualificationConfig,
    *,
    writer_role: str,
    probe_id: str,
    arm_writes: bool,
    confirmed_cas_root: Path,
) -> ProbeReceipt:
    """Write exactly one bounded immutable CAS object after dual arming."""

    if writer_role not in ("capture", "analysis"):
        raise OffHostQualificationError("probe writer must be capture or analysis")
    if not arm_writes:
        raise OffHostQualificationError("CAS probe writes are not armed")
    if confirmed_cas_root != config.cas.root:
        raise OffHostQualificationError("confirmed CAS root differs from config")
    if not _PROBE_ID.fullmatch(probe_id):
        raise OffHostQualificationError("probe_id is invalid")
    payload = _probe_payload(config, writer_role, probe_id)
    if len(payload) > MAX_PROBE_BYTES:  # defensive: inputs above are bounded
        raise OffHostQualificationError("probe payload exceeds its hard limit")
    _require_writable_mount(config, writer_role)
    store = FileSystemBlobStore(config.cas.root)
    ref = store.put(
        io.BytesIO(payload),
        expected_digest=Digest.sha256(payload),
        expected_bytes=len(payload),
        media_type=PROBE_MEDIA_TYPE,
        format_id=PROBE_FORMAT_ID,
        idempotency_key=f"offhost-qualification:{writer_role}:{probe_id}",
    )
    return ProbeReceipt(
        SCHEMA_ID,
        SCHEMA_VERSION,
        config.station_id,
        config.config_digest,
        writer_role,
        probe_id,
        ref,
    )


def read_probe(
    config: QualificationConfig,
    receipt: ProbeReceipt,
    *,
    reader_role: str,
) -> tuple[Gate, ...]:
    """Read and hash an exact CAS reference; no paths or discovery are accepted."""

    if reader_role not in ("capture", "analysis"):
        raise OffHostQualificationError("probe reader must be capture or analysis")
    if receipt.writer_role not in ("capture", "analysis") or not _PROBE_ID.fullmatch(
        receipt.probe_id
    ):
        raise OffHostQualificationError("probe receipt identity is invalid")
    if not 0 < receipt.object_ref.byte_count <= MAX_PROBE_BYTES:
        raise OffHostQualificationError("probe receipt exceeds its hard limit")
    store = FileSystemBlobReader(config.cas.root)
    with store.open(receipt.object_ref) as stream:
        payload = stream.read(MAX_PROBE_BYTES + 1)
    expected = _probe_payload(config, receipt.writer_role, receipt.probe_id)
    return (
        Gate(
            "probe.schema",
            receipt.schema_id == SCHEMA_ID and receipt.schema_version == SCHEMA_VERSION,
            f"{receipt.schema_id}/{receipt.schema_version}",
        ),
        Gate(
            "probe.object_contract",
            receipt.object_ref.media_type == PROBE_MEDIA_TYPE
            and receipt.object_ref.format_id == PROBE_FORMAT_ID,
            receipt.object_ref.format_id,
        ),
        Gate(
            "probe.opposite_role",
            reader_role != receipt.writer_role,
            f"writer={receipt.writer_role};reader={reader_role}",
        ),
        Gate(
            "probe.config_digest",
            receipt.config_digest == config.config_digest,
            receipt.config_digest,
        ),
        Gate(
            "probe.station",
            receipt.station_id == config.station_id,
            receipt.station_id,
        ),
        Gate("probe.payload", payload == expected, str(receipt.object_ref.digest)),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(prog="leo-flow-offhost-qualification")
    parser.add_argument("--config", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument(
        "--host-role", choices=("capture", "analysis", "dashboard"), required=True
    )

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument(
        "--host-role", choices=("capture", "analysis", "dashboard"), required=True
    )

    compare = subparsers.add_parser("compare-hosts")
    compare.add_argument("--capture-report", required=True, type=Path)
    compare.add_argument("--analysis-report", required=True, type=Path)

    subparsers.add_parser("verify-pipeline")

    probe = subparsers.add_parser("write-probe")
    probe.add_argument("--host-role", choices=("capture", "analysis"), required=True)
    probe.add_argument("--probe-id", required=True)
    probe.add_argument("--arm-writes", action="store_true")
    probe.add_argument("--confirm-cas-root", required=True, type=Path)

    read = subparsers.add_parser("read-probe")
    read.add_argument("--host-role", choices=("capture", "analysis"), required=True)
    read.add_argument("--probe-receipt", required=True, type=Path)

    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "preflight":
            preflight_report = build_preflight_report(config, args.host_role)
            document = preflight_report.document()
            code = 0 if preflight_report.passed else 2
        elif args.command == "inspect":
            roles = _database_roles_for_host(args.host_role)
            database_gates = inspect_database_audit(config) + tuple(
                gate for role in roles for gate in inspect_database_role(config, role)
            )
            host_report = inspect_host(
                config, args.host_role, database_gates=database_gates
            )
            document = host_report.document()
            code = 0 if host_report.passed else 2
        elif args.command == "compare-hosts":
            capture = _load_host_report(args.capture_report)
            analysis = _load_host_report(args.analysis_report)
            gates = compare_host_reports(config, capture, analysis)
            passed = all(gate.passed for gate in gates)
            document = {
                "schema_id": SCHEMA_ID,
                "schema_version": SCHEMA_VERSION,
                "event": "offhost_reports_compared",
                "status": "pass" if passed else "fail",
                "gates": [asdict(gate) for gate in gates],
            }
            code = 0 if passed else 2
        elif args.command == "verify-pipeline":
            pipeline_report = verify_pipeline(config)
            document = pipeline_report.document()
            code = 0 if pipeline_report.passed else 2
        elif args.command == "write-probe":
            receipt = write_probe(
                config,
                writer_role=args.host_role,
                probe_id=args.probe_id,
                arm_writes=args.arm_writes,
                confirmed_cas_root=args.confirm_cas_root,
            )
            document = receipt.document()
            code = 0
        else:
            receipt = _load_probe_receipt(args.probe_receipt)
            gates = read_probe(config, receipt, reader_role=args.host_role)
            passed = all(gate.passed for gate in gates)
            document = {
                "schema_id": SCHEMA_ID,
                "schema_version": SCHEMA_VERSION,
                "event": "offhost_probe_read",
                "status": "pass" if passed else "fail",
                "gates": [asdict(gate) for gate in gates],
            }
            code = 0 if passed else 2
    except Exception:  # noqa: BLE001 - CLI must never print credential/driver errors.
        stderr.write('{"event":"offhost_qualification_failed"}\n')
        stderr.flush()
        return 3
    stdout.write(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    stdout.flush()
    return code


def _load_pipeline_rows(
    config: QualificationConfig, credentials: CredentialProvider
) -> _PipelineRows:
    assert config.pipeline is not None
    analysis = _connection_factory(
        credentials.resolve(config.credential_names["leo_analysis"])
    )
    dashboard = _connection_factory(
        credentials.resolve(config.credential_names["leo_dashboard"])
    )
    with analysis() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL search_path = pg_catalog, public")
        connection.execute("SET ROLE leo_analysis")
        with connection.cursor() as cursor:
            recording = PostgresRecordingCatalog.get_with_cursor(
                cursor, config.pipeline.recording_id
            )
        if recording is None:
            raise OffHostQualificationError("selected recording is not published")
        job = connection.execute(
            """
            SELECT state, payload_schema_id, payload_schema_version,
                   payload, result_ref
            FROM job
            WHERE job_id = %s AND job_type = 'recording_analysis'
            """,
            (config.pipeline.job_id,),
        ).fetchone()
        if job is None:
            raise OffHostQualificationError("selected recording-analysis job is absent")
        payload = _mapping(job["payload"], "job.payload")
        result = _mapping(job["result_ref"], "job.result_ref")
        request = decode_recording_analysis_payload(
            JobPayload.create(
                SchemaRef(
                    _nonempty(job["payload_schema_id"], "job.payload_schema_id"),
                    SchemaVersion.parse(
                        _nonempty(
                            job["payload_schema_version"],
                            "job.payload_schema_version",
                        )
                    ),
                ),
                payload,
            )
        )
        submitted_recording = request.recording_object_ref
        if (
            result.get("schema_id") != FeatureSetBundle.SCHEMA_ID
            or result.get("schema_version") != SCHEMA_VERSION
        ):
            raise OffHostQualificationError(
                "job result does not declare the exact FeatureSet schema"
            )
        bundle_digest = Digest(
            DigestAlgorithm(
                _nonempty(result.get("digest_algorithm"), "result.digest_algorithm")
            ),
            _nonempty(result.get("digest_value"), "result.digest_value"),
        )
        feature = connection.execute(
            """
            SELECT feature_set_id, analysis_run_id, recording_id,
                   input_recording_digest_algorithm,
                   input_recording_digest_value,
                   observation_count, method_score_count,
                   bundle_digest_algorithm, bundle_digest_value,
                   object_blob.byte_count, object_blob.media_type,
                   object_blob.format_id, object_blob.locator
            FROM feature_set
            JOIN object_blob
              ON object_blob.digest_algorithm = feature_set.bundle_digest_algorithm
             AND object_blob.digest_value = feature_set.bundle_digest_value
            WHERE feature_set_id = %s
              AND bundle_digest_algorithm = %s
              AND bundle_digest_value = %s
            """,
            (
                _nonempty(result.get("artifact_id"), "result.artifact_id"),
                bundle_digest.algorithm.value,
                bundle_digest.value,
            ),
        ).fetchone()
        if feature is None:
            raise OffHostQualificationError("job result has no exact FeatureSet")
        feature_ref = FeatureSetRef(
            _feature_set_id(str(feature["feature_set_id"])),
            _analysis_run_id(str(feature["analysis_run_id"])),
            ObjectRef(
                bundle_digest,
                _integer(feature["byte_count"], "feature.byte_count"),
                str(feature["media_type"]),
                str(feature["format_id"]),
                str(feature["locator"]),
            ),
        )
        identified = connection.execute(
            """
            SELECT logical_id, authoritative_identity_digest
            FROM dashboard_analysis_projection_identity
            WHERE projection_kind = 'feature'
              AND logical_id IN (
                  SELECT feature_id
                  FROM dashboard_feature_projection
                  WHERE recording_id = %s
              )
            ORDER BY logical_id
            LIMIT %s
            """,
            (config.pipeline.recording_id, MAX_DASHBOARD_FEATURE_IDS + 1),
        ).fetchall()
        if len(identified) > MAX_DASHBOARD_FEATURE_IDS:
            raise OffHostQualificationError(
                "dashboard feature identity evidence exceeds qualification bound"
            )
    with dashboard() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL search_path = pg_catalog, public")
        connection.execute("SET ROLE leo_dashboard")
        projected = connection.execute(
            """
            SELECT DISTINCT ON (feature_id) feature_id
            FROM dashboard_feature_projection
            WHERE recording_id = %s
            ORDER BY feature_id, projection_sequence DESC
            LIMIT %s
            """,
            (config.pipeline.recording_id, MAX_DASHBOARD_FEATURE_IDS + 1),
        ).fetchall()
        if len(projected) > MAX_DASHBOARD_FEATURE_IDS:
            raise OffHostQualificationError(
                "dashboard FeatureSet evidence exceeds qualification bound"
            )
    return _PipelineRows(
        recording.recording_object,
        str(job["state"]),
        submitted_recording,
        feature_ref,
        str(feature["recording_id"]),
        Digest(
            DigestAlgorithm(str(feature["input_recording_digest_algorithm"])),
            str(feature["input_recording_digest_value"]),
        ),
        _integer(feature["observation_count"], "feature.observation_count"),
        _integer(feature["method_score_count"], "feature.method_score_count"),
        tuple(str(row["feature_id"]) for row in projected),
        tuple(
            (
                str(row["logical_id"]),
                str(row["authoritative_identity_digest"]),
            )
            for row in identified
        ),
    )


def _connection_factory(dsn: str) -> ConnectionFactory:
    if not dsn:
        raise OffHostQualificationError("database credential resolved empty")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        raise OffHostQualificationError(
            "off-host database inspection requires the server dependency"
        ) from error

    def connect() -> psycopg.Connection[dict[str, object]]:
        return psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=POSTGRES_TIMEOUT_S,
            options=(
                f"-c statement_timeout={POSTGRES_TIMEOUT_S * 1000} "
                f"-c lock_timeout={POSTGRES_TIMEOUT_S * 1000}"
            ),
        )

    return connect


def _require_writable_mount(config: QualificationConfig, writer_role: str) -> None:
    """Revalidate the mount immediately before the one explicitly armed write."""

    if not inspect_host(config, writer_role).passed:
        raise OffHostQualificationError("CAS mount is not safe for the armed probe")


def _table_privilege(connection: Any, table: str, privilege: str) -> bool:
    row = connection.execute(
        "SELECT has_table_privilege(current_user, %s, %s) AS allowed",
        (f"public.{table}", privilege),
    ).fetchone()
    return row is not None and row["allowed"] is True


def _function_privilege(connection: Any, function: str) -> bool:
    row = connection.execute(
        "SELECT has_function_privilege(current_user, %s, 'EXECUTE') AS allowed",
        (f"public.{function}",),
    ).fetchone()
    return row is not None and row["allowed"] is True


def _sequence_privilege(connection: Any, sequence: str, privilege: str) -> bool:
    row = connection.execute(
        "SELECT has_sequence_privilege(current_user, %s, %s) AS allowed",
        (f"public.{sequence}", privilege),
    ).fetchone()
    return row is not None and row["allowed"] is True


def _find_exact_mount(root: Path, mountinfo: str) -> _MountInfo:
    candidates: list[_MountInfo] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = _unescape_mount_field(fields[4])
            info = _MountInfo(
                mount_point=mount_point,
                mount_root=_unescape_mount_field(fields[3]),
                device=fields[2],
                options=tuple(fields[5].split(",")),
                filesystem_type=fields[separator + 1],
                source=_unescape_mount_field(fields[separator + 2]),
            )
        except (IndexError, ValueError):
            continue
        if mount_point == str(root):
            candidates.append(info)
    if len(candidates) != 1:
        raise OffHostQualificationError("CAS root is not one exact mount point")
    return candidates[0]


def _unescape_mount_field(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _probe_payload(
    config: QualificationConfig, writer_role: str, probe_id: str
) -> bytes:
    return _canonical_json(
        {
            "schema_id": SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "station_id": config.station_id,
            "config_digest": config.config_digest,
            "writer_role": writer_role,
            "probe_id": probe_id,
        }
    )


def _load_host_report(path: Path) -> HostReport:
    document = _mapping(
        json.loads(_read_bounded(path, MAX_REPORT_BYTES, "host report")),
        "host report",
    )
    _exact_keys(
        document,
        {
            "schema_id",
            "schema_version",
            "station_id",
            "config_digest",
            "host_role",
            "mount",
            "gates",
            "status",
        },
        "host report",
    )
    mount = _mapping(document["mount"], "host report mount")
    _exact_keys(
        mount,
        {
            "root",
            "source",
            "filesystem_type",
            "mount_root",
            "device",
            "options",
            "owner_uid",
            "owner_gid",
            "mode",
            "effective_uid",
            "effective_gid",
            "supplementary_gids",
        },
        "host report mount",
    )
    gates = tuple(
        _parse_gate(item) for item in _array(document["gates"], "host report gates")
    )
    host_role = str(document["host_role"])
    gate_names = tuple(gate.name for gate in gates)
    expected_gates = _expected_host_report_gates(host_role)
    if len(gate_names) != len(set(gate_names)) or set(gate_names) != expected_gates:
        raise OffHostQualificationError("host report gates are not exact")
    report = HostReport(
        str(document["schema_id"]),
        str(document["schema_version"]),
        str(document["station_id"]),
        str(document["config_digest"]),
        host_role,
        MountObservation(
            root=str(mount["root"]),
            source=str(mount["source"]),
            filesystem_type=str(mount["filesystem_type"]),
            mount_root=str(mount["mount_root"]),
            device=str(mount["device"]),
            options=tuple(
                str(item) for item in _array(mount["options"], "mount options")
            ),
            owner_uid=_integer(mount["owner_uid"], "owner_uid"),
            owner_gid=_integer(mount["owner_gid"], "owner_gid"),
            mode=str(mount["mode"]),
            effective_uid=_integer(mount["effective_uid"], "effective_uid"),
            effective_gid=_integer(mount["effective_gid"], "effective_gid"),
            supplementary_gids=tuple(
                _integer(item, "supplementary_gid")
                for item in _array(
                    mount["supplementary_gids"], "mount supplementary_gids"
                )
            ),
        ),
        gates,
    )
    expected_status = "pass" if report.passed else "fail"
    if document["status"] != expected_status:
        raise OffHostQualificationError("host report status disagrees with its gates")
    return report


def _parse_gate(value: object) -> Gate:
    gate = _mapping(value, "gate")
    _exact_keys(gate, {"name", "passed", "detail"}, "gate")
    return Gate(
        _nonempty(gate["name"], "gate.name"),
        _boolean(gate["passed"], "gate.passed"),
        str(gate["detail"]),
    )


def _expected_host_report_gates(host_role: str) -> set[str]:
    roles = _database_roles_for_host(host_role)
    names = set(_HOST_GATE_NAMES) | {
        "postgres.audit.read_only",
        "postgres.audit.session_login",
        "postgres.audit.cluster_identity",
        "postgres.audit.migration_receipts",
    }
    for role in roles:
        names.update(
            {
                f"postgres.{role}.read_only",
                f"postgres.{role}.session_login",
                f"postgres.{role}.membership",
                f"postgres.{role}.direct_authority",
                f"postgres.{role}.assumed_role",
                f"postgres.{role}.required_privileges",
                f"postgres.{role}.forbidden_privileges",
                f"postgres.{role}.required_functions",
                f"postgres.{role}.required_sequences",
            }
        )
    return names


def _database_roles_for_host(host_role: str) -> tuple[str, ...]:
    roles = {
        "capture": ("leo_capture",),
        "analysis": ("leo_analysis", "leo_dashboard"),
        "dashboard": ("leo_dashboard",),
    }.get(host_role)
    if roles is None:
        raise OffHostQualificationError("host role is unsupported")
    return roles


def _load_probe_receipt(path: Path) -> ProbeReceipt:
    document = _mapping(
        json.loads(_read_bounded(path, MAX_CONFIG_BYTES, "probe receipt")),
        "probe receipt",
    )
    return ProbeReceipt(
        str(document["schema_id"]),
        str(document["schema_version"]),
        str(document["station_id"]),
        str(document["config_digest"]),
        str(document["writer_role"]),
        str(document["probe_id"]),
        _object_ref(document["object_ref"]),
    )


def _object_document(ref: ObjectRef) -> dict[str, object]:
    return {
        "digest": {"algorithm": ref.digest.algorithm.value, "value": ref.digest.value},
        "byte_count": ref.byte_count,
        "media_type": ref.media_type,
        "format_id": ref.format_id,
        "locator": ref.locator,
    }


def _object_ref(value: object) -> ObjectRef:
    document = _mapping(value, "object_ref")
    digest = _mapping(document["digest"], "object_ref.digest")
    return ObjectRef(
        Digest(
            DigestAlgorithm(_nonempty(digest["algorithm"], "digest.algorithm")),
            _nonempty(digest["value"], "digest.value"),
        ),
        _integer(document["byte_count"], "object_ref.byte_count"),
        _nonempty(document["media_type"], "object_ref.media_type"),
        _nonempty(document["format_id"], "object_ref.format_id"),
        _nonempty(document["locator"], "object_ref.locator"),
    )


def _recording_ref(value: object) -> RecordingObjectRef:
    document = _mapping(value, "recording_object_ref")
    _exact_keys(
        document,
        {"recording_id", "data_object", "metadata_object", "manifest_digest"},
        "recording_object_ref",
    )
    manifest = _mapping(document["manifest_digest"], "manifest_digest")
    _exact_keys(manifest, {"algorithm", "value"}, "manifest_digest")
    return RecordingObjectRef(
        RecordingId(_nonempty(document["recording_id"], "recording_id")),
        _object_ref(document["data_object"]),
        _object_ref(document["metadata_object"]),
        Digest(
            DigestAlgorithm(
                _nonempty(manifest["algorithm"], "manifest_digest.algorithm")
            ),
            _nonempty(manifest["value"], "manifest_digest.value"),
        ),
    )


def _feature_set_id(value: str) -> FeatureSetId:
    return FeatureSetId(value)


def _analysis_run_id(value: str) -> AnalysisRunId:
    return AnalysisRunId(value)


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise OffHostQualificationError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise OffHostQualificationError(f"{name} must be an array")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise OffHostQualificationError(f"{name} fields are not exact")


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise OffHostQualificationError(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OffHostQualificationError(f"{name} must be an integer")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise OffHostQualificationError(f"{name} must be a boolean")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def _read_bounded(path: Path, limit: int, name: str) -> bytes:
    try:
        with path.open("rb") as stream:
            payload = stream.read(limit + 1)
    except OSError as error:
        raise OffHostQualificationError(f"{name} is unreadable") from error
    if len(payload) > limit:
        raise OffHostQualificationError(f"{name} exceeds its hard limit")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
