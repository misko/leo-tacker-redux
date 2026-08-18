# Optional symbolwise replay worker v1

This deployment consumes only work admitted by the explicit request command.
Capture publication does not call it, migration 0052 does not backfill it, and
the worker does not discover recordings. Each request pins the complete public
`RecordingObjectRef`, canonical stream geometry, and immutable data-independent
frequency-center evidence. Enqueue re-resolves the public catalog record and
requires the complete recording reference to match before it inserts one
idempotent work identity.

The service processes at most one claim per cycle. Immediately before claiming,
it requires a fresh capture guard, zero focused-analysis backlog, host resource
headroom, and the same `maximum-optional-concurrency=1` local permit used by the
other optional heavy workers. Capture guard denial happens before the database
claim. `Nice=15`, `CPUQuota=300%`, and hard memory/task bounds preserve capture
priority after admission. The template is checked in but is not installed or
enabled by repository code.

## Safe development rollout

1. While capture remains untouched, render a release pinned to the candidate
   commit and verify its release manifest and validation receipt. Render the
   unit placeholders, then run `systemd-analyze --user verify` on the rendered
   unit. Do not install or start it yet.
2. Prepare one canonical request document from the exact returned public
   recording reference and an approved immutable frequency-center artifact.
   Do not infer a CAS path, scan storage, or derive a center from the recording
   samples. Validate it without credentials or external access:

   ```console
   leo-gauss-symbolwise-enqueue validate-request \
     --request exact-symbolwise-request.json
   ```

3. Wait until the capture operator reports a terminal recording and no capture is active.
   Through the integration-owned migration procedure, apply exactly
   `0052_starlink_symbolwise_replay_product_v0_1.sql`. Do not edit or replay the
   migration body, and do not perform database readiness probes during active
   capture.
4. With capture inactive, enqueue exactly that request. Use priority `100` only
   for the first reviewed canary; ordinary current-recording and historical
   backfill requests use `0`. Repeat is idempotent only for the identical
   canonical request and priority:

   ```console
   leo-gauss-symbolwise-enqueue enqueue-request \
     --request exact-symbolwise-request.json \
     --credential-directory /home/mouse9911/.local/state/leo-flow/credentials/gauss-analysis \
     --priority 100
   ```

5. Install but do not enable the rendered unit. Start it manually for the
   canary. A missing/stale guard, capture guard window, focused backlog, resource
   pressure, or occupied optional slot pauses before claim. Inspect the
   sanitized JSON event and durable work/product state after capture is inactive.
6. Stop the canary worker before changing configuration. After one exact product
   is reviewed, optionally enable the worker; enqueue remains a separate bounded
   operator action. For backfill, enqueue one reviewed exact request at a time
   (or a separately reviewed bounded caller of this same one-request port) and
   never add capture-triggered admission.

The product is candidate evidence requiring whole-search calibration. This
deployment neither changes dashboard semantics nor authorizes a detection claim.
