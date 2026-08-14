# ADR 0033: Fail-closed release qualification evidence

- Status: accepted
- Date: 2026-08-13

## Decision

A release candidate is an immutable manifest that pins an exact Git object,
normalized deployment configuration artifact, dependency-lock artifact,
hardware metadata snapshots, and gate policy. The current policy always
contains, in canonical order, hardware qualification, provider canary, locked
scientific promotion, 8–24-hour soak, restore, at-least-2x load, capacity,
outage recovery, and old/new canary parity. A policy cannot omit, reorder, or
weaken those gates.

Each gate produces one bounded receipt tied to the candidate digest and the
exact verifier artifact named by the policy. A receipt records its measured
interval and scale, pass/fail outcome, typed metrics, immutable evidence object
identities, and operator/time/host provenance. Its identity excludes a
replaceable object locator but includes the evidence digest, size, media type,
and format.

A pure aggregator fails closed on a missing or duplicate gate, candidate or
verifier substitution, stale/future/predating evidence, a short or excessively
long interval, insufficient load scale, a failed result, or future operator
provenance. It emits a canonical promotion decision digest that closes over
the complete supplied receipt multiset, including rejected and duplicate
receipts, in order-independent canonical order. Evaluation does not update a
release alias or perform deployment.

## Boundaries

This is a contract and deterministic library function, not a workflow engine.
It executes no commands, schedules no work, owns no runtime service or
database, reads no paths, writes no NFS markers, and does not sign or notarize
evidence. Gate-specific tools remain independently operated and publish their
numeric evidence into immutable object storage through existing adapters.
Signing/notarization may be added later only with a separately reviewed trust
and key-management design.

## Current qualification state

No candidate is promoted by this ADR. Current production evidence is absent:

- the V5 radio is a functional canary, but the tested IP path dropped source
  frames and has no qualifying 8–24-hour continuous hardware soak;
- no archived live provider-canary receipt is bound to a release candidate;
- the locked scientific promotion set remains deliberately unopened/pending
  stronger ground truth;
- restore, 2x-scale load, capacity/outage, and old/new parity receipts have not
  been produced for one exact candidate.

Consequently an evaluation of current evidence must return unqualified. Unit
fixtures demonstrating a passing aggregate are synthetic contract tests, not
operational evidence.

## Consequences

Evidence can be collected by different operators and tools without sharing a
mutable control plane. Exact candidate and verifier binding prevents a result
from another commit, configuration, dependency set, hardware epoch, or test
implementation from satisfying a gate. Operators still need a controlled
artifact publication procedure and eventually an explicit signing decision.

## Verification

Focused tests require every current gate and prove deterministic ordering,
minimum soak/load policy, failure on duplicate/stale/mismatched/failed
receipts, and evidence relocation without identity drift. Full operational
qualification remains a deployment exercise on the named hardware and corpus.
