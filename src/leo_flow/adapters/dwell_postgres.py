"""Authenticated PostgreSQL ingress for immutable, versioned dwell requests."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from leo_flow.contracts.capture import CapturePlanRef
from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    FeatureSetId,
    JobId,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    StationId,
    UtcNs,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.dwell import DwellRequest, ScanResultRef
from leo_flow.contracts.evidence import EvidenceKind, LabelEvidenceRef
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.storage import ObjectRef
from leo_flow.jobs.contracts import validate_park_reason
from leo_flow.jobs.ports import StaleLeaseError

from . import dwell_postgres_sql

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]
DWELL_JOB_TYPE = "dwell_capture"
MAX_DWELL_PUBLICATION_BYTES = 65_536


class PostgresDwellIngressError(RuntimeError):
    """Durable ingress state is invalid, contradictory, or unavailable."""


class DwellRequestConflictError(PostgresDwellIngressError):
    """A request identity or idempotency key was reused with other content."""


class DwellRequestIntegrityError(PostgresDwellIngressError):
    """A claimed database payload does not match its immutable digest/indexes."""


class DwellRequestSourceError(PostgresDwellIngressError):
    """The source recording or FeatureSet is not exact and authoritative."""


@dataclass(frozen=True)
class PublishedDwellRequest:
    job_id: JobId
    request_id: str
    request_digest: Digest
    idempotency_key: str


@dataclass(frozen=True)
class DwellRequestLease:
    job_id: JobId
    request: DwellRequest
    request_digest: Digest
    attempt: int
    lease_token: str
    lease_generation: int
    lease_expires_utc_ns: UtcNs


class PostgresDwellRequestIngress:
    """Analysis-side publisher; database role membership authenticates ingress."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def publish(self, request: DwellRequest) -> PublishedDwellRequest:
        publication = _publication(request)
        if len(canonical_json_bytes(publication)) > MAX_DWELL_PUBLICATION_BYTES:
            raise ValueError("dwell request publication exceeds 65536 bytes")
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            try:
                cursor.execute(
                    dwell_postgres_sql.PUBLISH_SQL,
                    {"publication": Jsonb(publication)},
                )
                row = cursor.fetchone()
            except psycopg.errors.UniqueViolation as error:
                raise DwellRequestConflictError(
                    "dwell request identity identifies different content"
                ) from error
            except psycopg.errors.ForeignKeyViolation as error:
                raise DwellRequestSourceError(
                    "dwell request source is not exact and authoritative"
                ) from error
        if row is None or not isinstance(next(iter(row.values())), bool):
            raise PostgresDwellIngressError(
                "dwell publication function returned no stable outcome"
            )
        digest = canonical_digest(request)
        return PublishedDwellRequest(
            JobId(str(publication["job_id"])),
            request.request_id,
            digest,
            request.idempotency_key,
        )


