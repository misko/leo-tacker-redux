# Historical Starlink QAM regression canary

This independent oneshot/timer periodically reads the exact retained CI16
corpus, verifies its complete SHA-256 identity, runs the native Redux v0.3
multi-basin acquisition and pilot constellation analysis, and writes an atomic
receipt. It never imports `leo-tracker`; that repository and its frozen JSON
are numerical oracles only.

Render the templates with absolute values for `SOURCE_ROOT`,
`PYTHON_ENV_ROOT`, `CORPUS_MANIFEST`, `CORPUS_ROOT`, `STATE_ROOT`, and the exact
`SOURCE_COMMIT`. The production corpus manifest is
`tests/recording_analysis/fixtures/retro_qam_2026_08_17_v1.json`; its archive
root is `/mnt/qnap01/mouse9911/leo-store/2026_08_17_RETRO_QAM`.

The service is independent of capture, the database, the dashboard, and radio
ownership. A successful service result means only that acquisition and QAM
metrics match the frozen known-pilot oracle. The receipt remains explicitly
candidate-only and cannot claim a calibrated Starlink detection.
