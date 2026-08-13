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

Before enabling production schedules, integration still needs to supply the
provider-specific retriever factory and secret-store capability, run one
explicit opt-in provider canary, define operator handling for non-retryable job
failures, and install the cadence scheduler as a supervised process.

No live provider calls run during normal unit/integration tests. A separately
marked, opt-in provider canary should fetch once into a temporary CAS, validate
checksums/count/epoch bounds, verify no secret appears in diagnostics, and then
be removed.

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
