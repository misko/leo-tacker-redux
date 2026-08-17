"""Exact read-only receipt for deferred waterfall collection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from leo_flow.contracts.core import (
    AnalysisRunId,
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    JobId,
    RecordingId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.contracts.waterfall import WaterfallProductId, WaterfallProductRefV0_1

ConnectionFactory = Callable[[], psycopg.Connection[dict[str, object]]]


class WaterfallReceiptError(RuntimeError):
    pass


@dataclass(frozen=True)
class WaterfallAnalysisReceiptV0_1:
    work_id: str
    source_job_id: JobId
    work_state: str
    waterfall_ref: WaterfallProductRefV0_1
    input_recording_digest: Digest
    request_digest: Digest
    tile_count: int
    cell_count: int
    projected_utc_ns: UtcNs | None
    job_state: str
    job_result: ArtifactRef


class PostgresWaterfallReceiptReaderV0_1:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def read(self, job_id: JobId) -> WaterfallAnalysisReceiptV0_1 | None:
        with self._connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                "SELECT * FROM public.read_waterfall_analysis_receipt(%s)",
                (str(job_id),),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise WaterfallReceiptError("waterfall receipt is ambiguous")
        return _receipt(rows[0], job_id)


def _receipt(
    row: dict[str, object], expected_job_id: JobId
) -> WaterfallAnalysisReceiptV0_1:
    job_id = JobId(_text(row, "source_job_id"))
    bundle = ObjectRef(
        _digest(row, "bundle"),
        _integer(row, "bundle_byte_count"),
        _text(row, "bundle_media_type"),
        _text(row, "bundle_format_id"),
        _text(row, "bundle_locator"),
    )
    ref = WaterfallProductRefV0_1(
        WaterfallProductId(_text(row, "product_id")),
        AnalysisRunId(_text(row, "analysis_run_id")),
        RecordingId(_text(row, "recording_id")),
        bundle,
    )
    job_result = _artifact(row.get("job_result_ref"))
    if (
        job_id != expected_job_id
        or _text(row, "job_state") != "succeeded"
        or job_result.artifact_id != str(ref.product_id)
        or job_result.digest != bundle.digest
        or job_result.schema != SchemaRef("org.leo-flow.waterfall-bundle")
    ):
        raise WaterfallReceiptError("receipt contradicts waterfall job identity")
    projected = row.get("projected_at_utc")
    if projected is not None and not isinstance(projected, datetime):
        raise WaterfallReceiptError("receipt projection time is invalid")
    return WaterfallAnalysisReceiptV0_1(
        _text(row, "work_id"),
        job_id,
        _text(row, "work_state"),
        ref,
        _digest(row, "input"),
        _digest(row, "request"),
        _integer(row, "tile_count"),
        _integer(row, "cell_count"),
        None if projected is None else _utc_ns(projected),
        "succeeded",
        job_result,
    )


def _artifact(value: object) -> ArtifactRef:
    if not isinstance(value, dict):
        raise WaterfallReceiptError("job result is invalid")
    try:
        return ArtifactRef(
            str(value["artifact_id"]),
            Digest(
                DigestAlgorithm(str(value["digest_algorithm"])),
                str(value["digest_value"]),
            ),
            SchemaRef(
                str(value["schema_id"]),
                SchemaVersion.parse(str(value["schema_version"])),
            ),
        )
    except (KeyError, ValueError) as error:
        raise WaterfallReceiptError("job result identity is invalid") from error


def _digest(row: dict[str, object], prefix: str) -> Digest:
    try:
        return Digest(
            DigestAlgorithm(_text(row, f"{prefix}_digest_algorithm")),
            _text(row, f"{prefix}_digest_value"),
        )
    except ValueError as error:
        raise WaterfallReceiptError(f"{prefix} digest is invalid") from error


def _text(row: dict[str, object], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value:
        raise WaterfallReceiptError(f"receipt {name} is invalid")
    return value


def _integer(row: dict[str, object], name: str) -> int:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WaterfallReceiptError(f"receipt {name} is invalid")
    return value


def _utc_ns(value: datetime) -> UtcNs:
    if value.tzinfo is None:
        raise WaterfallReceiptError("receipt timestamp has no timezone")
    utc = value.astimezone(UTC)
    return UtcNs(int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000)
