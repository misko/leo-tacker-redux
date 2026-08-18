"""Frozen inputs assembled from durable adaptive response/QAM evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ._validation import require_token
from .core import V0_1, Digest, SchemaRef, canonical_digest
from .starlink_adaptive_calibration import (
    AdaptiveCalibrationDwellV0_1,
    AdaptiveCalibrationLabel,
    AdaptiveCalibrationSplit,
)
from .starlink_detector_suite import StarlinkDetectorMethod


class AdaptiveCalibrationEvidencePurpose(str, Enum):
    CALIBRATION = "frozen-calibration-member"
    CONDITIONED_POSITIVE = "conditioned-positive-plumbing-only"


@dataclass(frozen=True)
class AdaptiveCalibrationAssemblySpecV0_1:
    """Precommitted identity and membership for one offline dwell assembly."""

    schema: SchemaRef
    dwell_id: str
    member_digest: Digest
    group_digest: Digest
    split: AdaptiveCalibrationSplit
    split_manifest_digest: Digest
    label: AdaptiveCalibrationLabel
    cell_identity_digest: Digest
    method: StarlinkDetectorMethod
    response_bundle_digest: Digest
    qam_bundle_digest: Digest | None
    search_identity_digest: Digest
    receiver_identities: tuple[tuple[str, str], ...]
    pattern_template_digests: tuple[Digest, ...]
    purpose: AdaptiveCalibrationEvidencePurpose
    score_correction: str = "none"

    SCHEMA_ID = "org.leo-flow.adaptive-calibration-assembly-spec"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported adaptive calibration assembly schema")
        require_token(self.dwell_id, "dwell_id")
        if (
            not self.receiver_identities
            or self.receiver_identities
            != tuple(sorted(set(self.receiver_identities)))
            or not self.pattern_template_digests
            or len(self.pattern_template_digests)
            != len(set(self.pattern_template_digests))
        ):
            raise ValueError("adaptive calibration membership is noncanonical")
        if self.score_correction != "none":
            raise ValueError("adaptive calibration forbids label-derived correction")
        if (
            self.purpose is AdaptiveCalibrationEvidencePurpose.CONDITIONED_POSITIVE
            and self.label is not AdaptiveCalibrationLabel.POSITIVE
        ):
            raise ValueError("conditioned plumbing must be positive-labelled")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class AssembledAdaptiveCalibrationInputV0_1:
    """A calibration dwell with immutable closure over its durable sources."""

    schema: SchemaRef
    assembly_spec_digest: Digest
    split_manifest_digest: Digest
    response_bundle_digest: Digest
    qam_bundle_digest: Digest | None
    search_identity_digest: Digest
    pattern_template_digests: tuple[Digest, ...]
    dwell: AdaptiveCalibrationDwellV0_1
    complete_search_axes: tuple[str, ...]
    score_correction: str
    candidate_only: bool

    SCHEMA_ID = "org.leo-flow.assembled-adaptive-calibration-input"

    def __post_init__(self) -> None:
        if (
            self.schema != SchemaRef(self.SCHEMA_ID, V0_1)
            or self.complete_search_axes
            != ("declared-time-windows", "coarse-cfo", "residual-cfo", "epoch")
            or self.score_correction != "none"
            or not self.candidate_only
            or not self.pattern_template_digests
            or len(self.pattern_template_digests) != len(self.dwell.patterns)
        ):
            raise ValueError("assembled adaptive calibration input is invalid")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class AdaptiveConditionedPositivePlumbingV0_1:
    """Known-positive diagnostic deliberately incompatible with calibration input."""

    schema: SchemaRef
    fixture_id: str
    fixture_digest: Digest
    receiver_qam_goodness: tuple[float, ...]
    purpose: AdaptiveCalibrationEvidencePurpose
    eligible_for_calibration: bool

    SCHEMA_ID = "org.leo-flow.adaptive-conditioned-positive-plumbing"

    def __post_init__(self) -> None:
        if (
            self.schema != SchemaRef(self.SCHEMA_ID, V0_1)
            or self.purpose
            is not AdaptiveCalibrationEvidencePurpose.CONDITIONED_POSITIVE
            or self.eligible_for_calibration
            or not self.receiver_qam_goodness
            or any(not 0 <= value <= 1 for value in self.receiver_qam_goodness)
        ):
            raise ValueError("conditioned positive cannot become calibration evidence")
        require_token(self.fixture_id, "fixture_id")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)
