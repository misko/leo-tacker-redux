# Gauss 36-slot supercycle canary v1

This inactive deployment is a finite promotion gate, not a smaller main
campaign. It has a distinct definition schema, SQLite table, state root,
operator and receipt. Its receipt always says `main_campaign_authorized=false`
and cannot be consumed by the main-v3 or qualification-v2 codecs.

The capture unit must succeed before analysis starts. Capture owns `.20/.21`
for exactly 36 slots at `400000000000/13 ns`, with a 15 s preflight, no catch
up, no replay, a 100 ms skew gate and 40 ms hardware blocks (1/2/4 refills for
40/80/160 ms). `OnSuccess` then starts the no-radio analysis unit. Analysis
uses migration 0030's campaign-scoped claims and six ordered barriers with
8 compute or 4 projection workers. A parked/failed barrier halts the canary.

The final receipt requires 36 terminal eligible pairs, 72 distinct recordings,
72 FeatureSets, waterfalls, terminal Starlink v0.2 suite results and dashboard
recording projections. It reports candidate-only/not-evaluated science states;
it never reports a Starlink detection count. Per-stage wall time, process CPU,
peak RSS, per-pair completion latency and skew are immutable receipt evidence.

The `.service.in` templates are deliberately not installed or enabled. The
selected sealed release provides the Gauss runtime composition entry point,
but the integration steward must substitute every generic release/canary token,
retain the rendered bytes in release evidence, and pass the offline release
verifier. The canary definition and exact v8 receipt remain external immutable
state; they are never added to the already closed release inventory. Live
promotion separately validates migration head 0030 plus that exact v8 receipt,
then performs the normal drain/ownership, capacity,
passive-both-TX/DDS/constant-IQ and radio identity gates.
