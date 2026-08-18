# Starlink acquired QAM v0.3

This additive lane selects known-pilot QAM evidence with the bounded v0.3
multi-basin acquisition. It does not alter detector-suite v0.2 or pilot
constellation v0.1. Existing requests and durable bytes therefore remain
replay-compatible.

The v0.3 evidence binds the immutable recording stream, source suite, source
acquisition, acquisition search identity, exact and conditioned-control
templates, QAM implementation/configuration, and revised calibration identity.
The emitted product is candidate evidence for the published edge pilot. It is
neither payload decoding nor a Starlink-presence verdict.

## Calibration gate

No threshold is defined. The calibration identity names the complete maximum
over time windows, epoch hypotheses, coarse CFO hypotheses, and retained-basin
refinement. Its only permitted state in v0.3 is
`blocked-pending-whole-revised-search`, and `calibrated_threshold` must be null.
Calibrating a per-cell statistic, the old v0.2 maximum, or only the winning
basin is insufficient.

Threshold selection must use TRAIN, adjudication must use VALIDATION, and the
frozen TEST/RETRO corpus is acceptance-only. A later calibrated contract must
be additive and publish the corpus identity, partition membership, empirical
null construction, family-wise error target, threshold, and uncertainty.

## Composition and durability

`CombinedStarlinkSuiteAnalysisJobPreparerV0_3` wraps the frozen v0.2 preparer:
it preserves suite v0.2, surrogate-null v0.1, QAM v0.1, and temporal v0.1, then
adds receiver-profiled v0.3 acquisition and acquired-QAM evidence for eligible
streams. Ineligible clipped streams remain empty.

The v0.3 evidence is not compatible with the v0.1 catalog codec or migration
0036. Deployment therefore requires an additive v0.3 recording bundle, codec,
catalog migration, atomic publication update, query port, and dashboard
projection. Until those land, production composition must stay on the v0.2
preparer; silently storing v0.3 bytes as v0.1 is forbidden.

The exact approved configuration and algorithm digests are in
`benchmark/specs/starlink-acquisition-v0.3.json`. `leo-tracker` remains an
offline numerical oracle and is not imported at runtime.
