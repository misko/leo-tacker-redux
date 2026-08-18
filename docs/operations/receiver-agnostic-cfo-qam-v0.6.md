# Receiver-agnostic CFO/QAM v0.6 backfill

This diagnostic is explicit optional work. Capture never submits it, waits for
it, or imports it as a runtime dependency. The operator accepts one already
published Redux recording and one to six exact CI16 windows. Every window uses
the same `-700 kHz..+700 kHz` residual-CFO plan for every radio and receiver.
Receiver labels are identity and filter fields only; they never select a CFO
center or correction.

The process must run with the focused capture guard and the shared optional
work concurrency set to one. A rejected guard/resource decision exits cleanly
with a `receiver_agnostic_cfo_qam_paused` receipt so a timer can retry. A
successful exact request is idempotent: rerunning it returns the already
published V30 product without reading IQ again.

## Resource envelope

- one published recording per process invocation;
- at most two `(segment, receiver, edge)` streams;
- at most three windows per stream and six windows total;
- at most 50,000 complex samples per window (the normal 2.5 Msps window is
  20,000 or 25,000 samples);
- at most nine patterns, 1,000,000 pattern evaluations, and 64 MiB analyzer
  working memory per window;
- at most one optional-heavy process on the host, with one estimated CPU core,
  eight reserved capture cores, 8 GiB available memory, and I/O pressure
  `avg10 <= 5` by default.

A representative 20,000-sample, nine-pattern zero-input window took 9.0 s and
58 MiB peak RSS on the development host on 2026-08-18. For continuous service,
submit one or two windows per invocation so work begun in the capture-safe
interval normally finishes before the next pre-capture guard. Multi-window
historical backfills should use a planned maintenance-safe interval; capture
still does not await them.

## Deployment and backfill

The additive database migration `0053_recording_receiver_agnostic_cfo_qam_v0_6.sql`
must already be applied. Install the sealed release, then invoke its console
entry point. Window syntax is:

```text
segment_id:receiver_chain_id:lower|upper:start_sample:sample_count
```

The reviewed J1 late-burst product uses the recording's actual segment ID and
two physical receivers without legacy LNB corrections:

```bash
leo-gauss-receiver-agnostic-cfo-qam \
  --credential-directory /run/credentials/leo-receiver-agnostic-cfo-qam.service \
  --capture-guard-status /run/leo-flow/focused-capture-guard.json \
  --recording-id rec_01M09J1R6E59GCC8ANJVYVRN1B \
  --window seg_plan_focused_loop_00000001_18cccbd3289eb706_b_ch4_lower:rx_lnb_c:lower:149500000:25000 \
  --window seg_plan_focused_loop_00000001_18cccbd3289eb706_b_ch4_lower:rx_lnb_d:lower:149500000:25000
```

To retain both conditioned J1 epochs in one manual backfill, add the same two
receiver windows at start sample `101500000`. These coordinates are regression
acceptance cases, not training or calibration evidence.

For a reviewed recent recording, obtain the segment, receiver IDs, and exact
sample coordinates from its published manifest and issue the same command.
Do not infer an edge or frequency correction from an LNB-shaped receiver name.

RETRO remains a frozen external acceptance corpus, not a published Redux
`RecordingObjectRef`. Its conditioned numerical replay is therefore run with:

```bash
leo-starlink-retro-qam-canary \
  --corpus-manifest tests/recording_analysis/fixtures/retro_qam_2026_08_17_v1.json \
  --receipt /var/lib/leo-flow/retro-qam-canary/latest.json \
  --git-commit "$(git rev-parse HEAD)"
```

Publishing a V30 detail product for RETRO requires a separate integration-owned
one-time import that creates and verifies a public recording data/metadata pair.
The V30 operator deliberately refuses an NFS path or private storage layout;
after that import, use its public recording ID and manifest coordinates exactly
as above (`start_sample=38000000`, `sample_count=25000`, both receivers). This
keeps the external corpus out of the runtime dependency graph.

## Acceptance regressions

The mounted-corpus tests
`test_conditioned_retro_receivers_retain_v03_score_and_v05_qam` and
`test_conditioned_j1_early_and_late_receivers_retain_numerics` bind the source
hashes/geometry and the expected CFO, epoch, Qin score, accuracy, EVM, and frame
support. Their fixtures explicitly declare conditioned-canary scope and make no
detection or calibration claim.
