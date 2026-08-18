# Pattern-symmetric adaptive QAM v0.5

Status: additive offline candidate-evidence component. Adaptive-QAM v0.4 is
unchanged. No threshold, verdict, persistence, dashboard, or live operator is
defined here.

The component regenerates Qin and the complete precommitted surrogate bank and
verifies their template digests against the durable adaptive response. It
intersects declared response-window geometry across receivers, selects a
uniform bounded subset without reading any score, and runs the identical v0.3
epoch/CFO acquisition on every selected window for every pattern and receiver.
Each acquisition is followed by a known-pattern 300-by-8 cross-fitted QPSK
quality calculation. The output retains per-window winning epoch/CFO, search
identity, complete-frame support, hard-symbol accuracy, RMS EVM, and QAM
goodness for Qin and every surrogate.

Every pattern uses the same frozen 17-symbol-roll construction for its
acquisition control. Thus Qin receives no target-only control or
window-selection privilege. These controls are paired comparators, not
verified signal-absent observations.

## Bounded policy and measured cost

The default ceiling is 9 patterns, 3 windows, 2 receivers, and 54 complete
acquisition/QAM runs. Inputs exceeding any bound fail before reading samples;
patterns and receivers are never silently truncated. A normal Qin-plus-four
surrogate, three-window, dual-RX dwell requires 30 runs.

On the gauss development host on 2026-08-18, deterministic complex-noise
benchmarks with the production v0.3 CFO/refinement geometry measured:

| Window | Runs | Wall time | Time per run |
|---|---:|---:|---:|
| 7,500 samples | 5 | 1.233 s | 0.247 s |
| 50,000 samples | 5 | 2.564 s | 0.513 s |

The 50,000-sample measurement implies about 15.4 CPU-seconds for the normal
30-run shape and 27.7 CPU-seconds at the hard 54-run ceiling, before storage or
scheduler overhead. This is an optional bounded offline workload and does not
belong on the capture-critical path.

RETRO and J1 may check Qin numerical behavior, but both are conditioned examples
and contribute no calibration members or sample size. Calibration still
requires frozen independent null and positive manifests.
