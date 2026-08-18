# Frozen RETRO QAM recording import

This is a one-time, integration-owned import of the retained `clip-002.ci16`
known-positive corpus. It creates an ordinary immutable Redux recording/CAS
pair, a truthful historical hardware snapshot/link, and the base dashboard
detail projection. It never uses `leo-tracker` at runtime and never stores the
QNAP path in a recording contract.

The recording is permanently scoped as a historical `TEST` activity. Its
station identity is `station_historical_unattributed`, its radio is
`radio_historical_pluto_5d4d`, and its receivers are neutral physical-port
identities `rx_retro_qam_0` and `rx_retro_qam_1`. No current LNB assignment or
correction is inferred. The manifest labels it `conditioned_canary=true`,
`historical_capture=true`, `calibrated_detection=false`, and
`calibration_eligible=false`.

## Preconditions and one-shot command

Use a sealed release containing this command after all catalog and dashboard
migrations through `0054` are applied. The capture credential must be a member
of `leo_capture`; the analysis credential must be a member of `leo_analysis`.
The command is idempotent only for the exact pinned bytes. Reuse of its stable
recording ID, hardware ID, or idempotency keys for different content fails
closed.

Do not run this as a timer or service. The exact live invocation is:

```bash
/home/mouse9911/gits/leo-tracker-redux/.venv/bin/python \
  -m leo_flow.deployments.retro_qam_recording_import \
  --corpus-manifest /home/mouse9911/gits/leo-tracker-redux/tests/recording_analysis/fixtures/retro_qam_2026_08_17_v1.json \
  --archive-root /mnt/qnap01/mouse9911/leo-store/2026_08_17_RETRO_QAM \
  --expected-manifest-sha256 47a5c98064128cfdcebcf1350acb3b3005f2646e769d45d8c92a5f2def22ba7e \
  --staging-root /home/mouse9911/.local/state/leo-flow/retro-qam-import \
  --cas-root /var/lib/leo-flow/objects \
  --capture-credential-directory /home/mouse9911/.local/state/leo-flow/credentials/gauss-capture \
  --analysis-credential-directory /home/mouse9911/.local/state/leo-flow/credentials/gauss-analysis \
  --dashboard-base-url http://gauss:8090
```

Before publication the command verifies the pinned corpus-manifest digest,
the digest and exact inventory of `SHA256SUMS`, every listed archive object,
the 500,200,000-byte IQ object, the 62,525,000×2×2 little-endian CI16 geometry,
and the exact 25,000-sample window at sample 38,000,000. During materialization
it hashes the IQ stream again, and the public writer/publisher verify the copied
data and metadata objects before atomic catalog publication.

The success receipt names the stable detail URL:

`http://gauss:8090/recordings/rec_retro_qam_20260813_clip002`

After import, V30 can use the exact public coordinates without a QNAP path:

```bash
/opt/leo-flow/current/venv/bin/leo-gauss-receiver-agnostic-cfo-qam \
  --credential-directory /home/mouse9911/.local/state/leo-flow/credentials/gauss-analysis \
  --capture-guard-status /run/user/1000/leo-flow-optional-heavy/guard.json \
  --recording-id rec_retro_qam_20260813_clip002 \
  --window seg_retro_qam_clip002:rx_retro_qam_0:lower:38000000:25000 \
  --window seg_retro_qam_clip002:rx_retro_qam_1:lower:38000000:25000
```

V29/full-dwell admission may likewise target this recording ID. Its published
historical hardware link makes the neutral receiver mappings authoritative;
workers must continue to obey the optional-heavy capture guard.

## Real-stack browser acceptance plan

The integration steward should add one Playwright test backed by ephemeral
PostgreSQL plus the real filesystem CAS and dashboard composition:

1. Run this importer twice and assert identical success receipts, one recording
   row, one exact recording pair, one hardware snapshot/link, and one detail
   projection. Replace any pinned byte or preseed the recording ID with other
   content and assert a closed failure with no changed catalog row.
2. Open `/` and assert the RETRO acceptance-canary card is populated and its
   source link is present. Historical import must not be counted as a current
   live capture batch or calibration sample.
3. Follow `/canaries/retro-qam/source-recording` and assert its same-origin 302
   target is `/recordings/rec_retro_qam_20260813_clip002`.
4. Open that detail page through the real proxy. Assert historical UTC/radio/
   receiver facts, `TEST` scope, both receiver selectors, full-dwell/QAM/Doppler
   sections, and honest pending/unavailable states before derived products.
5. Publish bounded V30 and V29 products through their public operators, reload,
   and assert both receiver curves/constellations and every populated field are
   rendered. The acceptance assertion remains candidate-only and never changes
   the recording into a calibrated detection.
