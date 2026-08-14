from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.application.release_qualification import evaluate_release_candidate
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    HardwareSnapshotId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.hardware import HardwareMetadataSnapshotRef
from leo_flow.contracts.release import (
    CURRENT_RELEASE_GATES,
    OperatorProvenance,
    QualificationMetric,
    ReleaseCandidateManifest,
    ReleaseGate,
    ReleaseGatePolicy,
    ReleaseGateReceipt,
    ReleaseGateRequirement,
)
from leo_flow.contracts.storage import ObjectRef

HOUR_NS = 60 * 60 * 1_000_000_000
DAY_NS = 24 * HOUR_NS
CREATED_NS = UtcNs(1_800_000_000_000_000_000)
EVALUATED_NS = UtcNs(int(CREATED_NS) + 3 * DAY_NS)


def digest(seed: str) -> Digest:
    return Digest.sha256(seed.encode())


def artifact(name: str) -> ArtifactRef:
    return ArtifactRef(name, digest(name))


def policy() -> ReleaseGatePolicy:
    requirements = []
    for gate in CURRENT_RELEASE_GATES:
        requirements.append(
            ReleaseGateRequirement(
                gate=gate,
                verifier_ref=artifact(f"verify-{gate.value}"),
                minimum_duration_ns=(8 * HOUR_NS if gate is ReleaseGate.SOAK else 1),
                maximum_duration_ns=(
                    24 * HOUR_NS if gate is ReleaseGate.SOAK else None
                ),
                minimum_scale=(2.0 if gate is ReleaseGate.SCALE_LOAD else 1.0),
                maximum_evidence_age_ns=7 * DAY_NS,
            )
        )
    return ReleaseGatePolicy(
        SchemaRef(ReleaseGatePolicy.SCHEMA_ID, V0_1), tuple(requirements)
    )


def candidate() -> ReleaseCandidateManifest:
    return ReleaseCandidateManifest(
        schema=SchemaRef(ReleaseCandidateManifest.SCHEMA_ID, V0_1),
        candidate_id="release-2026.08.13.1",
        created_utc_ns=CREATED_NS,
        git_commit="a" * 40,
        config_ref=artifact("production-config-v1"),
        dependency_lock_ref=artifact("uv-lock-v1"),
        hardware_refs=(
            HardwareMetadataSnapshotRef(
                HardwareSnapshotId("hw_v5-production"), digest("hardware-v5")
            ),
        ),
        gate_policy=policy(),
    )


def evidence(gate: ReleaseGate, *, locator: str | None = None) -> ObjectRef:
    return ObjectRef(
        digest=digest(f"evidence-{gate.value}"),
        byte_count=123,
        media_type="application/json",
        format_id="leo-release-evidence-v1",
        locator=locator or f"cas://evidence-{gate.value}",
    )


def receipt(
    manifest: ReleaseCandidateManifest, gate: ReleaseGate
) -> ReleaseGateReceipt:
    duration = 8 * HOUR_NS if gate is ReleaseGate.SOAK else HOUR_NS
    scale = 2.0 if gate is ReleaseGate.SCALE_LOAD else 1.0
    start = UtcNs(int(CREATED_NS) + HOUR_NS)
    end = UtcNs(int(start) + duration)
    return ReleaseGateReceipt(
        schema=SchemaRef(ReleaseGateReceipt.SCHEMA_ID, V0_1),
        candidate_digest=manifest.identity_digest(),
        gate=gate,
        verifier_ref=manifest.gate_policy.requirement_for(gate).verifier_ref,
        measured_start_utc_ns=start,
        measured_end_utc_ns=end,
        measured_scale=scale,
        passed=True,
        metrics=(QualificationMetric("observations", 1.0, "count"),),
        evidence_refs=(evidence(gate),),
        operator=OperatorProvenance("operator-1", end, "qualification-host-1"),
    )


def all_receipts(manifest: ReleaseCandidateManifest) -> tuple[ReleaseGateReceipt, ...]:
    return tuple(receipt(manifest, gate) for gate in CURRENT_RELEASE_GATES)


