# Archived-TLE recording visibility candidates

`leo_flow.analysis.orbit.recording_visibility` produces a deterministic,
content-addressable candidate set for one immutable recording–ephemeris link.
It is an offline evidence step between temporal TLE selection and RF residual
association. It does not fetch a mutable catalog and does not assign target
truth.

## Existing components reused

The implementation deliberately reuses the established ephemeris tail:

| Existing boundary | Reused evidence |
| --- | --- |
| `HuggingFaceRetriever` / `SpaceTrackRetriever` | Fixed provider identity and credential-free query provenance |
| `CasRawEphemerisArchive` | Exact provider bytes, byte count, SHA-256, media and format identity |
| `TLECatalogNormalizer` | Strict checksums plus canonical NORAD-sorted element lines and epochs |
| `CasNormalizedEphemerisArchive` | Exact canonical catalog bytes and SHA-256 |
| `CasEphemerisProvenanceArchive` | Retrieval start/completion, provider query, parser and validation policy |
| `EphemerisSnapshot` and catalog | Immutable publication identity and parsed ID/epoch summary |
| `RecordingEphemerisLink` / `EphemerisLinkEvidence` | Recording identity, capture interval, temporal policy and `as_of` boundary |
| `EphemerisReader` | Exact normalized-snapshot read capability |
| `OrbitPropagator` / pinned SGP4 adapter | Offline station-relative propagation with explicit numerical choices |

No second provider client, TLE parser, archive, mutable-latest lookup, or orbit
implementation is introduced.

## Immutable association input and output

`RecordingVisibilityRequest` closes over the exact recording link, full
`EphemerisSnapshot`, provenance object reference, ITRF station geometry digest,
propagation specification, sorted NORAD allow-list, uncertainty policy, and
`recording-visibility-candidates-v1` algorithm artifact. Construction rejects a
source, scope, raw hash, normalized hash, temporal-policy, or snapshot mismatch.

Before propagation, the analyzer reads the normalized bytes through the public
`EphemerisReader` and verifies byte count, SHA-256, canonical encoding, source,
scope, satellite count, NORAD-set digest, and element epoch bounds. Each output
candidate repeats its parsed NORAD identity and element epoch. The association
also retains the full snapshot, including retrieval time and exact raw and
normalized object references, plus the provenance object reference.

`encode_recording_visibility_association` emits canonical JSON. The association
ID derives from a digest over all scientific inputs and outputs. A changed
recording, TLE byte, retrieval, station, propagation profile, uncertainty,
candidate membership, sample, or algorithm version therefore creates a
different identity.

## Deterministic visibility rule

The default policy samples recording start, integer midpoint, and finish. More
fractions may be declared as sorted millionths of the recording interval. For
each requested NORAD ID, the analyzer:

1. rejects elements beyond the frozen absolute-age bound;
2. propagates every sample time through the supplied offline orbit port;
3. records nominal elevation and a symmetric, pre-combined elevation margin;
4. reports one of `visible_at_sample_with_margin`,
   `elevation_margin_overlap`, `below_gate_at_samples`,
   `element_epoch_outside_bound`, or `propagation_error`.

The policy explicitly records station-position uncertainty, recording-time
uncertainty, the elevation margin, element-age limit, sampling fractions,
candidate bound, and the artifact supporting those uncertainty choices. This
version does not independently transform station or timestamp uncertainty into
angle; the supplied elevation margin must already cover the operator’s intended
uncertainty model.

Sparse samples do not prove continuous visibility between samples. “Visible”
means only that at least one sampled nominal elevation remains above the gate
after subtracting the declared margin. Margin-overlap candidates remain
possible rather than selected. The result always carries
`evidence_class: weak-ephemeris-visibility`,
`ground_truth_eligible: false`, and `sparse-sampling-only` and
`weak-evidence-not-ground-truth` reason codes.

## Exact operator inputs

Before an operational run, provide and review:

1. provider (`huggingface` or `space-track`), scope and request-spec label;
2. deterministic retrieval ID and retrieval schedule slot;
3. for Space-Track only, a dedicated credential capability from the process
   secret store—never a URL, job payload, provenance field, or log value;
4. temporal selection policy, policy artifact and `as_of_utc_ns` boundary;
5. exact published recording ID/identity and capture interval;
6. ITRF station XYZ metres and its reviewed geometry digest;
7. pinned propagation specification (currently the optional
   `sgp4==2.25`, Vallado improved, WGS72 profile where scientifically accepted);
8. sorted candidate NORAD IDs from the exact normalized catalog;
9. minimum elevation, element-age bound, PPM sample fractions, maximum candidate
   count, station/time uncertainties, pre-combined elevation margin, and the
   uncertainty-basis artifact.

Provider terms, cadence and attribution must be reviewed before live operation.
No network fetch or credential lookup was performed for this implementation.
Fixture-backed tests cover both provider identities and use the deterministic
orbit simulator; existing provider tests inject HTTP transports and placeholder
credentials without network access.

Run the focused checks with:

```text
MYPYPATH=src .venv/bin/mypy \
  src/leo_flow/analysis/orbit/recording_visibility.py

PYTHONPATH=src:. .venv/bin/pytest -q \
  tests/model_analysis/test_recording_visibility.py \
  tests/ephemeris tests/model_analysis/test_orbit_association.py \
  tests/model_analysis/test_sgp4_adapter.py
```

Operationally blocked work remains: an operator must supply approved provider
configuration, secrets capability for Space-Track, station geometry and
uncertainty artifacts, a recording list, candidate scope, and an explicit
opt-in provider canary. None is inferred or extracted by the offline analyzer.
