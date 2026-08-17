"""Calibrated Starlink detection and de-duplicated beacon event contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_finite, require_token, require_utc_ns
from .core import (
    V0_1,
    ArtifactRef,
    Digest,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from .starlink import StarlinkEdge


class StarlinkCoincidenceBasis(str, Enum):
    SINGLE_STREAM = "single_stream"
    INTRA_RADIO_SIMULTANEOUS = "intra_radio_simultaneous"
    SOFTWARE_COORDINATED_MULTI_RADIO = "software_coordinated_multi_radio"


@dataclass(frozen=True)
class StarlinkCalibratedDetectionV0_1:
    """One candidate that passed its exact cell's approved threshold."""

    schema: SchemaRef
    detection_id: str
    candidate_id: str
    candidate_digest: Digest
    evaluation_ref: ArtifactRef
    calibration_ref: ArtifactRef
    radio_id: RadioId
    receiver_chain_id: ReceiverChainId
    channel_number: int
    edge: StarlinkEdge
    tuning_identity_digest: Digest
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    winning_cfo_hz: float
    score: float
    threshold: float

    SCHEMA_ID = "org.leo-flow.starlink-calibrated-detection"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported calibrated Starlink detection schema")
        require_token(self.detection_id, "detection_id")
        require_token(self.candidate_id, "candidate_id")
        if self.channel_number not in (1, 2, 3, 4):
            raise ValueError("channel_number must be one of 1, 2, 3, 4")
        require_utc_ns(self.interval_start_utc_ns, "interval_start_utc_ns")
        require_utc_ns(self.interval_stop_utc_ns, "interval_stop_utc_ns")
        if self.interval_stop_utc_ns <= self.interval_start_utc_ns:
            raise ValueError("detection interval must be non-empty")
        require_finite(self.winning_cfo_hz, "winning_cfo_hz")
        require_finite(self.score, "score")
        require_finite(self.threshold, "threshold")
        if self.score < self.threshold:
            raise ValueError("a calibrated detection must pass its cited threshold")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            self.detection_id,
            self.digest,
            SchemaRef(self.SCHEMA_ID, V0_1),
        )


@dataclass(frozen=True)
class StarlinkBeaconEventV0_1:
    """One clustered beacon occurrence; not a satellite identity assertion."""

    schema: SchemaRef
    event_id: str
    channel_number: int
    edge: StarlinkEdge
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    cfo_min_hz: float
    cfo_max_hz: float
    detection_refs: tuple[ArtifactRef, ...]
    candidate_ids: tuple[str, ...]
    radio_ids: tuple[RadioId, ...]
    receiver_chain_ids: tuple[ReceiverChainId, ...]
    coincidence_basis: StarlinkCoincidenceBasis
    satellite_association_status: str

    SCHEMA_ID = "org.leo-flow.starlink-beacon-event"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported Starlink beacon event schema")
        require_token(self.event_id, "event_id")
        if not self.detection_refs:
            raise ValueError("beacon event requires calibrated detection evidence")
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("beacon event cannot count one candidate twice")
        if len(self.detection_refs) != len(self.candidate_ids):
            raise ValueError("each event detection must correspond to one candidate")
        require_utc_ns(self.interval_start_utc_ns, "interval_start_utc_ns")
        require_utc_ns(self.interval_stop_utc_ns, "interval_stop_utc_ns")
        if self.interval_stop_utc_ns <= self.interval_start_utc_ns:
            raise ValueError("event interval must be non-empty")
        require_finite(self.cfo_min_hz, "cfo_min_hz")
        require_finite(self.cfo_max_hz, "cfo_max_hz")
        if tuple(sorted(set(self.radio_ids))) != self.radio_ids:
            raise ValueError("event radio identities must be sorted and unique")
        if tuple(sorted(set(self.receiver_chain_ids))) != self.receiver_chain_ids:
            raise ValueError("event receiver identities must be sorted and unique")
        if self.cfo_max_hz < self.cfo_min_hz:
            raise ValueError("event CFO interval is reversed")
        if self.satellite_association_status != "not_evaluated":
            raise ValueError("v0.1 beacon events do not assert satellite identity")
        expected = (
            StarlinkCoincidenceBasis.SOFTWARE_COORDINATED_MULTI_RADIO
            if len(self.radio_ids) > 1
            else StarlinkCoincidenceBasis.INTRA_RADIO_SIMULTANEOUS
            if len(self.receiver_chain_ids) > 1
            else StarlinkCoincidenceBasis.SINGLE_STREAM
        )
        if self.coincidence_basis is not expected:
            raise ValueError("coincidence basis overclaims or understates its evidence")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)
