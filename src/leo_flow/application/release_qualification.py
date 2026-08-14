"""Pure fail-closed aggregation of immutable release qualification evidence."""

from __future__ import annotations

from dataclasses import dataclass

from leo_flow.contracts._validation import require_utc_ns
from leo_flow.contracts.core import Digest, UtcNs, canonical_digest
from leo_flow.contracts.release import (
    CURRENT_RELEASE_GATES,
    ReleaseCandidateManifest,
    ReleaseGate,
    ReleaseGateReceipt,
)


@dataclass(frozen=True)
class PromotionDecision:
    candidate_digest: Digest
    evaluated_utc_ns: UtcNs
    qualified: bool
    reasons: tuple[str, ...]
    receipt_digests: tuple[Digest, ...]
    decision_digest: Digest


def evaluate_release_candidate(
    candidate: ReleaseCandidateManifest,
    receipts: tuple[ReleaseGateReceipt, ...],
    *,
    evaluated_utc_ns: UtcNs,
) -> PromotionDecision:
    """Evaluate evidence only; this function cannot publish or promote a release."""
    require_utc_ns(evaluated_utc_ns, "evaluated_utc_ns")
    candidate_digest = candidate.identity_digest()
    reasons: list[str] = []
    by_gate: dict[ReleaseGate, list[ReleaseGateReceipt]] = {}
    for receipt in receipts:
        by_gate.setdefault(receipt.gate, []).append(receipt)

    if candidate.created_utc_ns > evaluated_utc_ns:
        reasons.append("candidate_created_in_future")
    for gate in CURRENT_RELEASE_GATES:
        matching = by_gate.get(gate, [])
        if not matching:
            reasons.append(f"missing:{gate.value}")
            continue
        if len(matching) != 1:
            reasons.append(f"duplicate:{gate.value}")
            continue
        receipt = matching[0]
        requirement = candidate.gate_policy.requirement_for(gate)
        gate_reasons: list[str] = []
        if receipt.candidate_digest != candidate_digest:
            gate_reasons.append(f"candidate_mismatch:{gate.value}")
        if receipt.verifier_ref != requirement.verifier_ref:
            gate_reasons.append(f"verifier_mismatch:{gate.value}")
        if receipt.measured_duration_ns < requirement.minimum_duration_ns:
            gate_reasons.append(f"too_short:{gate.value}")
        if (
            requirement.maximum_duration_ns is not None
            and receipt.measured_duration_ns > requirement.maximum_duration_ns
        ):
            gate_reasons.append(f"too_long:{gate.value}")
        if receipt.measured_scale < requirement.minimum_scale:
            gate_reasons.append(f"wrong_scale:{gate.value}")
        if receipt.measured_start_utc_ns < candidate.created_utc_ns:
            gate_reasons.append(f"predates_candidate:{gate.value}")
        if receipt.measured_end_utc_ns > evaluated_utc_ns:
            gate_reasons.append(f"future_evidence:{gate.value}")
        elif (
            int(evaluated_utc_ns) - int(receipt.measured_end_utc_ns)
            > requirement.maximum_evidence_age_ns
        ):
            gate_reasons.append(f"stale:{gate.value}")
        if not receipt.passed:
            gate_reasons.append(f"failed:{gate.value}")
        if receipt.operator.recorded_utc_ns > evaluated_utc_ns:
            gate_reasons.append(f"future_provenance:{gate.value}")
        reasons.extend(gate_reasons)

    # Close the decision over the complete supplied multiset, including rejected
    # and duplicate receipts, while remaining invariant to caller ordering.
    receipt_digests = tuple(
        digest
        for _, digest in sorted(
            ((receipt.gate.value, receipt.identity_digest()) for receipt in receipts),
            key=lambda item: (item[0], str(item[1])),
        )
    )
    reasons_tuple = tuple(reasons)
    qualified = not reasons_tuple
    decision_digest = canonical_digest(
        {
            "candidate_digest": candidate_digest,
            "evaluated_utc_ns": evaluated_utc_ns,
            "qualified": qualified,
            "reasons": reasons_tuple,
            "receipt_digests": receipt_digests,
        }
    )
    return PromotionDecision(
        candidate_digest=candidate_digest,
        evaluated_utc_ns=evaluated_utc_ns,
        qualified=qualified,
        reasons=reasons_tuple,
        receipt_digests=receipt_digests,
        decision_digest=decision_digest,
    )
