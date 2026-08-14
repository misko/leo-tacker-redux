# ADR 0005: Ground truth and dataset partitions

Status: accepted for the development contract

## Decision

Dataset membership is an immutable, explicit assignment of complete correlation
groups to `train`, `validation`, or `locked_test`. A correlation group is formed
before looking at detector output and includes the same pass, contiguous capture
session, satellite opportunity, radio/time neighborhood, base-noise recording,
and all of its digital injections. The dataset builder performs no random split.

Labels carry a source, evidence digest, producer, timestamp, uncertainty, and an
explicit statement of which evaluated methods did not produce the evidence.
Sources are distinct: observed instrument truth, manual review, exact digital
injection, ephemeris-derived association, pseudo-label, and unlabeled. TLE
agreement and pseudo-labels remain useful model inputs but are not accuracy
truth. Manual/observed truth is accepted for a method only when the evidence
declares independence from that method. Injection truth also pins the immutable
base recording and independently generated injection specification.

Membership separately freezes whether a member is scored truth or context-only.
Thus unlabeled recordings may support an unsupervised/global fit without being
silently counted as negatives or entering accuracy denominators.

Independent-recording analysis remains a pure recording-to-FeatureSet step.
Dataset construction consumes only immutable FeatureSet identities plus grouping
and truth metadata. Cross-recording fitting consumes the resulting frozen
membership and cannot reach back into the independent analyzer.

Method comparisons align scores only on the exact segment, receiver, and sample
window. Missing method output is not a non-firing. Firing covariance and phi are
reported with pairwise shared-window and shared-sample counts plus missingness.
Pairwise deletion may produce a non-positive-semidefinite matrix, so the report
is not represented by the `Covariance` parameter-estimate contract.

## Consequences

- Repeating construction with reordered inputs produces the same order and
  digest; changing a feature digest or partition changes membership identity.
- Train/validation/test require an upstream pass/session grouping decision.
  Unknown pass IDs are conservatively grouped rather than guessed from scores.
- Selection, threshold fitting, and calibration use train; method selection uses
  validation; locked test is opened only against a frozen method/config digest.
- Radio/LNB/firmware generalization requires a second locked evaluation holding
  out complete receiver and hardware epochs.
- The nine-member legacy corpus is explicitly non-promotable as scored truth: it has only two
  conservative groups, no locked test, no independent negatives, no exact
  injected truth, and proxy/unlabeled labels.

## Synthetic truth

`benchmark/specs/synthetic-iq-v1.json` remains the detector-independent fixture.
Its exact CI16 hashes and known frequency, drift, SNR, receiver delay/gain, and
clipping truth exercise scientific plumbing. When injected into a frozen real
noise recording, the derived fixture must add the base-recording digest and a
separate injection-spec digest, and inherit the base recording's split group.
Production detector code must never be called to generate this truth.

The paper-derived `leo-flow.starlink-edge-pilot-if-fixture/v1` source is also
detector-independent. It may supply exact digital-injection positives and
conducted-loopback fixtures, but its label covers only the generated edge-pilot
subset—not satellite identity, a complete Starlink waveform, over-air presence,
or received SNR after the analog chain. A real recording used as its background
remains named and digest-bound because it may already contain an unknown signal.
The source fixture and injection result are distinct immutable identities.

## Compatibility

This adds analysis-owned development fixtures and does not change frozen v0.1
capture, FeatureSet, or model contracts. Future production serialization needs
its own schema version and migration ADR before persisted snapshots are
published.
