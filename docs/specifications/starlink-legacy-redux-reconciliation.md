# Legacy Starlink pilot and Redux reconciliation

Status: implementation guidance and acceptance boundary, 2026-08-18.

`leo-tracker` is a numerical oracle only. Redux does not import it at runtime.
Historical LNB letters are not calibration identities: the physical LNBs have
been rearranged since the legacy captures. No legacy numerical CFO correction
may therefore be selected by `lnb-a`, `lnb-b`, `lnb-c`, `lnb-d`, or the current
`rx_lnb_*` aliases.

## Approach comparison and recommended settings

| Concern | Legacy `leo-tracker` | Redux before reconciliation | Recommendation / resolved setting |
|---|---|---|---|
| Pilot template | Frozen published Qin edge-pilot symbols; lower/upper parity handling | Same frozen template and conditioned roll-17 control | Keep exact template digests. Never add a runtime dependency on the oracle. |
| Receiver frequency center | Receiver-specific empirical centers; one historical 19f2 pairing used a +602,869.4 Hz differential | Four nominally receiver-specific profiles, all actually zero-centered ±400 kHz and named with mutable `rx_lnb_*` labels | Do **not** port the historical number. Search every current receiver independently over the same physical domain, now −1.04…+1.04 MHz. Future narrowing requires an immutable, effective-dated calibration for the current hardware snapshot. |
| Coarse CFO step | Operational paths used broad timing/CFO exploration around their receiver center; historical configurations varied | 80 kHz | Use 160 kHz at 2.5 Msps and 320 kHz at 5 Msps, with fine radii of 80/160 kHz. The domains meet at their boundaries with no CFO gap and keep complete-epoch coarse search near 50,000 cells at either sample rate. |
| Fine CFO search | Candidate-centered fine search, typically ±2 kHz at 100 Hz after a suitable coarse acquisition | ±80 kHz at 2.5 Msps or ±160 kHz at 5 Msps, at 500 Hz plus parabolic interpolation | Keep Redux v0.3. The refinement radii meet at adjacent 160/320 kHz coarse-cell boundaries; frozen RETRO tests require ≤35 Hz CFO error after interpolation. |
| Timing/epoch | Every frame epoch in the numerical detector; legacy production supplied suitably dense timing candidates | Sample-level folded epoch search with ±1 sample local refinement | Keep Redux v0.3. Acceptance covers all residues modulo 64; never return to the old 64-sample-only production grid. |
| Candidate basins | Up to 4 candidate epochs in legacy acquisition paths | 8 separated epoch/CFO basins | Keep 8. Adjudicate only after refinement using disjoint held-out pilot symbols so a stronger acquire-only alias cannot suppress the complete signal. |
| Acquire/verify split | Legacy qualification reused several correlated candidate gates | Even pilot symbols acquire; odd pilot symbols verify; conditioned control at the same winner | Prefer Redux. The whole time×epoch×CFO maximum still needs empirical null calibration; held-out here does not mean calibrated. |
| Legacy thresholds | Match/symbol/coherence gates such as 0.02–0.05 plus dual-RX timing and track gates | No v0.3 detection threshold; candidate-only | Do not port numerical thresholds. The search domain and window maximum changed. Fit thresholds only on TRAIN null/signal corpora and freeze validation/locked-test evaluation. |
| Dual receiver use | Candidate pairing, CFO-difference and inverse-noise combined QAM were useful | Evidence is intentionally unpooled by recording/radio/current LNB/RX | Keep per-RX evidence as the primary view. Add a combined view only after pairing the same pilot path and current hardware identity; never use an old label-derived offset. |
| Legacy operational windows | Varied: 10 ms probes every 3 s with ±0.5 s/100 ms follow-up; twenty 10 ms probes spaced 6 s; or three 20 ms probes in 2 s hops | Legacy-compatible adaptive v0.1 detector responses use 8 ms fixed sentinels every 3 s, the top 8 pattern-blind power seeds from the 100%-coverage timeline, and equal-quota Qin/surrogate ±0.1 s follow-up at 100 ms (at most 64 exact windows); adaptive QAM v0.4 expands up to twelve selected probes to 20 ms windows | Preserve exact accounting. The prompt timeline tiles 100% of IQ contiguously. Every pattern searches the union selected by Qin and controls. Exact detector/QAM work remains a disclosed selected subset; never label sparse exact coverage as full coverage. |
| Per-window algorithm search | Candidate scoring reused a common pilot timing/frequency hypothesis in several legacy paths | Adaptive responses independently repeated the entire epoch×CFO search for Anchor, Differential, GLRT and full-frame methods, multiplying runtime without describing one common candidate | Each Qin/surrogate pattern now independently searches its own full-frame acquire winner using a vectorized evaluation of the identical epoch×CFO cells; all eight report statistics are then evaluated at that pattern winner. This keeps target/control treatment symmetric, exposes the conditioned selection method, and reduces one frozen RETRO 8 ms window from 25.3 s to 0.9 s on the deployment host. Scalar/vectorized acquisition winner and score equivalence is a component regression. |
| Dwell aggregation | Candidate/qualified checks and track formation; strongest windows were inspected | Support-weighted overall QAM can dilute a late burst; detector overall is a max over returned exact windows | UI must show both overall and every analyzed window. Rank QAM by the best held-out-margin display window plus per-window goodness; retain the support-weighted summary but do not use it alone to declare absence. |
| Null/control | Legacy roll controls and gates were useful diagnostics | Exact Qin plus fixed precommitted surrogate patterns on identical grids | Keep paired identical searches. Increase the surrogate ensemble only with precommitted deterministic patterns; report finite rank, not a p-value, until calibrated. |
| Doppler | Tracks were formed only after pilot candidate epochs; J1 recovered about −4.0 kHz/s | Blind V9/V19 can follow a parallel ridge with a plausible slope | Do not equate a plausible rate with the pilot. Associate Doppler to an immutable QAM/acquisition path using frequency-distance and overlap gates; display blind/associated states separately. |
| QAM quality | Known-symbol accuracy, EVM, entropy and combined dual-RX plots exposed four clusters | Per-window accuracy/EVM/model SNR and diagnostic QAM goodness | Keep goodness diagnostic: geometric mean of chance-corrected accuracy and EVM compactness. Show constellation, accuracy, EVM, winner epoch/CFO, held-out/control/margin, window time, and hardware scope together. |