class PostgresDwellRequestQueue:
    """Capture-side, route-scoped lease operations over the shared job table."""

    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._connect = connect
        self._token = token_factory or (lambda: f"lease_{secrets.token_hex(16)}")

    def claim(
        self,
        station_id: StationId,
        radio_id: RadioId,
        ttl_s: float,
    ) -> DwellRequestLease | None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                dwell_postgres_sql.CLAIM_SQL,
                {
                    "station_id": str(station_id),
                    "radio_id": str(radio_id),
                    "lease_token": self._token(),
                    "ttl_interval": _ttl(ttl_s),
                },
            )
            row = cursor.fetchone()
        return None if row is None else _lease(row, station_id, radio_id)

    def heartbeat(self, lease: DwellRequestLease, ttl_s: float) -> DwellRequestLease:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                dwell_postgres_sql.HEARTBEAT_SQL,
                {**_lease_parameters(lease), "ttl_interval": _ttl(ttl_s)},
            )
            row = cursor.fetchone()
        expires = None if row is None else row["lease_expires_utc"]
        if not isinstance(expires, datetime):
            raise StaleLeaseError("dwell lease token, generation, or expiry is stale")
        return replace(lease, lease_expires_utc_ns=_datetime_to_ns(expires))

    def complete(self, lease: DwellRequestLease, result: CapturePlanRef) -> None:
        if result.plan_id != PlanId(f"plan_{lease.request.request_id}"):
            raise ValueError("capture plan result belongs to another dwell request")
        self._fenced(
            dwell_postgres_sql.COMPLETE_SQL,
            {
                **_lease_parameters(lease),
                "result_ref": Jsonb(
                    {
                        "artifact_id": str(result.plan_id),
                        "digest_algorithm": result.plan_digest.algorithm.value,
                        "digest_value": result.plan_digest.value,
                        "schema_id": "org.leo-flow.capture-plan",
                        "schema_version": "0.1",
                    }
                ),
            },
            "completed",
        )

    def fail(
        self,
        lease: DwellRequestLease,
        reason: str,
        retry_at_utc_ns: UtcNs,
    ) -> None:
        validate_park_reason(reason)
        if not int(retry_at_utc_ns) < int(lease.request.expires_utc_ns):
            raise ValueError("dwell retry must precede request expiry")
        self._fenced(
            dwell_postgres_sql.FAIL_SQL,
            {
                **_lease_parameters(lease),
                "reason": reason,
                "retry_at_utc": _ns_to_datetime(retry_at_utc_ns),
            },
            "failed",
        )

    def park(self, lease: DwellRequestLease, reason: str) -> None:
        validate_park_reason(reason)
        self._fenced(
            dwell_postgres_sql.PARK_SQL,
            {**_lease_parameters(lease), "reason": reason},
            "parked",
        )

    def _fenced(
        self,
        statement: str,
        parameters: dict[str, object],
        outcome: str,
    ) -> None:
        with (
            self._connect() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(statement, parameters)
            row = cursor.fetchone()
        if row is None or row[outcome] is not True:
            raise StaleLeaseError("dwell lease token, generation, or expiry is stale")


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)


def _publication(request: DwellRequest) -> dict[str, object]:
    payload = json.loads(canonical_json_bytes(request))
    digest = canonical_digest(request)
    feature = request.source.feature_set_ref
    return {
        "job_id": f"job_dwell_{digest.value}",
        "request_id": request.request_id,
        "request_schema_id": request.schema.schema_id,
        "request_schema_version": str(request.schema.version),
        "request_digest_algorithm": digest.algorithm.value,
        "request_digest_value": digest.value,
        "idempotency_key": request.idempotency_key,
        "source_recording_id": str(request.source.recording_id),
        "source_recording_digest_algorithm": (
            request.source.recording_identity_digest.algorithm.value
        ),
        "source_recording_digest_value": request.source.recording_identity_digest.value,
        "source_feature_set_id": str(feature.feature_set_id),
        "source_analysis_run_id": str(feature.analysis_run_id),
        "source_feature_digest_algorithm": feature.bundle_ref.digest.algorithm.value,
        "source_feature_digest_value": feature.bundle_ref.digest.value,
        "station_id": str(request.station_id),
        "radio_id": str(request.radio_id),
        "issued_utc_ns": int(request.issued_utc_ns),
        "expires_utc_ns": int(request.expires_utc_ns),
        "payload": payload,
    }


