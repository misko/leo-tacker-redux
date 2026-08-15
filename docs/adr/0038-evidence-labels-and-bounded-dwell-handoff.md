# ADR 0038: Evidence labels and bounded dwell handoff

- Status: Accepted for the v0.1 development contract
- Date: 2026-08-14

## Context

Detector evaluation needs labels whose strength and provenance cannot be confused.
The analysis side may also identify a scan result worth observing longer, but it
must not gain access to capture implementations or bypass station-owned safety
policy.

## Decision

The public evidence contract assigns every observation exactly one category:

| Category | Permitted role |
| --- | --- |
| Controlled injection truth | Scored truth when independent of the evaluated method |
| Independently verified observation | Scored truth when independent of the evaluated method |
| Verified negative control | Scored truth when independent of the evaluated method |
| TLE weak association | Context only |
| Operator note | Context only |
| Unlabeled | Context only, with no asserted target or evidence |

Every evidence reference names a versioned immutable artifact, producer, time,
kind, and the methods from which it is independent. Injection labels additionally
identify both the base-recording digest and injection-specification digest.

Dataset partitions are canonical and content-addressed. Explicit leakage groups,
recording identity digests, and injection base-recording digests are indivisible:
none may cross train, validation, or locked-test partitions. A scored member is
rejected unless its label is strong truth independent of the method under test.

The scan-to-dwell handoff has only two public values and two narrow ports:

```text
analysis policy -> ScanResultRef -> DwellRequestEmitter -> DwellRequest
                                                        |
capture policy  <- CapturePlan <- DwellRequestGatePort <-+
```

`DwellRequest` repeats and must exactly match the source station, radio, tuning,
and evidence. It carries an issue time, bounded expiry, reason code, exact sample
count/duration relation, and idempotency key. Contract hard bounds are:

| Field | v0.1 hard maximum |
| --- | ---: |
| Center frequency | 6 GHz |
| Sample rate | 20 MHz |
| Bandwidth | 20 MHz and no greater than sample rate |
| Duration | 60 seconds |
| Samples | 100,000,000 |
| Request lifetime | 300 seconds |

Capture owns a narrower local `DwellSafetyPolicy`, including station/radio,
frequency window, rates, duration, samples, receiver chains, and gain. The gate
rejects stale, misrouted, over-policy, and conflicting idempotency requests before
lowering an accepted request to one declarative `DWELL` capture plan. Exact
replays return the same plan. No detector or analysis implementation is imported
by the capture-side gate.

## Consequences

Weak TLE association and operator interpretation remain useful context but cannot
silently become scored truth. Split leakage is rejected at contract construction,
before training or evaluation. Analysis can propose bounded work but cannot
execute hardware or choose station-owned settings.

This change does not enable automatic live dwell. Durable transport, authorization,
operator enablement, scheduler integration, persistence of idempotency decisions,
and hardware qualification remain deployment work.

## Compatibility and migration

These are new exact v0.1 schemas: `org.leo-flow.label-evidence-ref`,
`org.leo-flow.observation-label`, `org.leo-flow.evidence-partition-plan`,
`org.leo-flow.scan-result-ref`, and `org.leo-flow.dwell-request`. Unknown schema
IDs or versions fail closed. Existing private dataset labels are not implicitly
upgraded; producers must map them explicitly and supply the required provenance.

The new tests construct contract values and exercise the complete handoff with
in-memory fakes. No golden recording fixture changes.
