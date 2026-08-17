# Gauss capture-first dual-radio collection

`leo-v5-continuous` advances the reviewed 936-unit, eight-hour coordinated
campaign on radios `.20` and `.21`. Capture and local analysis are separate
durable phases. The operator never analyzes while collection is open and never
captures after collection closes.

The exact no-drift period is `400,000,000,000 / 13 ns` (30.769230769… s).
The nine rate/duration cells cross a four-phase `L/L`, `L/U`, `U/U`, `U/L`
radio-order schedule, producing 26 repetitions of every cell/geometry pair.
Raw acquisition is 32,614,400,000 bytes; initial admission reserves twice that
amount plus the configured capacity margin.

These are the sole active science endpoints. Their checked station documents
bind `.20` to serial `...5d4d` / `rx_lnb_a,b` and `.21` to serial `...19f2` /
`rx_lnb_c,d`. Armed preflight requires both TX hardware gains at or below
`-80 dB` and all eight DDS scales (`altvoltage0` through `altvoltage7`) equal
to zero before capture metadata I/O is released.

The two services are deliberately unarmed in the repository. Their
`ConditionPathExists` checks fail closed because no successful 9/9 qualification
receipt exists, and both checked units contain an explicit
`REPLACE_WITH_EXACT_MAIN_DEFINITION_DIGEST` placeholder. Do not manufacture a
receipt or reuse a terminal campaign identity.

## Admission and exclusion

Before the first durable transition, the operator requires the existing
`capture_analysis_drain_ready()` decision: prior recording analysis, feature
projection, waterfall analysis, Starlink suite analysis, their projections,
and dashboard delivery must be fully drained. Once a collection record exists, each RF transition uses
`capture_analysis_inactive()`. This second decision permits the new pending
backlog but rejects any current recording, feature, waterfall, or Starlink-suite
lease.

Both capture and analysis retain the host-wide
`/home/mouse9911/.local/state/leo-flow/pipeline-mode.lock`. Each radio capture
runs in a fresh spawned interpreter and the campaign lock serializes journal
transitions. A terminal capture failure, constant-IQ failure, peer failure, or
skew greater than 100 ms halts collection without allocating a fresh retry.

## Operator lifecycle

All armed commands require the exact main definition, its qualification
receipt, both station files, absolute state paths, capacity margin, `--arm`,
and the exact definition digest. The main definition must have been planned
with `plan-main --deferred-analysis`, which records
`analysis_after_each_capture=false`; a one-shot/per-capture definition is
rejected before any durable continuous transition.

- `capture-next` advances at most one exact dual capture transition.
- `close` irreversibly ends RF collection and permits deferred analysis.
- `analyze-next` analyzes and projects at most one captured batch.
- `capture-run` performs only the capture phase, bounded to 1,873 loop
  transitions and the configured runtime.
- `drain-analysis` performs only the deferred analysis phase, bounded to 937
  durable transitions and the configured runtime.
- `status` reads sanitized counts and phase state without credentials or radio
  contact.

For every successfully recorded radio, `analyze-next` submits separate durable
FeatureSet, waterfall, and Starlink detector-suite jobs. The Starlink lane is
bounded and fenced independently, writes a content-addressed v0.2 bundle, then
projects a method-comparison table to the dashboard. A 1.25 MS/s edge scan is
terminal `not_evaluated` with reason `clipped-pilot-band`; it cannot block the
campaign drain. Exact 2.5 and 5 MS/s scans execute all eight report methods for
every selected segment/receiver stream. Results remain candidate evidence with
`whole-search-calibration-required`; no detection count is published before a
separately approved whole-search calibration exists.

The v6 ordinary feature/waterfall analysis envelope was 9.635–24.346 s per
radio pair, but that run did not wire Starlink. Before arming the 936-unit main
definition, benchmark the complete deferred analyzer on the 36-slot canary and
show that the selected service slice accommodates its measured high tail. A
9-hour drain allows 34.6 s per pair on average; this is a budget, not evidence
that the Starlink stage meets it.

Install the checked unit only after migrations through
`0030_campaign_scoped_analysis_claims.sql` are applied, the full drain gate is
true, the checked `qualification-v5` has produced a genuine 9/9 qualification
receipt, a main definition
has been planned from it, and the digest placeholder has been replaced with
that definition's exact digest. The units run explicitly as `mouse9911`, and
use the existing Gauss runtime
configuration and credential directories; secrets never appear in its
arguments or journal.
