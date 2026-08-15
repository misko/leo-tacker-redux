# Ephemeris acquisition operations

## Configuration and credentials

Use a dedicated Space-Track account and inject its username/password into the
process secret store. Do not put credentials in job payloads, request specs,
URLs, configuration artifacts, logs, or provenance. The request spec is a
versioned audit label such as `gp-starlink-active-v1`, not arbitrary URL text.

Hugging Face retrieval is unauthenticated. Its transport deliberately rejects a
credential capability. Both provider URLs and allowed hosts are fixed by the
adapter composition.

Recommended starting schedule: one retrieval per provider every six hours, with
at most 24 missed slots enqueued in one scheduler pass. Confirm current provider
terms and rate limits before enabling the scheduler.

## Deployment composition

1. Construct the shared `FileSystemBlobStore` (or an equivalent object-store
   `BlobWriter`/`BlobReader`).
2. Give separate `CasRawEphemerisArchive`, `CasNormalizedEphemerisArchive`, and
   `CasEphemerisProvenanceArchive` capabilities the writer.
3. Compose `SpaceTrackSessionTransport` with `SpaceTrackRetriever`, or a host-
   bounded `UrllibHttpTransport` with `HuggingFaceRetriever`.
4. Compose the retriever, normalizer, validator, archive and persistent catalog
   through `EphemerisIngestionService`.
5. Enqueue stable cadence slots with `EphemerisScheduler`; execute them under
   the existing fenced `EPHEMERIS_RETRIEVAL` job lease.

## Provider canary and scheduled evidence

The checked-in provider canary is offline by default. It runs the provider
parser through a deterministic TLE fixture, writes immutable raw, normalized,
provenance, and canary-receipt objects, resolves the exact snapshot through the
catalog, and runs the pinned `sgp4==2.25` profile at the element epoch. The
receipt binds the object references, NORAD IDs, element epochs, parser and
validation policy, station geometry, propagation profile, and propagated
state.

Run the default fixture path with:

```console
/opt/leo-flow/bin/python -m leo_flow.deployments.ephemeris_provider_canary \
  --config /etc/leo-flow/ephemeris-provider-canary.json \
  --root /var/lib/leo-flow/ephemeris-provider-canary
```

Install `leo-ephemeris-provider-canary.service` and its timer for a persistent,
six-hour, randomized schedule. The installed service permits only `AF_UNIX`,
so the timer repeatedly exercises the archive/catalog/SGP4 path without network
access. Same-slot fixture execution is content-idempotent: the four CAS object
identities remain unchanged. Existing `EphemerisScheduler`/fenced-worker tests
separately prove stable cadence IDs, at-least-once enqueue idempotence, and
bounded rate-limit/transient retries with secret-free failure codes.

Live retrieval has two independent gates: the reviewed config must set
`network_approved` to `true`, and the process must receive `--allow-network`.
The sample `allow-network.conf.example` supplies that command-line gate and
permits only Internet address families; it must not be installed by default.
The canary then performs at most one provider data request, enforces a 60-second
absolute timeout ceiling, a 16 MiB response ceiling, and a persistent minimum-
interval lock before the request. Redirect and host policy remain fixed in the
provider HTTP adapters. Process-boundary failures emit only exception class,
never provider response text or credential values.

One bounded Hugging Face call passed on 2026-08-14 local time and its immutable
evidence is recorded in `docs/experiments/ephemeris-provider-canary.md`. A
machine-installed recurring live configuration still requires explicit
operator installation. Remaining operational inputs are:

| Provider | Missing input before live proof |
|---|---|
| Hugging Face | Install a reviewed copy of `huggingface-dry-run.example.json` with `network_approved: true`, plus the explicit network override; the one-shot live path itself is proven |
| Space-Track | All Hugging Face-style approval inputs, plus existing dedicated systemd credential capabilities named by the config (the example names are `space-track-identity` and `space-track-password`) and matching `LoadCredential=` mappings |

Do not discover alternate environment variables, enumerate credential
directories, copy credential values into JSON, or infer approval from network
reachability. A Space-Track dry run records only configured capability names
and does not resolve them. Live and fixture receipts are distinguishable by
`mode` and `live_retrieval_performed`; an injected test transport is never
reported as live evidence.

The dated implementation/live-evidence split is recorded in
`docs/experiments/ephemeris-provider-canary.md`.

## Persistent catalog and fenced completion

Migration `0003_ephemeris_catalog.sql` adds the immutable
`ephemeris_snapshot` catalog. The catalog row is the single visibility point
for the raw, normalized, and provenance object references. Retrieval IDs and
snapshot IDs are each unique; replaying identical content is idempotent, while
reusing either identity for different content is an error. History is ordered
by exact `retrieved_at_utc_ns` and then snapshot ID, preserving deterministic
`AVAILABLE_THEN` and `FIRST_AFTER` selection.

Production workers must call `EphemerisIngestionService.prepare`, then commit
with `PostgresFencedEphemerisCommitter`. The committer locks and validates the
active `EPHEMERIS_RETRIEVAL` lease, registers all object references, publishes
the snapshot, and marks the job succeeded in one transaction. Do not compose a
worker from `catalog.publish` followed by `jobs.complete`: that split permits a
lease-expiry race. A stale token or generation cannot publish or complete.

The job payload is an allowlisted description containing retrieval ID, source,
scope, request-spec label, and schedule slot only. Credentials remain a
separate provider-transport capability. Failures store retry-policy reason
codes, never provider exception text.

Database capabilities are intentionally asymmetric: `leo_analysis` may insert
catalog/object references and update jobs; `leo_dashboard` can only read the
catalog; `leo_capture` has no ephemeris access. Snapshot rows are immutable for
all component roles.

Before enabling the production ingestion worker, integration still needs to
supply its provider-specific retriever factory and persistent catalog/secret-
store capabilities, run one explicit opt-in provider canary, and define
operator handling for non-retryable job failures. The checked-in canary timer
is supervised schedule evidence; it is not a substitute for the fenced
production ingestion worker.

No live provider calls run during normal unit/integration tests. Live canary
evidence must be preserved separately from fixture evidence and must never be
described as provider proof unless `live_retrieval_performed` is true.

## Alerts and recovery

| Condition | Action |
|---|---|
| HTTP 429 | Retry at provider `Retry-After` |
| HTTP 5xx/interrupted body | Capped exponential retry |
| HTTP 401/403 | Stop and rotate/check credentials |
| HTML, partial, oversized or invalid TLE | Quarantine raw object; do not publish snapshot |
| Validation count/epoch failure | Preserve raw evidence; review policy/provider |
| Retrieval ID conflict | Stop; scheduler/config identity has been reused incorrectly |
| No temporal selection | Leave analysis input unresolved; never substitute mutable latest data |

CAS objects are immutable. Recovery replays the raw object through a pinned
parser/policy into a new snapshot identity when behavior changes; it never edits
an existing normalized catalog or provenance manifest.

## Recording-time visibility candidates

After an authoritative recording–ephemeris link exists, the offline
`recording_visibility` analyzer can bind the exact snapshot/provenance objects,
parsed element identities and epochs, recording identity, ITRF station,
propagation profile, uncertainty policy and algorithm version into a weak
visibility-candidate artifact. See
`docs/experiments/recording-visibility-candidates.md` for the exact operator
inputs and evidence limitations. This step never fetches a provider, chooses a
mutable latest snapshot, or promotes TLE visibility to ground truth.
