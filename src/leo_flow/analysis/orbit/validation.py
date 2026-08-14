"""Deterministic confusion-style reporting for RF association experiments."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from leo_flow.contracts.core import ArtifactRef, Digest, canonical_digest

from .association import (
    AssociationDecision,
    AssociationStatus,
    OrbitPropagator,
    RfAssociationRequest,
    associate_rf_measurement,
)


class ValidationOutcome(str, Enum):
    MATCH = "match"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"
    BELOW_ELEVATION = "below_elevation"
    PROPAGATION_ERROR = "propagation_error"


@dataclass(frozen=True)
class AssociationValidationCase:
    case_id: str
    request: RfAssociationRequest
    expected_outcome: ValidationOutcome
    expected_norad_id: int | None = None

    def __post_init__(self) -> None:
        if not self.case_id or any(character.isspace() for character in self.case_id):
            raise ValueError("validation case_id must be a token")
        if self.expected_outcome is ValidationOutcome.MATCH:
            if self.expected_norad_id is None or self.expected_norad_id <= 0:
                raise ValueError("a match expectation requires a positive NORAD ID")
        elif self.expected_norad_id is not None:
            raise ValueError("only a match expectation can name a NORAD ID")


@dataclass(frozen=True)
class ValidationResult:
    case_id: str
    expected_outcome: ValidationOutcome
    observed_outcome: ValidationOutcome
    expected_norad_id: int | None
    observed_norad_id: int | None
    passed: bool
    request_digest: Digest
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ConfusionCell:
    expected: ValidationOutcome
    observed: ValidationOutcome
    count: int

    def __post_init__(self) -> None:
        if self.count <= 0:
            raise ValueError("confusion cell count must be positive")


@dataclass(frozen=True)
class AssociationValidationReport:
    experiment_ref: ArtifactRef
    results: tuple[ValidationResult, ...]
    confusion: tuple[ConfusionCell, ...]
    passed_count: int
    total_count: int
    report_digest: Digest

    def __post_init__(self) -> None:
        if self.total_count != len(self.results):
            raise ValueError("validation total differs from result count")
        if self.passed_count != sum(result.passed for result in self.results):
            raise ValueError("validation passed count differs")
        if len({result.case_id for result in self.results}) != len(self.results):
            raise ValueError("validation result case IDs must be unique")
        cell_keys = {(cell.expected, cell.observed) for cell in self.confusion}
        if len(cell_keys) != len(self.confusion):
            raise ValueError("validation confusion cells must be unique")
        if sum(cell.count for cell in self.confusion) != self.total_count:
            raise ValueError("validation confusion count differs")
        expected_digest = canonical_digest(
            {
                "experiment_ref": self.experiment_ref,
                "results": self.results,
                "confusion": self.confusion,
            }
        )
        if self.report_digest != expected_digest:
            raise ValueError("validation report digest differs")

    def to_document(self) -> dict[str, object]:
        """Return a compact, stable document suitable for experiment artifacts."""

        return {
            "experiment_ref": {
                "artifact_id": self.experiment_ref.artifact_id,
                "digest": str(self.experiment_ref.digest),
            },
            "truth_scope": "exact_digital_injection_not_observational_tle_truth",
            "passed_count": self.passed_count,
            "total_count": self.total_count,
            "confusion": [
                {
                    "expected": cell.expected.value,
                    "observed": cell.observed.value,
                    "count": cell.count,
                }
                for cell in self.confusion
            ],
            "results": [
                {
                    "case_id": result.case_id,
                    "expected": result.expected_outcome.value,
                    "observed": result.observed_outcome.value,
                    "expected_norad_id": result.expected_norad_id,
                    "observed_norad_id": result.observed_norad_id,
                    "passed": result.passed,
                    "request_digest": str(result.request_digest),
                    "reason_codes": list(result.reason_codes),
                }
                for result in self.results
            ],
            "report_digest": str(self.report_digest),
        }


def run_association_validation(
    experiment_ref: ArtifactRef,
    cases: tuple[AssociationValidationCase, ...],
    propagator: OrbitPropagator,
) -> AssociationValidationReport:
    """Evaluate ordered cases without treating model association as truth."""

    if not cases:
        raise ValueError("association validation requires at least one case")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("association validation case IDs must be unique")

    results: list[ValidationResult] = []
    counts: dict[tuple[ValidationOutcome, ValidationOutcome], int] = {}
    for case in cases:
        decision = associate_rf_measurement(case.request, propagator)
        observed = _classify(decision)
        passed = observed is case.expected_outcome and (
            case.expected_norad_id is None
            or decision.selected_norad_id == case.expected_norad_id
        )
        result = ValidationResult(
            case.case_id,
            case.expected_outcome,
            observed,
            case.expected_norad_id,
            decision.selected_norad_id,
            passed,
            decision.request_digest,
            decision.reason_codes,
        )
        results.append(result)
        key = case.expected_outcome, observed
        counts[key] = counts.get(key, 0) + 1

    confusion = tuple(
        ConfusionCell(expected, observed, count)
        for (expected, observed), count in sorted(
            counts.items(), key=lambda item: (item[0][0].value, item[0][1].value)
        )
    )
    identity = {
        "experiment_ref": experiment_ref,
        "results": tuple(results),
        "confusion": confusion,
    }
    return AssociationValidationReport(
        experiment_ref,
        tuple(results),
        confusion,
        sum(result.passed for result in results),
        len(results),
        canonical_digest(identity),
    )


def _classify(decision: AssociationDecision) -> ValidationOutcome:
    if decision.status is AssociationStatus.MATCH:
        return ValidationOutcome.MATCH
    if decision.status is AssociationStatus.AMBIGUOUS:
        return ValidationOutcome.AMBIGUOUS
    if decision.reason_codes and all(
        reason.startswith("below-elevation-gate:") for reason in decision.reason_codes
    ):
        return ValidationOutcome.BELOW_ELEVATION
    if decision.reason_codes and all(
        reason.startswith("propagation-error:") for reason in decision.reason_codes
    ):
        return ValidationOutcome.PROPAGATION_ERROR
    return ValidationOutcome.NO_MATCH
