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
