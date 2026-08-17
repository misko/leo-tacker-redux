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
the historical acquisition winners as pending acceptance targets for a revised
search. The current test deliberately does not require the present production
search to reproduce them, because doing so would freeze its known acquisition
failure as desired behavior.

When the QNAP corpus is unavailable, external tests skip cleanly. The manifest
scope and the separation between conditioned decode and future search
acceptance remain covered without the mount.
