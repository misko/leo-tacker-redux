"""Exact read-only evidence for one durable FeatureSet projection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    FeatureSetId,
    JobId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
)
from leo_flow.contracts.features import FeatureSetRef
from leo_flow.contracts.storage import ObjectRef

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class CampaignProjectionReceiptError(RuntimeError):
    """Projection receipt evidence is missing or contradictory."""


@dataclass(frozen=True, slots=True)
class FeatureProjectionReceiptEvidence:
    work_id: str
    source_job_id: JobId
    state: str
    feature_ref: FeatureSetRef
    recording_id: RecordingId
    recording_digest: Digest
    projected_utc_ns: UtcNs | None
    job_state: str
    job_result: ArtifactRef


class PostgresCampaignProjectionReceiptReader:
    """Read one exact receipt without granting access to the private outbox."""

    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def read(self, source_job_id: JobId) -> FeatureProjectionReceiptEvidence | None:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                "SELECT * FROM public.read_feature_projection_receipt(%s)",
                (str(source_job_id),),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise CampaignProjectionReceiptError(
                "projection receipt query returned multiple identities"
            )
        return _receipt(rows[0], source_job_id)


def _receipt(
    row: dict[str, object], expected_job_id: JobId
) -> FeatureProjectionReceiptEvidence:
    source_job_id = JobId(_text(row, "source_job_id"))
    if source_job_id != expected_job_id:
        raise CampaignProjectionReceiptError(
            "projection receipt returned another source job"
        )
    feature_digest = _digest(row, "feature")
    feature_ref = FeatureSetRef(
        FeatureSetId(_text(row, "feature_set_id")),
        AnalysisRunId(_text(row, "analysis_run_id")),
        ObjectRef(
            feature_digest,
            _nonnegative_int(row, "feature_byte_count"),
            _text(row, "feature_media_type"),
            _text(row, "feature_format_id"),
            _text(row, "feature_locator"),
        ),
    )
    projected = row.get("projected_at_utc")
    if projected is not None and not isinstance(projected, datetime):
        raise CampaignProjectionReceiptError("projection receipt timestamp is invalid")
    job_state = _text(row, "job_state")
    job_result = _artifact(row.get("job_result_ref"))
    if (
        job_state != "succeeded"
        or job_result.artifact_id != str(feature_ref.feature_set_id)
        or job_result.digest != feature_ref.bundle_ref.digest
    ):
        raise CampaignProjectionReceiptError(
            "projection receipt does not match the succeeded source job"
        )
    return FeatureProjectionReceiptEvidence(
        _text(row, "work_id"),
        source_job_id,
        _text(row, "work_state"),
        feature_ref,
        RecordingId(_text(row, "recording_id")),
        _digest(row, "recording"),
        None if projected is None else _utc_ns(projected),
        job_state,
        job_result,
    )


def _digest(row: dict[str, object], prefix: str) -> Digest:
    try:
        return Digest(
            DigestAlgorithm(_text(row, f"{prefix}_digest_algorithm")),
            _text(row, f"{prefix}_digest_value"),
        )
    except ValueError as error:
        raise CampaignProjectionReceiptError(
            f"projection receipt {prefix} digest is invalid"
        ) from error


def _text(row: dict[str, object], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value:
        raise CampaignProjectionReceiptError(f"projection receipt {name} is invalid")
    return value


def _nonnegative_int(row: dict[str, object], name: str) -> int:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CampaignProjectionReceiptError(f"projection receipt {name} is invalid")
    return value


def _utc_ns(value: datetime) -> UtcNs:
    if value.tzinfo is None:
        raise CampaignProjectionReceiptError(
            "projection receipt timestamp has no timezone"
        )
    utc = value.astimezone(UTC)
    return UtcNs(int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000)


def _artifact(value: object) -> ArtifactRef:
    if not isinstance(value, dict):
        raise CampaignProjectionReceiptError("projection receipt job result is invalid")
    try:
        artifact_id = value["artifact_id"]
        algorithm = value["digest_algorithm"]
        digest_value = value["digest_value"]
        schema_id = value["schema_id"]
        schema_version = value["schema_version"]
    except KeyError as error:
        raise CampaignProjectionReceiptError(
            "projection receipt job result is incomplete"
        ) from error
    if not all(
        isinstance(item, str) and item
        for item in (
            artifact_id,
            algorithm,
            digest_value,
            schema_id,
            schema_version,
        )
    ):
        raise CampaignProjectionReceiptError(
            "projection receipt job result fields are invalid"
        )
    try:
        return ArtifactRef(
            artifact_id,
            Digest(DigestAlgorithm(algorithm), digest_value),
            SchemaRef(schema_id, SchemaVersion.parse(schema_version)),
        )
    except ValueError as error:
        raise CampaignProjectionReceiptError(
            "projection receipt job result identity is invalid"
        ) from error


def connection_factory(dsn: str) -> ConnectionFactory:
    return lambda: psycopg.connect(dsn, row_factory=dict_row)
