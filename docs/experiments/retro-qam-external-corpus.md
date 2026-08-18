# Retrospective QAM external corpus

The frozen manifest at
`tests/recording_analysis/fixtures/retro_qam_2026_08_17_v1.json` binds the
historical CH4 lower-edge observation to the canonical read-only archive at
`/mnt/qnap01/mouse9911/leo-store/2026_08_17_RETRO_QAM`.

The external integration test verifies the archive inventory, every retained
object hash, CI16 geometry, and the exact selected-window hash. It then reads
the 25,000-sample window beginning at original recording time 68.7 seconds and
runs the native Redux constellation analyzer for both receivers. The analyzer
is conditioned through the public detector-suite contract on the historical
epoch and CFO. No `leo-tracker` code is imported or used at runtime.

This is candidate evidence for a known published synchronization pilot. It is
not a calibrated detection and does not decode payload. The manifest records
the historical acquisition winners as accepted targets for the additive native
v0.3 multi-basin search. The archived integration regression now requires Redux
to recover the exact epoch on both receivers, CFO within 35 Hz, the held-out
exact/control margin, individual constellation accuracy/EVM, and the historical
inverse-noise dual-receiver metrics. The published v0.2 search remains
immutable and is not reinterpreted.

The independent `leo-starlink-retro-qam-canary` oneshot repeats this check from
the raw object on a systemd timer. It hashes the complete 500,200,000-byte IQ
object before every run and atomically publishes a candidate-only receipt. A
passing canary is a numerical regression result for a known pilot, never a
calibrated detection verdict.

When the QNAP corpus is unavailable, external tests skip cleanly. The manifest
scope and candidate-only boundary remain covered without the mount.