def _lease(
    row: dict[str, object], station_id: StationId, radio_id: RadioId
) -> DwellRequestLease:
    if (
        row["payload_schema_id"] != DwellRequest.SCHEMA_ID
        or row["payload_schema_version"] != "0.1"
        or row["request_digest_algorithm"] != DigestAlgorithm.SHA256.value
        or row["station_id"] != str(station_id)
        or row["radio_id"] != str(radio_id)
    ):
        raise DwellRequestIntegrityError("claimed dwell request indexes differ")
    request = _decode_request(row["payload"])
    digest = Digest(
        DigestAlgorithm(str(row["request_digest_algorithm"])),
        str(row["request_digest_value"]),
    )
    expires = row["lease_expires_utc"]
    if (
        canonical_digest(request) != digest
        or request.request_id != row["request_id"]
        or request.idempotency_key != row["idempotency_key"]
        or int(request.issued_utc_ns) != _database_int(row["issued_utc_ns"], "issued")
        or int(request.expires_utc_ns)
        != _database_int(row["expires_utc_ns"], "expires")
        or request.station_id != station_id
        or request.radio_id != radio_id
        or not isinstance(expires, datetime)
    ):
        raise DwellRequestIntegrityError(
            "claimed dwell request digest or route differs"
        )
    return DwellRequestLease(
        JobId(str(row["job_id"])),
        request,
        digest,
        _database_int(row["attempt"], "attempt"),
        str(row["lease_token"]),
        _database_int(row["lease_generation"], "lease_generation"),
        _datetime_to_ns(expires),
    )


def _decode_request(value: object) -> DwellRequest:
    root = _object(value, "request")
    _keys(
        root,
        {
            "schema",
            "request_id",
            "source",
            "station_id",
            "radio_id",
            "issued_utc_ns",
            "expires_utc_ns",
            "center_frequency_hz",
            "sample_rate_hz",
            "bandwidth_hz",
            "duration_ns",
            "sample_count",
            "reason_code",
            "evidence_refs",
            "idempotency_key",
        },
        "request",
    )
    return DwellRequest(
        _schema(root["schema"]),
        _string(root, "request_id"),
        _scan_result(root["source"]),
        StationId(_string(root, "station_id")),
        RadioId(_string(root, "radio_id")),
        UtcNs(_integer(root, "issued_utc_ns")),
        UtcNs(_integer(root, "expires_utc_ns")),
        _integer(root, "center_frequency_hz"),
        _integer(root, "sample_rate_hz"),
        _integer(root, "bandwidth_hz"),
        _integer(root, "duration_ns"),
        _integer(root, "sample_count"),
        _string(root, "reason_code"),
        _evidence_tuple(root["evidence_refs"]),
        _string(root, "idempotency_key"),
    )


def _scan_result(value: object) -> ScanResultRef:
    item = _object(value, "scan result")
    _keys(
        item,
        {
            "schema",
            "result_id",
            "recording_id",
            "recording_identity_digest",
            "feature_set_ref",
            "station_id",
            "radio_id",
            "observed_utc_ns",
            "center_frequency_hz",
            "sample_rate_hz",
            "bandwidth_hz",
            "evidence_refs",
        },
        "scan result",
    )
    return ScanResultRef(
        _schema(item["schema"]),
        _string(item, "result_id"),
        RecordingId(_string(item, "recording_id")),
        _digest(item["recording_identity_digest"]),
        _feature_ref(item["feature_set_ref"]),
        StationId(_string(item, "station_id")),
        RadioId(_string(item, "radio_id")),
        UtcNs(_integer(item, "observed_utc_ns")),
        _integer(item, "center_frequency_hz"),
        _integer(item, "sample_rate_hz"),
        _integer(item, "bandwidth_hz"),
        _evidence_tuple(item["evidence_refs"]),
    )


def _feature_ref(value: object) -> FeatureSetRef:
    item = _object(value, "feature set reference")
    _keys(
        item,
        {"feature_set_id", "analysis_run_id", "bundle_ref"},
        "feature set reference",
    )
    return FeatureSetRef(
        FeatureSetId(_string(item, "feature_set_id")),
        AnalysisRunId(_string(item, "analysis_run_id")),
        _object_ref(item["bundle_ref"]),
    )


def _object_ref(value: object) -> ObjectRef:
    item = _object(value, "object reference")
    _keys(
        item,
        {"digest", "byte_count", "media_type", "format_id", "locator"},
        "object reference",
    )
    return ObjectRef(
        _digest(item["digest"]),
        _integer(item, "byte_count"),
        _string(item, "media_type"),
        _string(item, "format_id"),
        _string(item, "locator"),
    )


