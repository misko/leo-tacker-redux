# leo-tracker-redux

A deliberately small rebuild of the LEO radio experiment pipeline around three
applications: capture, analysis, and dashboard.

The architecture is contract-first. Capture publishes immutable recordings,
independent analysis maps one recording to a feature bundle, model analysis fits
cross-recording state from frozen feature sets, and the dashboard is read-only.

This repository is under initial construction. The legacy `leo-tracker`
repository is a reference and numerical oracle, not a runtime dependency.
