# Release qualification evidence

Release qualification is an evidence review, not an automated deployment.
Construct one `ReleaseCandidateManifest` only after immutable configuration,
dependency lock, and hardware snapshot artifacts exist. Each gate tool must be
reviewed and pinned as the corresponding `verifier_ref` before measurements
begin. A receipt from a different verifier or candidate is intentionally
unusable.

The current release policy requires exactly these receipts:

| Gate | Bound enforced by the aggregator |
|---|---|
| Hardware qualification | Exact candidate/verifier, fresh pass and evidence |
| Provider canary | Exact candidate/verifier, fresh pass and evidence |
| Scientific promotion | Recorded locked-set pass for the exact candidate |
| Soak | At least 8 hours and no more than 24 hours |
| Restore | Fresh, exact restore-drill pass and evidence |
| Scale load | Measured scale at least 2.0 |
| Capacity | Fresh, exact capacity-exercise pass and evidence |
| Outage recovery | Fresh, exact outage-exercise pass and evidence |
| Canary parity | Fresh, exact old/new comparison pass and evidence |

Every receipt contains a non-empty measured interval, metrics, immutable
evidence object references, and operator provenance. Use CAS references for
logs, reports, query output, and measurement series; do not turn receipt files
into a queue or NFS marker protocol. The receipt digest remains stable if CAS
content is relocated, but changes if the evidence content or metadata changes.

Call `evaluate_release_candidate` with a trusted evaluation UTC timestamp and
the complete receipt set. `qualified=True` means only that the supplied
evidence satisfies the candidate-pinned policy. The returned decision is
deterministically hashed; the function does not deploy, mutate PostgreSQL, or
switch a `ModelRelease` alias. Preserve the manifest, receipts, decision, and
referenced evidence through the normal immutable catalog/object boundaries.

As of 2026-08-13 there is no operationally qualified candidate. Hardware soak,
provider, locked-science, restore, load, capacity/outage, and parity evidence
bound to one exact candidate is absent. Do not use synthetic unit-test fixtures
as release evidence.
