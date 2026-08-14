"""Immutable release-candidate and qualification-evidence contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ._validation import require_finite, require_positive, require_token, require_utc_ns
from .core import (
    V0_1,
    ArtifactRef,
    Digest,
    SchemaRef,
    UtcNs,
    canonical_digest,
)
from .hardware import HardwareMetadataSnapshotRef
from .storage import ObjectRef


class ReleaseGate(str, Enum):
    """The plan's independently evidenced promotion prerequisites."""

    HARDWARE_QUALIFICATION = "hardware_qualification"
    PROVIDER_CANARY = "provider_canary"
    SCIENTIFIC_PROMOTION = "scientific_promotion"
    SOAK = "soak"
    RESTORE = "restore"
    SCALE_LOAD = "scale_load"
    CAPACITY = "capacity"
    OUTAGE_RECOVERY = "outage_recovery"
    CANARY_PARITY = "canary_parity"


CURRENT_RELEASE_GATES = tuple(ReleaseGate)


@dataclass(frozen=True)
class ReleaseGateRequirement:
    """Candidate-pinned verifier and quantitative bounds for one gate."""

    gate: ReleaseGate
    verifier_ref: ArtifactRef
    minimum_duration_ns: int
    maximum_duration_ns: int | None
    minimum_scale: float
    maximum_evidence_age_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.gate, ReleaseGate):
            raise TypeError("gate must be a ReleaseGate")
        require_utc_ns(self.minimum_duration_ns, "minimum_duration_ns")
        if self.maximum_duration_ns is not None:
            require_positive(self.maximum_duration_ns, "maximum_duration_ns")
            if self.maximum_duration_ns < self.minimum_duration_ns:
                raise ValueError("maximum duration is below minimum duration")
        require_positive(self.minimum_scale, "minimum_scale")
        require_positive(self.maximum_evidence_age_ns, "maximum_evidence_age_ns")


@dataclass(frozen=True)
class ReleaseGatePolicy:
    """Complete, ordered policy; the current acceptance set cannot be weakened."""

    schema: SchemaRef
    requirements: tuple[ReleaseGateRequirement, ...]

    SCHEMA_ID = "org.leo-flow.release-gate-policy"
    MINIMUM_SOAK_NS = 8 * 60 * 60 * 1_000_000_000
    MAXIMUM_SOAK_NS = 24 * 60 * 60 * 1_000_000_000
    MINIMUM_LOAD_SCALE = 2.0

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported release gate policy schema")
        gates = tuple(item.gate for item in self.requirements)
        if gates != CURRENT_RELEASE_GATES:
            raise ValueError(
                "policy must contain every current gate in canonical order"
            )
        soak = self.requirement_for(ReleaseGate.SOAK)
        if soak.minimum_duration_ns < self.MINIMUM_SOAK_NS:
            raise ValueError("release soak must be at least 8 hours")
        if (
            soak.maximum_duration_ns is None
            or soak.maximum_duration_ns > self.MAXIMUM_SOAK_NS
        ):
            raise ValueError("release soak must be bounded by 24 hours")
        load = self.requirement_for(ReleaseGate.SCALE_LOAD)
        if load.minimum_scale < self.MINIMUM_LOAD_SCALE:
            raise ValueError(
                "release load qualification must require at least 2x scale"
            )

    def requirement_for(self, gate: ReleaseGate) -> ReleaseGateRequirement:
        for requirement in self.requirements:
            if requirement.gate is gate:
                return requirement
        raise ValueError(f"missing release gate requirement: {gate.value}")

    def identity_digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class ReleaseCandidateManifest:
    """Exact immutable identity of a deployment candidate and its gate policy."""

    schema: SchemaRef
    candidate_id: str
    created_utc_ns: UtcNs
    git_commit: str
    config_ref: ArtifactRef
    dependency_lock_ref: ArtifactRef
    hardware_refs: tuple[HardwareMetadataSnapshotRef, ...]
    gate_policy: ReleaseGatePolicy

    SCHEMA_ID = "org.leo-flow.release-candidate-manifest"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported release candidate schema")
        require_token(self.candidate_id, "candidate_id")
        require_utc_ns(self.created_utc_ns, "created_utc_ns")
        if re.fullmatch(r"[0-9a-f]{40}", self.git_commit) is None:
            raise ValueError("git_commit must be an exact lowercase 40-hex object ID")
        if not self.hardware_refs:
            raise ValueError(
                "release candidate must pin at least one hardware snapshot"
            )
        if len(set(self.hardware_refs)) != len(self.hardware_refs):
            raise ValueError("hardware snapshot references must be unique")

    def identity_digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class QualificationMetric:
    name: str
    value: float
    unit: str

    def __post_init__(self) -> None:
        require_token(self.name, "metric name")
        require_finite(self.value, "metric value")
        require_token(self.unit, "metric unit")


