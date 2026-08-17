from __future__ import annotations

import pytest

from leo_flow.adapters.waterfall_doppler_postgres import (
    WaterfallDopplerConflictError,
    _publish_doppler,
    _publish_waterfall_v0_2,
)
from leo_flow.analysis.recording.waterfall_v0_2_persistence import (
    WaterfallCatalogProjectionV0_2,
)
from leo_flow.analysis.tracking.doppler_persistence import (
    DopplerCatalogProjectionV0_1,
)
from leo_flow.contracts.core import (
    Digest,
    JobId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.doppler_evidence import DopplerAnalysisId
from leo_flow.contracts.storage import ObjectRef
from leo_flow.jobs.contracts import JobLease, JobPayload, JobType


class _ReplayCursor:
    def __init__(self, row):
        self.row = row
        self.statements = []

    def execute(self, statement, parameters):
        self.statements.append((statement, parameters))

    def fetchone(self):
        return {"inserted": False}

    def fetchall(self):
        return [self.row]


def _lease() -> JobLease:
    return JobLease(
        JobId("job_replay"),
        JobType.WATERFALL_ANALYSIS,
        JobPayload.create(SchemaRef("job"), {}),
        1,
        "lease",
        1,
        UtcNs(10_000),
    )


def _object(value: bytes, format_id: str) -> ObjectRef:
    return ObjectRef(
        Digest.sha256(value),
        len(value),
        "application/json",
        format_id,
        f"cas:{format_id}",
    )


def test_waterfall_v0_2_exact_replay_is_accepted_and_conflict_is_rejected() -> None:
    projection = WaterfallCatalogProjectionV0_2(
        "waterfall_" + "1" * 32,
        "arun_" + "2" * 32,
        "rec_replay",
        Digest.sha256(b"recording"),
        Digest.sha256(b"request"),
        2,
        1_024,
    )
    ref = _object(b"waterfall", "waterfall-bundle-v0.2")
    row = {
        "product_id": projection.product_id,
        "analysis_run_id": projection.analysis_run_id,
        "source_job_id": "job_replay",
        "recording_id": projection.recording_id,
        "input_recording_digest_value": projection.input_recording_digest.value,
        "request_digest_value": projection.request_digest.value,
        "bundle_digest_value": ref.digest.value,
        "tile_count": 2,
        "pixel_count": 1_024,
        "idempotency_key": "waterfall-analysis:job_replay:v0.2",
    }

    _publish_waterfall_v0_2(
        _ReplayCursor(row), _lease(), projection, ref, "waterfall_" + "3" * 32
    )

    conflict = dict(row, bundle_digest_value=Digest.sha256(b"other").value)
    with pytest.raises(WaterfallDopplerConflictError, match="identity conflicts"):
        _publish_waterfall_v0_2(
            _ReplayCursor(conflict),
            _lease(),
            projection,
            ref,
            "waterfall_" + "3" * 32,
        )


def test_doppler_exact_replay_is_accepted_and_conflict_is_rejected() -> None:
    waterfall_digest = Digest.sha256(b"waterfall")
    projection = DopplerCatalogProjectionV0_1(
        DopplerAnalysisId("doppler_" + "4" * 32),
        RecordingId("rec_replay"),
        "waterfall_" + "1" * 32,
        waterfall_digest,
        SegmentId("seg_ch4_lower"),
        ReceiverChainId("rx_first"),
        Digest.sha256(b"spectrogram"),
        Digest.sha256(b"basic-config"),
        Digest.sha256(b"advanced-config"),
        3,
        2,
        12.5,
    )
    basic = _object(b"basic", "blind-doppler-bundle-v0.1")
    advanced = _object(b"advanced", "advanced-doppler-evidence-bundle-v0.1")
    row = {
        "doppler_id": str(projection.doppler_id),
        "source_job_id": "job_replay",
        "recording_id": str(projection.recording_id),
        "waterfall_product_id": projection.waterfall_product_id,
        "waterfall_bundle_digest_value": waterfall_digest.value,
        "segment_id": str(projection.segment_id),
        "receiver_chain_id": str(projection.receiver_chain_id),
        "spectrogram_digest_value": projection.spectrogram_digest.value,
        "basic_config_digest_value": projection.basic_config_digest.value,
        "advanced_config_digest_value": projection.advanced_config_digest.value,
        "basic_bundle_digest_value": basic.digest.value,
        "advanced_bundle_digest_value": advanced.digest.value,
        "candidate_count": 3,
        "moving_candidate_count": 2,
        "strongest_candidate_score": 12.5,
        "idempotency_key": f"waterfall-analysis:job_replay:{projection.doppler_id}",
    }

    _publish_doppler(_ReplayCursor(row), _lease(), projection, basic, advanced)

    conflict = dict(row, moving_candidate_count=1)
    with pytest.raises(WaterfallDopplerConflictError, match="identity conflicts"):
        _publish_doppler(_ReplayCursor(conflict), _lease(), projection, basic, advanced)