## Windowing and UI acceptance

The recording detail page has one unified, non-pooling selector set for
recording/radio, authoritative effective-dated LNB assignment, receiver port,
channel, edge, detector method, Qin/surrogate pattern, and overall/window mode.
It must render:

1. A 100%-union contiguous raw-IQ power timeline, including a short final tail.
2. Highlighted windows selected for expensive exact refinement.
3. Exact Qin and every precommitted surrogate response versus UTC for every
   analyzed detector window.
4. QAM goodness and constellation evidence for every analyzed QAM window, plus
   the support-weighted overall summary and selected display window. Adaptive
   windows expose their Qin/control source scores, margin, and selection reason;
   the UI falls back to immutable v0.3 only when v0.4 is not published.
5. Basic and advanced-path-only Doppler totals and local window slopes.
6. An analysis-approach table containing the actual persisted window duration,
   window count, union coverage, physical CFO bounds, hypothesis-cell counts,
   aggregation rule, and calibration state.

All missing, pending, error, truncated, sparse, historical-profile, and
uncalibrated states are visible. A historical profile is never silently
reinterpreted as the current wide profile.

## Acceptance tests

- Synthetic acquisition at every timing residue and across the original
  ±400 kHz domain.
- Label-independent recovery at −900 kHz, +446.6 kHz (the J1 failure region),
  and +900 kHz with the new bounded domain.
- Frozen read-only RETRO corpus recovery with exact object hashes and expected
  epoch/CFO/QAM metrics; absence of the corpus skips, never regenerates, the
  fixture.
- Real-handler Playwright coverage for the recording evidence page, including
  all selectors, approach rows, coverage, CFO domain, candidate-only warnings,
  and pending/error/missing states.
- Real-handler Playwright coverage for every master capture-table column,
  including current LNB/RX-separated Doppler and QAM, capture duration, detail
  links, and explicit unavailable calibrated pilot/satellite values.

## Remaining calibration boundary

The wider acquisition prevents the observed CFO miss; it does not create a
Starlink verdict. Its larger time×epoch×CFO maximum must be replayed against
precommitted surrogates and held-out real null recordings before thresholds are
promoted. Current effective-dated hardware calibration may later reduce search
cost, but only if it is measured after the swap and bound by immutable hardware
snapshot—not by a human-readable LNB letter.