@dataclass(frozen=True)
class OperatorProvenance:
    operator_id: str
    recorded_utc_ns: UtcNs
    host_id: str

    def __post_init__(self) -> None:
        require_token(self.operator_id, "operator_id")
        require_utc_ns(self.recorded_utc_ns, "recorded_utc_ns")
        require_token(self.host_id, "host_id")


@dataclass(frozen=True)
class ReleaseGateReceipt:
    """Bounded evidence for exactly one gate on exactly one candidate."""

    schema: SchemaRef
    candidate_digest: Digest
    gate: ReleaseGate
    verifier_ref: ArtifactRef
    measured_start_utc_ns: UtcNs
    measured_end_utc_ns: UtcNs
    measured_scale: float
    passed: bool
    metrics: tuple[QualificationMetric, ...]
    evidence_refs: tuple[ObjectRef, ...]
    operator: OperatorProvenance

    SCHEMA_ID = "org.leo-flow.release-gate-receipt"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported release gate receipt schema")
        if not isinstance(self.gate, ReleaseGate):
            raise TypeError("gate must be a ReleaseGate")
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be boolean")
        require_utc_ns(self.measured_start_utc_ns, "measured_start_utc_ns")
        require_utc_ns(self.measured_end_utc_ns, "measured_end_utc_ns")
        if self.measured_end_utc_ns <= self.measured_start_utc_ns:
            raise ValueError("measured interval must be non-empty")
        require_positive(self.measured_scale, "measured_scale")
        if self.operator.recorded_utc_ns < self.measured_end_utc_ns:
            raise ValueError(
                "operator provenance cannot predate measurement completion"
            )
        metric_names = tuple(metric.name for metric in self.metrics)
        if not self.metrics:
            raise ValueError("gate receipt must contain measured metrics")
        if len(set(metric_names)) != len(metric_names):
            raise ValueError("qualification metric names must be unique")
        if not self.evidence_refs:
            raise ValueError("gate receipt must reference immutable evidence")
        if any(ref.byte_count == 0 for ref in self.evidence_refs):
            raise ValueError("gate evidence objects must be non-empty")
        evidence_digests = tuple(ref.digest for ref in self.evidence_refs)
        if len(set(evidence_digests)) != len(evidence_digests):
            raise ValueError("gate evidence digests must be unique")

    @property
    def measured_duration_ns(self) -> int:
        return int(self.measured_end_utc_ns) - int(self.measured_start_utc_ns)

    def identity_digest(self) -> Digest:
        """Scientific receipt identity excludes replaceable object locators."""
        return canonical_digest(
            {
                "schema": self.schema,
                "candidate_digest": self.candidate_digest,
                "gate": self.gate,
                "verifier_ref": self.verifier_ref,
                "measured_start_utc_ns": self.measured_start_utc_ns,
                "measured_end_utc_ns": self.measured_end_utc_ns,
                "measured_scale": self.measured_scale,
                "passed": self.passed,
                "metrics": self.metrics,
                "evidence": tuple(
                    {
                        "digest": ref.digest,
                        "byte_count": ref.byte_count,
                        "media_type": ref.media_type,
                        "format_id": ref.format_id,
                    }
                    for ref in self.evidence_refs
                ),
                "operator": self.operator,
            }
        )
