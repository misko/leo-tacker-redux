# Durable PostgreSQL dwell-request ingress

Migration `0019_dwell_request_ingress.sql` adds the narrow authenticated handoff
from analysis to capture. It does not add another workflow queue. The existing
`job` table remains the sole ready/leased/failed/succeeded/parked state machine;
`dwell_request_ingress` is an immutable one-to-one registry for the public
`org.leo-flow.dwell-request/0.1` payload.

## Stored identity and authority

Each registry row binds:

| Field group | Durable meaning |
|---|---|
| Request | Request ID, schema/version, canonical SHA-256 digest, idempotency key |
| Queue | Deterministic `job_dwell_<request digest>` job ID |
| Source recording | Recording ID and exact recording-identity digest |
| Source FeatureSet | FeatureSet ID, analysis-run ID, exact bundle digest and object metadata |
| Routing | Exact station ID and radio ID |
| Lifetime | Integer issue and expiry UTC nanoseconds, with the contract’s five-minute maximum TTL |

`publish_dwell_request(jsonb)` accepts only `leo_analysis`. Before inserting the
job and registry row in one transaction, it verifies that the FeatureSet and
its live object metadata exactly match the request and that the FeatureSet is
bound to the same authoritative recording and recording-identity digest.
Replaying identical content succeeds without a second row. Reusing the request
ID, deterministic job ID, digest, or idempotency key for different content
fails and rolls back the whole publication.

The Python publisher is `PostgresDwellRequestIngress`. It imports only public
contracts and PostgreSQL infrastructure, not capture or analysis
implementations.

## Capture lease API

`PostgresDwellRequestQueue` is the capture-side adapter. Its database functions
are deliberately separate from the generic analysis job API:

| Function | Role | Fence and policy |
|---|---|---|
| `claim_dwell_request` | `leo_capture` | Exact station/radio, issued and unexpired request, `SKIP LOCKED`, random token, incremented generation, lease capped at request expiry |
| `heartbeat_dwell_request` | `leo_capture` | Active token/generation and request expiry; extension remains capped at expiry |
| `complete_dwell_request` | `leo_capture` | Active token/generation/expiry and a request-derived capture-plan ID |
| `fail_dwell_request` | `leo_capture` | Active fence, bounded reason code, retry strictly before request expiry |
| `park_dwell_request` | `leo_capture` | Active fence and bounded reason code |

Expired unleased requests are parked as `request_expired` before selection.
Expired leases can be reclaimed only while the request itself remains valid;
the next claim receives a new token and generation. A stale process cannot
heartbeat, retry, park, or complete the replacement lease.

The adapter reconstructs the public `DwellRequest` from its versioned JSON and
recomputes the canonical digest before returning it. Unknown fields,
substituted routing/index fields, malformed nested references, or digest
differences fail closed. Capture therefore consumes a public contract without
importing analysis code.

## Role separation

Neither `leo_analysis` nor `leo_capture` receives direct access to
`dwell_request_ingress`. Analysis can execute only publication. Capture can
execute only the five route-scoped lease functions. Dashboard and maintenance
receive neither. The non-login `leo_routine_owner` owns the fixed-search-path
functions and has only the additional catalog read and registry insert rights
needed by those functions. Generic analysis jobs that merely use the
`dwell_capture` text label cannot be claimed because every capture claim must
join an authenticated registry row.

## Recording and FeatureSet persistence

This boundary deliberately reuses the production persistence already present:

- capture publishes the two exact recording objects through
  `PostgresRecordingPublisher` and exposes their atomic pair through
  `PostgresRecordingCatalog` under `leo_capture`;
- analysis publishes a FeatureSet through `PostgresFeatureSetCatalog`, or
  atomically publishes it with fenced recording-analysis completion through
  `AtomicPostgresRecordingAnalysisCommitter`, under `leo_analysis`;
- dwell publication accepts only those exact, already-cataloged inputs.

The PostgreSQL tests exercise this complete recording → FeatureSet → dwell
claim path, same-content replay, conflicting replay rollback, route isolation,
expiry parking, expired-lease restart, retry, stale completion rejection,
payload tamper detection, and directional privileges.

## Production inputs still required

No live database, NFS, or station was contacted while implementing this
boundary. Deployment still needs separately managed PostgreSQL login
credentials whose memberships are exactly `leo_analysis` for publication and
`leo_capture` for consumption, migration `0019` applied by the migration owner,
the production CAS used by the existing recording/FeatureSet publishers, and a
capture supervisor that passes its configured station/radio IDs to the queue.
Those inputs must not be inferred from environment variables or shared across
the two roles.
