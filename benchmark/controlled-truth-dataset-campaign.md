# Controlled-truth campaign artifact

The versioned campaign schema is
`specs/controlled-truth-dataset-campaign-v1.schema.json`. It describes compact
truth and identity metadata only; no Recording, FeatureSet, or QNAP payload is
stored under `benchmark/`.

`manifests/controlled-truth-campaign-v1.example.json` is a schema/validator
example made entirely from placeholder identities and tiny synthetic times. It
is not acquired truth, not a real locked test, and not evidence for a detector.

Campaign authors freeze matrix points for SNR, carrier offset, drift, receiver
delay/gain, clipping, null class, confounders, and independent truth provenance.
After fixture production and independent recording analysis, the manifest binds
each truth specification to one Recording identity digest and one FeatureSet
digest. The Python validator recomputes truth and membership digests and rejects
unknown fields, so mutable queries and storage locations cannot enter v1.

Each group enumerates its base-noise truth specification and every injection.
Groups are assigned whole and in time order to train, validation, and a sealed
locked test. An optional TLE input is accepted only as immutable
`available_then` context available no later than capture; it is never truth.

See `docs/dataset/controlled-truth-campaign-v1.md` for the freeze and locked-test
opening procedure. This artifact does not authorize generating, copying, or
reading campaign data.
