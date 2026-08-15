# Controlled-truth dataset campaign v1

`leo-flow.dataset-campaign/v1` is the metadata boundary between an independently
specified truth campaign and immutable Recording/FeatureSet results. It extends
the dataset rules in ADR 0005 and the durable snapshot boundary in ADR 0010; it
does not replace either published contract.

The normative executable validator is
`leo_flow.analysis.dataset.campaign`. The structural companion schema is
`benchmark/specs/controlled-truth-dataset-campaign-v1.schema.json`.

## Freeze sequence

1. Name every candidate method before truth construction. Freeze the generator,
   instrument, score-blind null selector, or confounder-characterization
   artifact and its selection policy by SHA-256.
2. Freeze each truth specification and campaign matrix point before producing a
   Recording or FeatureSet. Every provenance record must attest independence
   from every named candidate method; detector-derived truth is rejected.
3. Establish whole correlation groups and half-open capture intervals. Each
   group names one base-noise truth specification, every digital injection made
   from it, and any controlled RF, hard-null, or confounder specifications.
4. Materialize exactly one immutable Recording identity and one immutable
   FeatureSet identity for every truth specification. The base recording and
   every derived injection remain in the same group and partition.
5. Order complete groups by time into `train`, `validation`, then
   `locked_test`. The validator rejects missing partitions, reordered members,
   duplicate IDs or digests, group leakage, and incomplete truth/member closure.

No campaign field is a path, storage root, SQL predicate, URL, “latest” alias,
or detector query. Moving bytes does not change scientific identity, and this
layer never reads those bytes.

## Required campaign matrix

Every truth specification carries all matrix columns, using `null` only where a
dimension does not apply:

| Dimension | Frozen representation |
|---|---|
| SNR | Decimal string `snr_db` |
| Carrier offset | Integer `frequency_offset_hz` |
| Drift | Decimal string `drift_hz_s` |
| Receiver delay | Ordered integer `receiver_delay_samples` |
| Receiver gain | Ordered decimal-string `receiver_gain_db` |
| Clipping | Boolean plus exact integer `clip_min`/`clip_max` when enabled |
| Null | Score-blind `null_class` |
| Confounder | Unique named `confounders` |

Digital injections require SNR, offset, drift, delay, and gain values, pin their
base truth-spec identity, and materialize with the exact base Recording digest
in label evidence. Hard nulls must be independently selected without candidate
scores. Confounders require an independently frozen characterization artifact.

## Ephemeris knowledge boundary

An optional campaign ephemeris input contains only immutable snapshot,
provenance, and selection-policy digests. Its policy must be `available_then`,
and its availability time must not be later than the member capture time. TLE
association remains context, never campaign truth. Mutable provider lookup and
future snapshots are outside this boundary.

## Locked-test opening

Materialization requires one predeclared `evaluated_method_id` and freezes
locked membership, but `members_in(locked_test)` fails closed. A release steward
must provide a campaign-bound `LockedTestOpening` for that same method after
membership freeze. The receipt pins:

- one predeclared method identity and immutable method artifact digest;
- immutable normalized configuration and metric-specification digests;
- at least one explicit metric acceptance bound, with unique metric IDs;
- freeze/open timestamps and steward identity.

The API rejects absent receipts, receipts for another campaign, methods outside
the campaign, bounds frozen before campaign membership, empty bounds, and
inverted bounds. This is a reproducibility and orchestration guard, not an
operating-system confidentiality mechanism; the release steward must keep the
locked manifest inaccessible until opening.

## Non-goals

Campaign validation performs no IQ copying, QNAP access, network retrieval,
radio operation, PostgreSQL access, service control, detector execution,
threshold fitting, or metric evaluation.
