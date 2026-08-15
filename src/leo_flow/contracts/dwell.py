"""Strict scan-result and bounded dwell-request handoff contracts."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_token, require_utc_ns
from .core import V0_1, Digest, RadioId, RecordingId, SchemaRef, StationId, UtcNs
from .evidence import LabelEvidenceRef
from .features import FeatureSetRef

MAX_DWELL_CENTER_FREQUENCY_HZ = 6_000_000_000
MAX_DWELL_SAMPLE_RATE_HZ = 20_000_000
MAX_DWELL_BANDWIDTH_HZ = 20_000_000
MAX_DWELL_DURATION_NS = 60_000_000_000
MAX_DWELL_SAMPLE_COUNT = 100_000_000
MAX_DWELL_TTL_NS = 300_000_000_000


@dataclass(frozen=True)
class ScanResultRef:
    """One analysis-owned tuning result without detector implementation state."""

    schema: SchemaRef
    result_id: str
    recording_id: RecordingId
    recording_identity_digest: Digest
    feature_set_ref: FeatureSetRef
    station_id: StationId
    radio_id: RadioId
    observed_utc_ns: UtcNs
    center_frequency_hz: int
    sample_rate_hz: int
    bandwidth_hz: int
    evidence_refs: tuple[LabelEvidenceRef, ...]

    SCHEMA_ID = "org.leo-flow.scan-result-ref"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported scan result schema")
        require_token(self.result_id, "result_id")
        if not self.result_id.startswith("scanresult_"):
            raise ValueError("result_id must start with 'scanresult_'")
        require_utc_ns(self.observed_utc_ns, "observed_utc_ns")
        _validate_tuning(
            self.center_frequency_hz,
            self.sample_rate_hz,
            self.bandwidth_hz,
        )
        _validate_evidence(self.evidence_refs)


@dataclass(frozen=True)
class DwellRequest:
    """An expiring, idempotent request that capture can validate without analysis."""

    schema: SchemaRef
    request_id: str
    source: ScanResultRef
    station_id: StationId
    radio_id: RadioId
    issued_utc_ns: UtcNs
    expires_utc_ns: UtcNs
    center_frequency_hz: int
    sample_rate_hz: int
    bandwidth_hz: int
    duration_ns: int
    sample_count: int
    reason_code: str
    evidence_refs: tuple[LabelEvidenceRef, ...]
    idempotency_key: str

    SCHEMA_ID = "org.leo-flow.dwell-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported dwell request schema")
        require_token(self.request_id, "request_id")
        if not self.request_id.startswith("dwell_"):
            raise ValueError("request_id must start with 'dwell_'")
        require_utc_ns(self.issued_utc_ns, "issued_utc_ns")
        require_utc_ns(self.expires_utc_ns, "expires_utc_ns")
        if not (
            self.source.observed_utc_ns
            <= self.issued_utc_ns
            < self.expires_utc_ns
            <= self.issued_utc_ns + MAX_DWELL_TTL_NS
        ):
            raise ValueError("dwell request validity interval is invalid or too long")
        if (
            self.station_id != self.source.station_id
            or self.radio_id != self.source.radio_id
        ):
            raise ValueError("dwell routing identity differs from its scan result")
        tuning = (
            self.center_frequency_hz,
            self.sample_rate_hz,
            self.bandwidth_hz,
        )
        _validate_tuning(*tuning)
        if tuning != (
            self.source.center_frequency_hz,
            self.source.sample_rate_hz,
            self.source.bandwidth_hz,
        ):
            raise ValueError("dwell tuning differs from its scan result")
        if (
            isinstance(self.duration_ns, bool)
            or not isinstance(self.duration_ns, int)
            or not 0 < self.duration_ns <= MAX_DWELL_DURATION_NS
        ):
            raise ValueError("dwell duration exceeds its hard bound")
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or not 0 < self.sample_count <= MAX_DWELL_SAMPLE_COUNT
        ):
            raise ValueError("dwell sample count exceeds its hard bound")
        if self.sample_count * 1_000_000_000 != self.sample_rate_hz * self.duration_ns:
            raise ValueError("dwell duration and sample count disagree")
        require_token(self.reason_code, "reason_code")
        _validate_evidence(self.evidence_refs)
        if self.evidence_refs != self.source.evidence_refs:
            raise ValueError("dwell evidence differs from its scan result")
        require_token(self.idempotency_key, "idempotency_key")


def _validate_tuning(
    center_frequency_hz: int, sample_rate_hz: int, bandwidth_hz: int
) -> None:
    values = (center_frequency_hz, sample_rate_hz, bandwidth_hz)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError("dwell tuning values must be integer Hz")
    if not 0 < center_frequency_hz <= MAX_DWELL_CENTER_FREQUENCY_HZ:
        raise ValueError("dwell center frequency exceeds its hard bound")
    if not 0 < sample_rate_hz <= MAX_DWELL_SAMPLE_RATE_HZ:
        raise ValueError("dwell sample rate exceeds its hard bound")
    if not 0 < bandwidth_hz <= min(sample_rate_hz, MAX_DWELL_BANDWIDTH_HZ):
        raise ValueError("dwell bandwidth exceeds its hard bound")


def _validate_evidence(evidence_refs: tuple[LabelEvidenceRef, ...]) -> None:
    if not evidence_refs:
        raise ValueError("scan and dwell requests require evidence")
    identities = tuple(item.evidence_id for item in evidence_refs)
    if identities != tuple(sorted(identities)) or len(identities) != len(
        set(identities)
    ):
        raise ValueError("request evidence must be unique and canonical")