def test_complete_exact_evidence_qualifies_deterministically() -> None:
    manifest = candidate()
    receipts = all_receipts(manifest)

    first = evaluate_release_candidate(
        manifest, tuple(reversed(receipts)), evaluated_utc_ns=EVALUATED_NS
    )
    second = evaluate_release_candidate(
        manifest, receipts, evaluated_utc_ns=EVALUATED_NS
    )

    assert first.qualified
    assert first.reasons == ()
    assert first.decision_digest == second.decision_digest
    assert first.receipt_digests == second.receipt_digests


@pytest.mark.parametrize("missing_gate", CURRENT_RELEASE_GATES)
def test_every_current_acceptance_gate_is_required(missing_gate: ReleaseGate) -> None:
    manifest = candidate()
    receipts = tuple(
        item for item in all_receipts(manifest) if item.gate is not missing_gate
    )

    decision = evaluate_release_candidate(
        manifest, receipts, evaluated_utc_ns=EVALUATED_NS
    )

    assert not decision.qualified
    assert f"missing:{missing_gate.value}" in decision.reasons


def test_policy_cannot_omit_reorder_or_weaken_current_requirements() -> None:
    current = policy()
    with pytest.raises(ValueError, match="every current gate"):
        ReleaseGatePolicy(current.schema, current.requirements[:-1])
    with pytest.raises(ValueError, match="canonical order"):
        ReleaseGatePolicy(current.schema, tuple(reversed(current.requirements)))

    soak_index = CURRENT_RELEASE_GATES.index(ReleaseGate.SOAK)
    weak_soak = replace(current.requirements[soak_index], minimum_duration_ns=1)
    with pytest.raises(ValueError, match="at least 8 hours"):
        ReleaseGatePolicy(
            current.schema,
            current.requirements[:soak_index]
            + (weak_soak,)
            + current.requirements[soak_index + 1 :],
        )

    load_index = CURRENT_RELEASE_GATES.index(ReleaseGate.SCALE_LOAD)
    weak_load = replace(current.requirements[load_index], minimum_scale=1.99)
    with pytest.raises(ValueError, match="at least 2x"):
        ReleaseGatePolicy(
            current.schema,
            current.requirements[:load_index]
            + (weak_load,)
            + current.requirements[load_index + 1 :],
        )


def test_duplicate_gate_fails_closed_even_when_both_receipts_pass() -> None:
    manifest = candidate()
    receipts = all_receipts(manifest) + (receipt(manifest, ReleaseGate.RESTORE),)
    decision = evaluate_release_candidate(
        manifest, receipts, evaluated_utc_ns=EVALUATED_NS
    )
    assert not decision.qualified
    assert "duplicate:restore" in decision.reasons


@pytest.mark.parametrize(
    ("gate", "mutation", "reason"),
    [
        (
            ReleaseGate.SOAK,
            {"measured_end_utc_ns": UtcNs(int(CREATED_NS) + 2 * HOUR_NS)},
            "too_short:soak",
        ),
        (
            ReleaseGate.SOAK,
            {
                "measured_end_utc_ns": UtcNs(int(CREATED_NS) + 26 * HOUR_NS),
                "operator": OperatorProvenance(
                    "operator-1",
                    UtcNs(int(CREATED_NS) + 26 * HOUR_NS),
                    "qualification-host-1",
                ),
            },
            "too_long:soak",
        ),
        (ReleaseGate.SCALE_LOAD, {"measured_scale": 1.99}, "wrong_scale:scale_load"),
        (ReleaseGate.RESTORE, {"passed": False}, "failed:restore"),
        (
            ReleaseGate.CAPACITY,
            {"verifier_ref": artifact("substitute-verifier")},
            "verifier_mismatch:capacity",
        ),
    ],
)
def test_quantitative_failed_and_verifier_substitutions_do_not_pass(
    gate: ReleaseGate, mutation: dict[str, object], reason: str
) -> None:
    manifest = candidate()
    target = replace(receipt(manifest, gate), **mutation)
    receipts = tuple(
        target if item.gate is gate else item for item in all_receipts(manifest)
    )
    decision = evaluate_release_candidate(
        manifest, receipts, evaluated_utc_ns=EVALUATED_NS
    )
    assert not decision.qualified
    assert reason in decision.reasons


