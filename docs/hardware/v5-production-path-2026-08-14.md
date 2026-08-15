# V5 PostgreSQL production-path qualification — 2026-08-14

The explicitly armed composed qualification passed against the V5 radio at
`ip:192.168.1.15`. The radio had no LNB; RX1 and RX2 remained on the conducted
tee fixture. This run establishes transport, persistence, fencing, lineage,
restart, and cleanup behavior. It does not establish Starlink ground truth or
detection accuracy.

This is a historical receipt from the 2026-08-14 harness. It predates the
current fail-before-write gates for a separately pinned database name, owner,
cluster system identifier across every DSN, exact authenticated-session/direct
grant closure, and an empty application catalog. It must not be represented as
a pass of the current production-path qualification or used to authorize a new
live run. The hardening work did not contact the radio; a new explicitly armed
qualification is required to produce current evidence.

## Immutable evidence

| Item | Value |
|---|---|
| Evidence root | `/var/tmp/leo-v5-production-path-e2e-20260814.gM126W` |
| Report | `production-path-report.json` |
| Report SHA-256 | `e2b4f40ac22b65876114d845f5592ef7c93e4a3fef5083aa0466f85c220f35a7` |
| Runtime | `pluto-v5-libiio-0.25-spfmeta3` passed |
| PostgreSQL | Disposable PostgreSQL 16; removed after report creation and readback |
| Migrations | 19, `0001_first_slice.sql` through `0019_dwell_request_ingress.sql` |
| Storage | Local ext4 source and output CAS; no NFS |

The report contains the SHA-256 receipt for every migration. The database was
intentionally disposable; the report captures the final rows before container
removal.

## Scoped authority

| Login | Sole capability membership | Elevated attributes |
|---|---|---|
| `wave7_capture` | `leo_capture` | None |
| `wave7_analysis` | `leo_analysis` | None |

Both logins were independently authenticated. Each had `NOSUPERUSER`,
`NOCREATEDB`, `NOCREATEROLE`, `NOREPLICATION`, and `NOBYPASSRLS`, with no
membership in the other capability roles or `leo_routine_owner`.

## Exact request and lease receipts

| Item | Value |
|---|---|
| Request | `dwell_v5_postgres_qualification` |
| Request job | `job_dwell_b4dc55a12f670663d6dd82bf4ae43a29eb6036b254c86b8757a7632e70a8d3ca` |
| Request digest | `sha256:b4dc55a12f670663d6dd82bf4ae43a29eb6036b254c86b8757a7632e70a8d3ca` |
| Stale lease | attempt 1, generation 1; heartbeat rejected after replacement |
| Active lease | attempt 2, generation 2; heartbeat and fenced completion passed |
| Result plan | `plan_dwell_v5_postgres_qualification` |
| Plan digest | `sha256:1029653d7bfdeeff16fae246ab2213e99e5ec437ea8fbcfa15ca939f5f0910a1` |
| Supervisor receipt | `sha256:84829be7d93735413c6be8b096d2bdd4e2839783a18b72cbd2a45c1dc178d63c` |

Lease tokens are represented only by SHA-256 digests in the evidence report.

## Recording and FeatureSet lineage

| Stage | Recording / FeatureSet | Exact digest |
|---|---|---|
| Qualified source scan | `rec_01M019X0KZK9JEPWPYATZ7SGTX` | recording identity `sha256:a6420a620eeadac992bc7530cc1a6e570b69352b9caea11ccba861400954516c` |
| Source analysis | `fset_ce07a923f02bda6f27c33159e8bd7186` | bundle `sha256:f4f71ca82ebd3fd5027886b8e43c2493f1fc03fd959ce5f2b44f786333326a66` |
| Live dwell | `rec_01M01YVTNC87EXM5QAJ02TP442` | recording identity `sha256:031478c5a5d93dcb59f4adda11a66c838e452d1991af6ba0768aadc28ea9f7a7` |
| Dwell analysis | `fset_c3478d2bcbbb8e63eb97a0da20584ce2` | bundle `sha256:25b824fdd9fe16256be6da9444e9e7022da767cb74d42393c9c9d441860d9213` |

The dwell analysis job was
`job_032bbb6d874400b46e401be4b27548879c8259c825a3f7020a44f85970057eab`.
It finished `succeeded` with 36 observations and 34 method scores. Its bundle
names the exact dwell recording ID and identity digest above.

The final disposable database contained exactly one dwell ingress row, two
recordings, two FeatureSets, six object rows, and three succeeded jobs: source
analysis, dwell capture, and dwell analysis.

## Capture and safety evidence

| Check | Result |
|---|---|
| Refills | 16 exact, consecutive buffer and sample sequences |
| Paired samples | 4,194,304 |
| IQ bytes | 33,554,432 |
| Missing buffers / samples | 0 / 0 |
| Continuity gaps / flags / overflows | 0 / 0 / 0 |
| IQ digest | `sha256:465232be5c5dc5a5395ff6bcee7c0ff273a3c1f27d882460383e5e47c8477b1d` |
| Metadata digest | `sha256:a358dca7263acbbf550809b65314e7f0df0d2579e958203a4f795e5d806044f1` |
| Capture spool | `cleaned`; one publication attempt; zero remaining recording files |
| Supervisor | `stopped`; one completed unit; zero failed units; healthy capacity |
| TX2 before / after / final | DDS scales all `0.0`; hardware gain `-80 dB` |

Fresh queue, supervisor, recording catalog, blob store, job repository, and
analysis-worker instances replayed the exact identities. The completed request
was not claimable, the supervisor did not open the radio, and the analysis
worker found no duplicate work.

Two setup-only failed attempts occurred before any radio contact: one caught a
host-port readiness race and one caught invalid parameterization of PostgreSQL
role DDL. Their empty temporary roots were removed. The successful PostgreSQL
container and both randomized credentials were destroyed after the report was
written and independently read back.

## Verification gates

| Gate | Result |
|---|---|
| Ruff | Passed |
| mypy `--strict` | Passed |
| Supervisor/composition tests | 14 passed |
| Focused PostgreSQL ingress/catalog/atomic-analysis tests | 17 passed |
| Full fake-radio PostgreSQL composition | Passed as part of the integration suite |