def _evidence_tuple(value: object) -> tuple[LabelEvidenceRef, ...]:
    if not isinstance(value, list):
        raise DwellRequestIntegrityError("evidence_refs must be an array")
    return tuple(_evidence(item) for item in value)


def _evidence(value: object) -> LabelEvidenceRef:
    item = _object(value, "evidence reference")
    _keys(
        item,
        {
            "schema",
            "evidence_id",
            "kind",
            "artifact_ref",
            "producer_id",
            "produced_utc_ns",
            "independent_of_method_ids",
        },
        "evidence reference",
    )
    independent = item["independent_of_method_ids"]
    if not isinstance(independent, list) or not all(
        isinstance(method, str) for method in independent
    ):
        raise DwellRequestIntegrityError("evidence independence must be an array")
    return LabelEvidenceRef(
        _schema(item["schema"]),
        _string(item, "evidence_id"),
        EvidenceKind(_string(item, "kind")),
        _artifact(item["artifact_ref"]),
        _string(item, "producer_id"),
        UtcNs(_integer(item, "produced_utc_ns")),
        tuple(independent),
    )


def _artifact(value: object) -> ArtifactRef:
    item = _object(value, "artifact reference")
    _keys(item, {"artifact_id", "digest", "schema"}, "artifact reference")
    schema_value = item["schema"]
    return ArtifactRef(
        _string(item, "artifact_id"),
        _digest(item["digest"]),
        None if schema_value is None else _schema(schema_value),
    )


def _digest(value: object) -> Digest:
    item = _object(value, "digest")
    _keys(item, {"algorithm", "value"}, "digest")
    return Digest(
        DigestAlgorithm(_string(item, "algorithm")),
        _string(item, "value"),
    )


def _schema(value: object) -> SchemaRef:
    item = _object(value, "schema")
    _keys(item, {"schema_id", "version"}, "schema")
    version = _object(item["version"], "schema version")
    _keys(version, {"major", "minor"}, "schema version")
    return SchemaRef(
        _string(item, "schema_id"),
        SchemaVersion(_integer(version, "major"), _integer(version, "minor")),
    )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DwellRequestIntegrityError(f"database {name} must be an object")
    return value


def _keys(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise DwellRequestIntegrityError(f"database {name} fields differ")


def _string(value: Mapping[str, object], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result:
        raise DwellRequestIntegrityError(f"database {name} must be a string")
    return result


def _integer(value: Mapping[str, object], name: str) -> int:
    result = value.get(name)
    if isinstance(result, bool) or not isinstance(result, int):
        raise DwellRequestIntegrityError(f"database {name} must be an integer")
    return result


def _lease_parameters(lease: DwellRequestLease) -> dict[str, object]:
    return {
        "job_id": str(lease.job_id),
        "lease_token": lease.lease_token,
        "lease_generation": lease.lease_generation,
    }


def _ttl(ttl_s: float) -> timedelta:
    if isinstance(ttl_s, bool) or ttl_s <= 0:
        raise ValueError("lease TTL must be positive")
    result = timedelta(seconds=ttl_s)
    if result <= timedelta(0):
        raise ValueError("lease TTL is below PostgreSQL timestamp resolution")
    return result


def _ns_to_datetime(value: UtcNs) -> datetime:
    seconds, nanoseconds = divmod(int(value), 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC).replace(
        microsecond=nanoseconds // 1_000
    )


def _datetime_to_ns(value: datetime) -> UtcNs:
    if value.tzinfo is None:
        raise DwellRequestIntegrityError("database lease expiry is not timezone-aware")
    utc = value.astimezone(UTC)
    return UtcNs(int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000)


def _database_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DwellRequestIntegrityError(f"database {name} must be an integer")
    return value