def test_stale_and_other_candidate_evidence_do_not_pass() -> None:
    manifest = candidate()
    other = replace(manifest, git_commit="b" * 40)
    stale_eval = UtcNs(int(EVALUATED_NS) + 8 * DAY_NS)
    mismatched = replace(
        receipt(manifest, ReleaseGate.CANARY_PARITY),
        candidate_digest=other.identity_digest(),
    )
    receipts = tuple(
        mismatched if item.gate is ReleaseGate.CANARY_PARITY else item
        for item in all_receipts(manifest)
    )

    mismatch_decision = evaluate_release_candidate(
        manifest, receipts, evaluated_utc_ns=EVALUATED_NS
    )
    stale_decision = evaluate_release_candidate(
        manifest, all_receipts(manifest), evaluated_utc_ns=stale_eval
    )

    assert "candidate_mismatch:canary_parity" in mismatch_decision.reasons
    assert not stale_decision.qualified
    assert all(reason.startswith("stale:") for reason in stale_decision.reasons)


def test_decision_digest_closes_over_rejected_receipts_and_ignores_input_order() -> (
    None
):
    manifest = candidate()
    base = all_receipts(manifest)
    wrong_a = replace(
        receipt(manifest, ReleaseGate.CAPACITY),
        verifier_ref=artifact("wrong-verifier-a"),
    )
    wrong_b = replace(wrong_a, verifier_ref=artifact("wrong-verifier-b"))
    receipts_a = tuple(
        wrong_a if item.gate is ReleaseGate.CAPACITY else item for item in base
    )
    receipts_b = tuple(
        wrong_b if item.gate is ReleaseGate.CAPACITY else item for item in base
    )

    decision_a = evaluate_release_candidate(
        manifest, receipts_a, evaluated_utc_ns=EVALUATED_NS
    )
    reordered_a = evaluate_release_candidate(
        manifest, tuple(reversed(receipts_a)), evaluated_utc_ns=EVALUATED_NS
    )
    decision_b = evaluate_release_candidate(
        manifest, receipts_b, evaluated_utc_ns=EVALUATED_NS
    )

    assert decision_a.reasons == decision_b.reasons == ("verifier_mismatch:capacity",)
    assert decision_a.decision_digest == reordered_a.decision_digest
    assert decision_a.decision_digest != decision_b.decision_digest
    assert decision_a.receipt_digests != decision_b.receipt_digests


def test_receipt_digest_survives_evidence_relocation_but_not_evidence_change() -> None:
    manifest = candidate()
    original = receipt(manifest, ReleaseGate.RESTORE)
    relocated = replace(
        original,
        evidence_refs=(evidence(ReleaseGate.RESTORE, locator="s3://relocated/key"),),
    )
    changed_ref = replace(
        evidence(ReleaseGate.RESTORE), digest=digest("different-restore-evidence")
    )
    changed = replace(original, evidence_refs=(changed_ref,))

    assert original.identity_digest() == relocated.identity_digest()
    assert original.identity_digest() != changed.identity_digest()


def test_receipt_requires_metrics_evidence_and_bounded_interval() -> None:
    manifest = candidate()
    valid = receipt(manifest, ReleaseGate.OUTAGE_RECOVERY)
    with pytest.raises(ValueError, match="measured metrics"):
        replace(valid, metrics=())
    with pytest.raises(ValueError, match="immutable evidence"):
        replace(valid, evidence_refs=())
    with pytest.raises(ValueError, match="non-empty"):
        replace(valid, measured_start_utc_ns=valid.measured_end_utc_ns)
    with pytest.raises(TypeError, match="passed must be boolean"):
        replace(valid, passed=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="objects must be non-empty"):
        replace(valid, evidence_refs=(replace(valid.evidence_refs[0], byte_count=0),))


def test_gate_values_reject_untyped_substitutions() -> None:
    current = policy()
    with pytest.raises(TypeError, match="ReleaseGate"):
        replace(current.requirements[0], gate="hardware_qualification")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ReleaseGate"):
        replace(
            receipt(candidate(), ReleaseGate.HARDWARE_QUALIFICATION),
            gate="hardware_qualification",  # type: ignore[arg-type]
        )
