"""Capture-side policy gate for analysis-proposed public dwell requests."""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.contracts._validation import require_utc_ns
from leo_flow.contracts.capture import (
    ActivityKind,
    ActivityRequest,
    CapturePlan,
    GainSetting,
    SegmentRequest,
)
from leo_flow.contracts.core import (
    ActivityId,
    Digest,
    PlanId,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    SegmentId,
    StationId,
    UtcNs,
    canonical_digest,
)
from leo_flow.contracts.dwell import (
    MAX_DWELL_BANDWIDTH_HZ,
    MAX_DWELL_CENTER_FREQUENCY_HZ,
    MAX_DWELL_DURATION_NS,
    MAX_DWELL_SAMPLE_COUNT,
    MAX_DWELL_SAMPLE_RATE_HZ,
    DwellRequest,
)
from leo_flow.contracts.ports import (
    CapturePlanPublisher,
    CapturePlanSource,
    DwellRequestGatePort,
)


class DwellRequestRejected(ValueError):
    """A request is stale, misrouted, over policy, or reuses an identity."""


@dataclass(frozen=True)
class DwellSafetyPolicy:
    station_id: StationId
    radio_id: RadioId
    receiver_chain_ids: tuple[ReceiverChainId, ...]
    gain: GainSetting
    minimum_center_frequency_hz: int
    maximum_center_frequency_hz: int
    maximum_sample_rate_hz: int
    maximum_bandwidth_hz: int
    maximum_duration_ns: int
    maximum_sample_count: int

    def __post_init__(self) -> None:
        if not self.receiver_chain_ids or len(set(self.receiver_chain_ids)) != len(
            self.receiver_chain_ids
        ):
            raise ValueError("dwell receiver chains must be non-empty and unique")
        frequency_limits = (
            self.minimum_center_frequency_hz,
            self.maximum_center_frequency_hz,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in frequency_limits
        ) or not (
            0
            < self.minimum_center_frequency_hz
            <= self.maximum_center_frequency_hz
            <= MAX_DWELL_CENTER_FREQUENCY_HZ
        ):
            raise ValueError("dwell policy frequency range is invalid")
        caps = (
            (self.maximum_sample_rate_hz, MAX_DWELL_SAMPLE_RATE_HZ),
            (self.maximum_bandwidth_hz, MAX_DWELL_BANDWIDTH_HZ),
            (self.maximum_duration_ns, MAX_DWELL_DURATION_NS),
            (self.maximum_sample_count, MAX_DWELL_SAMPLE_COUNT),
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 < value <= hard_cap
            for value, hard_cap in caps
        ):
            raise ValueError("dwell policy exceeds a contract hard bound")
        if self.maximum_bandwidth_hz > self.maximum_sample_rate_hz:
            raise ValueError("dwell policy bandwidth cannot exceed sample rate")


class DwellRequestGate:
    """Validate, deduplicate, and lower one request to a declarative plan."""

    def __init__(self, policy: DwellSafetyPolicy) -> None:
        self._policy = policy
        self._accepted: dict[str, tuple[Digest, CapturePlan]] = {}

    def accept(self, request: DwellRequest, now_utc_ns: UtcNs) -> CapturePlan:
        require_utc_ns(now_utc_ns, "now_utc_ns")
        if not request.issued_utc_ns <= now_utc_ns < request.expires_utc_ns:
            raise DwellRequestRejected("dwell request is not currently valid")
        policy = self._policy
        if (
            request.station_id != policy.station_id
            or request.radio_id != policy.radio_id
        ):
            raise DwellRequestRejected(
                "dwell request is routed to another station or radio"
            )
        if not (
            policy.minimum_center_frequency_hz
            <= request.center_frequency_hz
            <= policy.maximum_center_frequency_hz
            and request.sample_rate_hz <= policy.maximum_sample_rate_hz
            and request.bandwidth_hz <= policy.maximum_bandwidth_hz
            and request.duration_ns <= policy.maximum_duration_ns
            and request.sample_count <= policy.maximum_sample_count
        ):
            raise DwellRequestRejected("dwell request exceeds capture policy")
        identity = canonical_digest(request)
        prior = self._accepted.get(request.idempotency_key)
        if prior is not None:
            if prior[0] != identity:
                raise DwellRequestRejected(
                    "dwell idempotency key identifies another request"
                )
            return prior[1]
        plan = _capture_plan(request, policy)
        self._accepted[request.idempotency_key] = (identity, plan)
        return plan


class DurableDwellRequestGate:
    """Persist and verify an accepted plan before exposing it to a scheduler."""

    def __init__(
        self,
        gate: DwellRequestGatePort,
        publisher: CapturePlanPublisher,
        source: CapturePlanSource,
    ) -> None:
        self._gate = gate
        self._publisher = publisher
        self._source = source

    def accept(self, request: DwellRequest, now_utc_ns: UtcNs) -> CapturePlan:
        proposed = self._gate.accept(request, now_utc_ns)
        expected_digest = canonical_digest(proposed)
        ref = self._publisher.publish(proposed, idempotency_key=request.idempotency_key)
        if ref.plan_id != proposed.plan_id or ref.plan_digest != expected_digest:
            raise DwellRequestRejected("capture plan publisher changed plan identity")
        durable = self._source.get(ref.plan_id)
        if durable != proposed or canonical_digest(durable) != ref.plan_digest:
            raise DwellRequestRejected("capture plan source returned different content")
        return durable


def _capture_plan(request: DwellRequest, policy: DwellSafetyPolicy) -> CapturePlan:
    evidence_digests = tuple(
        str(item.artifact_ref.digest) for item in request.evidence_refs
    )
    segment = SegmentRequest.create(
        segment_id=SegmentId(f"seg_{request.request_id}"),
        center_frequency_hz=float(request.center_frequency_hz),
        sample_rate_hz=float(request.sample_rate_hz),
        bandwidth_hz=float(request.bandwidth_hz),
        receiver_chain_ids=policy.receiver_chain_ids,
        gain=policy.gain,
        sample_count=request.sample_count,
        tags={
            "dwell_request_id": request.request_id,
            "dwell_request_schema": f"{request.schema.schema_id}/{request.schema.version}",
            "source_scan_result_id": request.source.result_id,
            "source_recording_id": str(request.source.recording_id),
            "source_feature_set_id": str(request.source.feature_set_ref.feature_set_id),
            "reason_code": request.reason_code,
            "evidence_digests": evidence_digests,
            "tx": "prohibited",
        },
    )
    return CapturePlan(
        schema=SchemaRef(CapturePlan.SCHEMA_ID),
        plan_id=PlanId(f"plan_{request.request_id}"),
        radio_id=request.radio_id,
        receiver_chain_ids=policy.receiver_chain_ids,
        activities=(
            ActivityRequest(
                ActivityId(f"act_{request.request_id}"),
                ActivityKind.DWELL,
                (segment,),
            ),
        ),
        experiment_tags=(
            ("dwell_idempotency_key", request.idempotency_key),
            ("dwell_request_digest", str(canonical_digest(request))),
            ("dwell_request_id", request.request_id),
            ("station_id", str(request.station_id)),
        ),
    )
