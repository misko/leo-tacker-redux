# Gauss capture-to-dashboard setup

Status: implementation in progress; no live capture authorized or attempted

Host: Gauss (capture, deferred local analysis, PostgreSQL, CAS, and dashboard)

Repository baseline: `292840b` (`Add Wave 8 campaign readiness groundwork`)

Prepared: 2026-08-15 UTC

## Objective

Bring up the production-shaped pipeline in deliberately bounded stages:

1. qualify and capture one passive V5 radio over standard libiio IP;
2. publish its immutable paired-RX recording to Gauss-local content-addressed
   storage and PostgreSQL;
3. run analysis only after capture has finished;
4. expose recording and analysis results through the read-only dashboard;
5. repeat the same qualification for the second radio; and
6. promote to a dual-radio capture batch without allowing analysis work to
   compete with live acquisition.

No step in this document authorizes radio contact, service installation,
database mutation, or service start. Each live or mutating step remains an
operator action after its preceding evidence gate passes.

## Progress log

| UTC date | State | Evidence and decision |
| --- | --- | --- |
| 2026-08-15 | complete | Reviewed repository collaboration rules. `leo-tracker` remains a read-only numerical/reference oracle; no runtime dependency or shell/NFS control plane will be introduced. |
| 2026-08-15 | complete | Reviewed newly pulled `292840b`. It adds fail-closed offline site-readiness checking and controlled-truth campaign contracts; it does not change the V5 scan/capture deployment. |
| 2026-08-15 | complete | Confirmed the checked V5 scan composition is still a one-shot plan hard-coded to the historical radio at `ip:192.168.1.15`. Its URI, serial, radio identity, hardware snapshot, state paths, and lock cannot describe `.20` or `.21`. |
| 2026-08-15 | complete | Confirmed the new site-readiness v0.1 manifest binds exactly one capture config/radio/serial. Use it for the one-radio candidate; a dual-radio deployment needs a separately reviewed manifest/schema revision rather than overloading v0.1. |
| 2026-08-15 | complete | Confirmed capture publication does not select an analyzer or enqueue recording-analysis work. Current submission is an exact analysis-side operator action. Automatic post-batch submission is therefore explicit remaining work. |
| 2026-08-15 | complete | Traced the site-owned analysis plugin, exact recording-submission seam, PostgreSQL projections, systemd ordering, and dashboard v1. The first analysis lane publishes a `FeatureSet`; detector evaluation and a dual-batch dashboard view are distinct later capabilities. |
| 2026-08-15 | complete | Re-read the `leo-tracker` dual-Pluto operations notes and corrected 2026-08-14 synchronized-scan report as reference evidence only. Its one-process/two-thread/barrier apparatus worked, but its scientific claims were withdrawn and its measured timing was much looser than the analysis assumed. No Redux runtime dependency is proposed. |
| 2026-08-15 | resolved | Selected Redux deployment/projection tests initially had 44 passes and three new site-readiness failures. Root cause was Python 3.11.16's `fcntl` binding omitting `F_ADD_SEALS` and the `F_SEAL_*` names even though the Linux kernel supports them. The later private compatibility change closed this without weakening seal verification. |
| 2026-08-15 | complete | Confirmed the blocker is isolated: all seven site-readiness tests pass when the standard Linux seal constants are injected into that one diagnostic Python process. This did not modify source or fixtures and is not an operational workaround. |
| 2026-08-15 | complete | Ran the capture-mechanism proof set: 21 engine, plan-repository, one-shot V5 scan, dwell-request, and dwell-supervisor tests passed. This supports one common per-radio engine with separate single and synchronized orchestration paths. |
| 2026-08-15 | complete | Confirmed the working tree already contained a user change to `tests/benchmark/test_qnap_real_dataset.py`; it and the dirty `leo-tracker` worktree were left untouched. No radio, database, CAS mount, credential, service manager, or network endpoint was contacted. |
| 2026-08-15 | complete | Delegated capture, downstream, and coordination reviews. They confirmed the critical path, proposed bounded work packets, and found one important gap: the production offline worker atomically publishes a `FeatureSet` but does not invoke `PostgresAnalysisProjectionWriter`; an idempotent public projection runner is required before the dashboard can transition automatically. |
| 2026-08-15 | complete | Delegated `fcntl` diagnosis proved Gauss's kernel and headers support memfd sealing. System Python 3.14 exports the seal constants; only the uv standalone Python 3.11 binding omits them. Under that exact `.venv`, the stable Linux UAPI values applied/read back mask `15` and a later write failed with `EPERM`. Recommended closure is an integration-steward-owned, fail-closed private compatibility helper that applies and verifies all seals before invoking any loader. |
| 2026-08-15 | complete | Delegated projection diagnosis confirmed there is no projection outbox, queue, or runnable deployment. Recommended closure is an analysis-owned leased projection runner plus an integration-steward-owned durable inbox/outbox row created in the same transaction as FeatureSet publication and job success. Exact replay invokes the existing public `FeatureProjectionCommand`/writer and converges safely. |
| 2026-08-15 | superseded | A side-by-side Python 3.14 Gauss environment was evaluated as the simplest first attempt to clear `B-RDY-01`. Qualification found a small numerical receipt drift and an incompatible generic libiio path, so production approval remains on exact Python 3.11.16 with the verified private seal compatibility path. |
| 2026-08-15 | superseded | A one-shot dashboard handoff was considered for commissioning. The implemented solution is the stronger unattended transactional projection work queue/reconciler, so the temporary shortcut was not needed. |
| 2026-08-15 | complete | User authorized implementation of the complete capture-to-local-analysis/dashboard plan, a working CLI and web interface, Playwright E2E coverage, and use of `ip:192.168.1.15` as the development radio. The delegated readiness, projection, capture/CLI, analysis, dashboard, and integration packets are complete; no live radio action was included. |
| 2026-08-15 | complete | Created a side-by-side Python 3.14.4 environment at `/home/mouse9911/.cache/leo-flow/venv-py314` from the frozen lock with all extras. All 14 expanded site-readiness tests pass and seal constants are present. PostgreSQL, NumPy, and h5py install successfully. |
| 2026-08-15 | resolved | The generic Python 3.14 environment could not import `adi` because Gauss had no compatible host libiio. Capture now uses the separately pinned V5 runtime (`c26258b` patched libiio and matching generated binding); exact Python 3.11.16 remains the approved science/runtime interpreter and Python 3.14 is side-test-only. |
| 2026-08-15 | complete | Implemented and independently verified the fail-closed Linux memfd-seal compatibility path. The repository `.venv` and Python 3.14 both pass 14 readiness tests, including exact seal readback, post-seal `EPERM`, missing-binding fallback, incomplete mask, write failure, and loader-not-called cases. |
| 2026-08-15 | complete | Implemented the strict station-bound V5 operator CLI and checked development specification for `ip:192.168.1.15`. Offline `validate`/`show-plan` and explicitly armed `capture` bind the exact serial and plan digest; dual station validation rejects radio/receiver/plan/state/spool/lock aliases while permitting only the intended shared CAS. No radio or database was contacted. |
| 2026-08-15 | complete | Implemented durable FeatureSet projection work: work creation is atomic with FeatureSet publication and recording-analysis completion, while a separate generation-fenced worker resolves exact public refs and replays the existing idempotent dashboard projection writer. Focused component/static tests pass; real PostgreSQL cases remain unexecuted until a local server is available. |
| 2026-08-15 | complete | Added real-browser Playwright coverage against the loopback dashboard server. Chromium exercises activity, recording details/features, models/evaluations, tracks, storage, and accessible ready/partial/missing/error states without browser request interception. The browser/dashboard proof set passes 45 tests using user-local Chromium libraries. |
| 2026-08-15 | complete | Combined-tree static gate passes on Python 3.14: Ruff lint and formatting cover 504 files and strict mypy covers 211 source files. The broad non-hardware run produced 552 passes and 538 environment skips; its only failure is the existing ephemeris receipt golden digest under Python 3.14, while that same golden passes on the repository Python 3.11. Per repository policy, the fixture was not regenerated; interpreter-dependent ephemeris output must be diagnosed before Python 3.14 is promoted as the sole production runtime. |
| 2026-08-15 | complete | Diagnosed the Python 3.14 ephemeris mismatch without changing the golden: only three propagated floating-point values differ at roughly 1e-14 to 1e-11 (`elevation_deg`, `range_rate_m_s`, and `range_acceleration_m_s2`). This is enough to change the canonical receipt digest. The safer near-term choice is therefore the repository's pinned Python 3.11 plus the verified private `fcntl` compatibility fix; Python 3.14 remains a side-by-side qualification environment, not a drop-in replacement for numerically attested receipts. |
| 2026-08-15 | complete | Promoted the deployment and qualification boundary to migration `0020`: off-host/V5 receipts expect the exact 20-file chain; offline analysis verifies function-only access to all six projection-work routines and no direct work-table access; the historical 0018-to-0019 restore rehearsal remains isolated at 0019. Focused integration-owned tests reported 51 passes with 31 Docker-only skips. |
| 2026-08-15 | complete | Built PostgreSQL 16.10 under `/home/mouse9911/.cache/leo-flow/postgresql-16`, including `btree_gist`, and started an isolated user-owned test cluster on loopback port 55432. This avoids sudo and Docker while retaining the intended PostgreSQL major. The disposable `leo_test` database was recreated after the final migration security edit; no production database or service was changed. |
| 2026-08-15 | complete | Added an integration-steward test-harness extension for an explicit external disposable PostgreSQL DSN and ran the migration, atomic analysis, projection, and role-closure cases on PostgreSQL 16. Docker remains the default test path; the external server is opt-in and is never removed by the fixture. |
| 2026-08-15 | complete | Added the checked station-owned `leo_station.analysis_v1` package and included it in the built wheel. It pins the Quality/PSD and receiver-aggregate algorithms/configurations, Python 3.11.16, the exact `uv.lock` digest, source commit, dependency refs, CAS root, and logical execution provenance. Its JSON operator supports offline `validate`, exact idempotent `submit`, one fenced `process-one`, and one durable `project-one`; 29 focused tests and static checks pass. Python 3.14 is rejected at this approval boundary. |
| 2026-08-15 | complete | Added immutable v0.1 dual-capture batch contracts and a narrow optimistic coordinator. Independent mode binds two recordings without a synchronization claim; coordinated mode requires a common requested start and admits paired work only when measured first-sample skew is within the declared limit. Successful solo evidence survives peer failure. Contract/application evidence: 67 passes and 216-source-file strict mypy clean. |
| 2026-08-15 | complete | Cleared the real-PostgreSQL verification block with an opt-in external-test DSN while retaining Docker as the default. The fixture refuses non-PG16 servers, verifies exact migration names and SHA-256 receipts through 0020, and never stops an externally owned database. Full `tests/postgres` evidence on local PG16.10: 171 passed, one intentionally Docker-only restart rehearsal skipped. |
| 2026-08-15 | complete | Added `src/leo_station` to the wheel package inventory and built a wheel containing `analysis_v1`, `analysis_operator`, and the station package initializer. |
| 2026-08-15 | complete | Built the exact patched libiio source commit `c26258bfa33098c2b215e19cf85d448e89499b1a` user-locally, without sudo, at `/home/mouse9911/.cache/leo-flow/v5-runtime`. Under repository Python 3.11 it reports version `(0,25,c26258b)`, exposes `MetadataBuffer`, and provides `local`, `ip`, and `usb` backends. Exact SPF commit `c40ee4116546889effd72056115adaaa1bc3fd40` imports successfully, and the strict Gauss runtime manifest passed offline `.15` attestation. |
| 2026-08-15 | complete | Implemented the capture-owned dual executor over one common per-radio runner port. It runs two attempts concurrently; independent mode releases each immediately, while coordinated mode releases both from one software barrier and judges the result only from measured first-sample UTC evidence. Startup/finish/cleanup are bounded, failures are sanitized terminal facts, partial state cannot be recaptured, and no paired admission occurs before both outcomes are terminal. Capture/batch evidence: 134 passes. |
| 2026-08-15 | complete | Completed restart-safe dual operator integration: strict batch codec, private SQLite state adapter, explicit arm confirmations, two station-bound V5 runners, exact catalog/CAS result resolution, and first-refill V5 timing evidence. This adds no analysis import and performed no live radio action during implementation. |
| 2026-08-15 | complete | Added dashboard-owned v0.1 dual-batch DTOs, a new narrow V2 query port, compatible `/api/v2/capture-batches` routes, accessible cards for both attempts and analysis availability, and explicit independent-versus-measured-software-coordination wording. Failed attempts preserve observed start timing. Real-browser coverage includes ready, pending, peer-failed/solo-preserved, and excessive-skew states; 42 focused tests and 220-source-file strict mypy passed. |
| 2026-08-15 | complete | Completed integration-steward PostgreSQL persistence for immutable batch dashboard projections, least-privilege publication/query roles, normal dashboard deployment wiring, and exact migration-head promotion to 0021. The projection consumes only public batch dashboard DTOs and does not inspect CAS or capture-private state. |
| 2026-08-15 | complete | Performed the development V5 runtime's offline-only attestation under the repository Python 3.11 environment. It verified exact libiio `0.25`/commit bindings, `local/xml/ip/usb` backends, pyadi, the exact SPF metadata receiver source, native library provenance, and manifest digest `sha256:1544c390d66a2a53c9b86dc0cf7a2fab63e9fca0a08563638744121b107f431f`. The resulting station spec digest is `sha256:35243baf15a5e97e658841c779f5cb4e6bbb4a2dbfeebf08ee2cfe5885032882`. This command performed no radio or database I/O. |
| 2026-08-15 | complete | Completed the restart-safe dual operator: canonical batch codec, private WAL/FULL SQLite compare-and-swap state, offline validation/show commands, exact two-serial plus batch/pair-digest arming, independent/coordinated execution, and exact spool receipt -> catalog ref -> verified SigMF first-refill timing resolution. A real shared nonblocking `fcntl` lock spans runner construction, both captures, cleanup, and terminal state persistence; contention fails before credentials or radios and replay never recaptures. Focused evidence: 49 passes plus Ruff and mypy. |
| 2026-08-15 | complete | Completed the capture-to-dashboard publication boundary: a pure public-contract batch-view mapper and narrow writer port publish terminal initial batch state, with outage/replay tests and no dependency on dashboard-private tables. The concrete PostgreSQL writer remains integration-steward owned. |
| 2026-08-15 | complete | Added the public snapshot-to-dashboard mapper and wired dual capture to publish its exact terminal initial view. Projection outage returns a distinct failure while preserving the terminal SQLite snapshot; replay retries publication without constructing capture runners. Added offline `plan-batch`/`create-batch` with canonical exclusive output and radio-free `show-state` emitting the strict public snapshot consumed by analysis. Focused evidence: 62 passes plus Ruff and mypy. |
| 2026-08-15 | complete | Completed the final synthetic vertical proof on real local PostgreSQL/CAS/browser: two fake-radio recordings -> closed batch -> exact Gauss analysis -> durable FeatureSet projection -> batch completion -> real dashboard V2 -> Chromium, followed by idempotent replay. No live radio endpoint was in scope. |
| 2026-08-15 | complete | Revalidated the checked Gauss analysis approval after binding it to the shared user-local CAS and mode lock. The offline command reports science manifest digest `sha256:2166a8402f7840d03b43ff290079af79cc87c53502ae19db2ac20106f18e9f8c` under approved Python 3.11.16; it opened no credential, database, CAS object, or radio. |
| 2026-08-15 | complete | Added explicit public `submit-batch` and bounded `drain-batch` analysis paths. They require a canonical terminal snapshot, publish/ensure initial dashboard batch state before enqueue, compare every claimed recording ref exactly with the public catalog, preserve a successful solo peer, enqueue stable per-recording jobs, and report paired eligibility without submitting unapproved paired science. Submit, process, project, and drain all acquire the same nonblocking mode lock before credential/DB/CAS use. The full hardware-free suite passed 615 tests with 763 environment skips; Ruff, formatting across 534 files, and strict mypy across 227 source files were clean. |
| 2026-08-15 | complete | Added the final mechanical admission gate: the common lock prevents overlap, while a PostgreSQL drain-readiness function prevents a later capture from starting while prior recording analysis, FeatureSet projection, or latest dashboard recording state is still claimable/pending. This replaces reliance on process ordering or an operator checkbox. |
| 2026-08-15 | complete | Passed a complete synthetic dual-radio vertical on a fresh disposable PostgreSQL 16 database in 1.42 seconds: two exact 256-sample verified-continuity SigMF recordings in real local CAS -> public recording catalog/projection -> terminal independent batch -> exact Gauss `SCIENTIFIC` selection -> two durable jobs -> two FeatureSets -> two durable projection items -> batch-aware dashboard completion -> real loopback dashboard V2 -> Chromium. The browser saw one eligible independent batch, both recordings, and four exact Gauss feature results per recording without interception. Replaying recording publication, job submission, analysis, outbox work, FeatureSet commands, and the stale initial capture batch view left DB/CAS counts and COMPLETE dashboard semantics unchanged. No radio was contacted; the disposable database was removed afterward. |
| 2026-08-15 | complete | Added migration `0021_dashboard_capture_batch_projection.sql`: immutable versioned batch/attempt dashboard projections, V2 read routes/UI, capture- and analysis-role idempotent publication, an analysis-only exact recording-to-batch resolver, and automatic batch completion from durable FeatureSet projection retry. V1 routes and grants remain unchanged. Real PostgreSQL evidence for the completed dashboard/admission slice: 185 passes and one intentional Docker-lifecycle skip. |
| 2026-08-15 | complete | Added `capture_analysis_drain_ready()` and the restricted capture-role adapter. Both armed capture CLIs now hold the shared mode lock, verify the credential is a `leo_capture` member, set that role in a read-only bounded transaction, and refuse before cycle/runner/radio/CAS construction while any latest recording dashboard state is pending/running or any recording-analysis/projection work is ready/leased/failed. Parked and succeeded work are terminal; database uncertainty fails closed. |
| 2026-08-15 | complete | Added the authoritative [Gauss local pipeline runbook](docs/operations/gauss-local-pipeline.md), linked from the single/dual V5 guides. It provides checkpointed Python 3.11, PostgreSQL credential, offline `.15`, gated live single/dual, batch handoff/drain, dashboard/browser, replay, and stop procedures; `.20`/`.21` remain placeholders until their serial/firmware evidence is observed. |
| 2026-08-15 | complete | Final combined qualification on a fresh disposable PostgreSQL 16 database: `1393 passed, 1 skipped` (only the Docker-owned restart rehearsal), including real Chromium. Ruff and `ruff format --check` are clean across 538 files; strict mypy is clean across 229 source files; `git diff --check` and `uv lock --check` pass. The wheel builds with `leo-v5-capture`, `leo-v5-dual-capture`, and `leo-gauss-analysis` console entry points plus both `leo_flow` and `leo_station`. The exact disposable final-test database was removed after the pass; the user-local PostgreSQL server/data directory remain intact. |
| 2026-08-15 | complete | User authorized review of `/home/mouse9911/gits/pluto-plus-utils` and staged live qualification of `192.168.1.20` and `192.168.1.21`. Bounded read-only identity/TX inspection completed; capture was correctly withheld because firmware, TX-mute, existing-owner, storage, credential, and PostgreSQL drain gates did not pass. The dirty `pluto-plus-utils` worktree remained review-only and was not modified. |
| 2026-08-15 | complete | Reviewed `pluto-plus-utils`. Its normal `plutod --iio-ip` path is not an observational version probe: opening a pyadi device destroys buffers and disables TX channels, creates local SQLite state, and does not share Redux locks. Its serial and firmware fields come from the useful raw IIO context attributes, but status is cached, firmware has no content digest, model can fall back, and the API discards kernel/context provenance. For initial evidence, used the same qualified libiio context-attribute mechanism directly, with a five-second context timeout and no radio attribute writes. The utility repository was not modified. |
| 2026-08-15 | blocked | Two bounded read-only observations agreed exactly. `.20` is serial `1040005e0b100007100010000bf33a5d4d`; `.21` is serial `10400056f695001322002d0010ad1719f2`. Both report PlutoSDR Rev.C `(Z7010-AD9361)`, kernel `5.15.0-gd798b0d821b8`, and firmware `v0.38-plutoplus-spf-gain-series-v4`. Redux requires the qualified `v0.38-plutoplus-spf-libiio-metadata-v5` / source `d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8`, so neither station is admitted. SSH `/opt/VERSIONS` readback was unavailable because no accepted login credential is configured. |
| 2026-08-15 | blocked | Read-only TX inspection found all `.20`/`.21` TX2 DDS scales `altvoltage4..7` at zero, but both TX1 and TX2 hardware-gain readbacks are `-10 dB`; the passive Redux gate requires TX2 at or below `-80 dB`. No mute write was attempted because the firmware gate already failed. |
| 2026-08-15 | blocked | An existing external `plutod` process already owns `.15`, `.20`, and `.21`; `.21` reports `streaming` through its loopback API while `.20` and `.15` report ready. It listens on `0.0.0.0:8765` without remote authentication. Redux capture was not started and the external process/activity was not stopped or altered. The API corroborated both observed serial/release pairs but is cached evidence, not a replacement for the independent IIO reads. |
| 2026-08-15 | blocked | Remaining local admission gaps are explicit: no reviewed `.20`/`.21` station specs or current physical receiver mappings; configured CAS path absent; scoped capture/analysis/dashboard credentials and installed services absent; local PostgreSQL is the synthetic qualification cluster and returns `capture_analysis_drain_ready() = false` because `rec_01` remains dashboard-pending; dashboard port 8090 is not running; and the implemented source is not yet frozen in a commit. The V5 runtime and Gauss science validations pass in their exact pinned environment. No capture, tuning, firmware mutation, database mutation, or artifact creation was attempted. |
| 2026-08-15 | ready for approval | The locally reviewed firmware manifest identifies the already hardware-qualified V5 candidate: release `v0.38-plutoplus-spf-libiio-metadata-v5`, asset `plutoplus-spf-libiio-metadata-v5-d7c87a9a2809-pluto.dfu`, SHA-256 `948b46506febacb087f3955be86015e074f8c0e3370a9dfc6a942e735d97f882`, source `d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8`. Firmware change is not implied by capture authorization: it requires an explicit maintenance window and approval, should begin with the documented volatile/RAM canary on one exact radio, and must not proceed while the current external stream/daemon owns the device. |
| 2026-08-15 | complete | User explicitly assigned `.20` and `.21` for this qualification and authorized stopping the existing stream/daemon and upgrading `.20` first. The observed stream had already reached `ready`; the subsequently running `plutod` wrapper/child were sent a graceful `SIGTERM`. Both exited, port 8765 closed, and no Pluto-owning process remained. No persisted Pluto+ capture was deleted. |
| 2026-08-15 | complete | Downloaded the exact hardware-qualified V5 release into the temporary staging directory `/tmp/leo-v5-radio-upgrade.rOrpm4`. The DFU SHA-256 is exactly `948b46506febacb087f3955be86015e074f8c0e3370a9dfc6a942e735d97f882`. `pluto-plus-utils` independently validated the `0456:b673` DFU target, suffix CRC, FIT structure, and 12,743,859-byte FIT body. A broad bundle `SHA256SUMS` check reported the intentionally undownloaded provenance/build files as missing; the selected DFU itself passed. |
| 2026-08-15 | blocked | The reviewed firmware workflow has no supported network flashing method: safe persistent mass-storage and volatile/persistent DFU paths require USB. Gauss currently sees only `.15` on USB, serial `104000b29905000e17000800065934759d`; `.20` and `.21` are IP-only. Direct MTD writes over SSH are explicitly prohibited, and SSH login was unavailable. The V5 upgrade therefore pauses until `.20` is physically attached to Gauss by USB (then `.21` after `.20` qualifies). |
| 2026-08-15 | complete | With exclusive radio ownership authorized, set both TX1 and TX2 hardware gain on `.20` and `.21` from the reboot-default `-10 dB` to `-80 dB`. Independent readback verified both gains at `-80.000000 dB` and all TX2 DDS scales `altvoltage4..7` at `0.000000` on both radios. No DDS, TX buffer, tune, or receive capture was armed. Firmware remains `gain-series-v4`, so capture is still not admitted. |
| 2026-08-15 | complete | Materialized offline-only `.20`/`.21` V5 station candidates with the independently observed serials and established stable mappings: `.20` -> `radio_pluto_5d4d` -> `rx_lnb_a/rx_lnb_b`; `.21` -> `radio_pluto_19f2` -> `rx_lnb_c/rx_lnb_d`. Their isolated plan digests are `sha256:df1fe7e3bfb38cec07afa067c6347425cf283294d1745a49f067c485c8a3ed9a` and `sha256:3f75935fcad427fef273fa44ad42d9f57e2f1390c8933268d3ed4244245b57a5`; station digests are `sha256:860a1fb319b638c13efdebdf4f9514def4ec0a288ca1c3d0bccf2967cc966319` and `sha256:42e0e27718f0efd4ea05fd3c8b511922ef9b9f9aabc03e6b376d659437c266f9`. Both pass offline station and exact-runtime validation. Candidate identity/pair/arm coverage, including swapped-endpoint refusal before I/O, passes 39 tests; Ruff and strict mypy across 229 source files are clean. They remain labeled live candidates pending firmware. |
| 2026-08-15 | complete | Provisioned the user-local shared CAS candidate and isolated `.20`/`.21` recording directories at the checked paths with mode `0700`. The ext4 filesystem reports 860 GiB available, well above each station's 1 GiB minimum. This is a local-directory candidate (`require_cas_mount=false`), not an NFS control plane. |
| 2026-08-15 | complete | Prepared user-local firmware tooling without sudo by extracting Ubuntu's `dfu-util 0.11` and `u-boot-tools 2025.10` packages below `/home/mouse9911/.cache/leo-flow/firmware-tools`. The release verifier then passed all 14 checks for the staged V5 DFU: exact image hash, DFU suffix, FIT description, FPGA and ramdisk MD5s, embedded `device-fw`, component version pins/tags, and gadget source. Nothing was sent to a radio; the exact USB serial remains the next selection gate. |
| 2026-08-15 | monitoring | User requested recurring read-only firmware observation every five minutes until both IP radios report the exact qualified V5 release, followed by automatic continuation of the checkpointed pipeline. Initial observation at `2026-08-15T19:52:30Z`: `.20=gain-series-v4`, `.21=gain-series-v4`. The monitor reads only the `fw_version` IIO context attribute with a five-second timeout; it does not open pyadi, buffers, or settings. |
| 2026-08-15 | monitoring | At `2026-08-15T19:58:12Z`, `.20` timed out creating its IIO context; the first monitor invocation stopped before querying `.21`. This may indicate a reboot/firmware transition. Monitoring was immediately resumed with independent per-endpoint checks; timeout/unreachable is retained as evidence and no longer terminates the requested cadence. |
| 2026-08-15 | monitoring | At `2026-08-15T19:58:50Z`, `.20` returned as exact `v0.38-plutoplus-spf-libiio-metadata-v5`; `.21` remained `v0.38-plutoplus-spf-gain-series-v4`. The two-radio continuation gate remains closed until `.21` also reports the exact release. |
| 2026-08-15 | monitoring | At `2026-08-15T20:04:06Z`, `.20` again returned exact `v0.38-plutoplus-spf-libiio-metadata-v5`; `.21` timed out while creating its IIO context. The timeout is retained as an observation consistent with an update/reboot window, not treated as V5 evidence, and the five-minute monitor continues. |
| 2026-08-15 | exclusion | A user transient `pluto-plus-gauss.service` had restarted at `19:49:46Z`, opened `.15`, `.20`, and `.21` through `plutod`, and exposed port `8765` on all interfaces. A direct child termination was automatically restarted by the unit; the user unit was then stopped through systemd. Port `8765` closed and no matching `plutod` process remained. Redux capture remains disarmed while firmware monitoring continues. |
| 2026-08-15 | monitoring | **Firmware gate passed.** At `2026-08-15T20:09:11Z`, independent context-only observations returned exact `v0.38-plutoplus-spf-libiio-metadata-v5` from both `192.168.1.20` and `192.168.1.21` with exit status zero. The five-minute monitor stopped normally. Live work now advances to identity/metadata re-attestation and post-reboot TX-mute verification before any capture. |
| 2026-08-15 | live admission | Re-attested `.20` as serial `1040005e0b100007100010000bf33a5d4d` and `.21` as serial `10400056f695001322002d0010ad1719f2`; both expose exact V5 firmware and `iio,buffer-metadata=1`. Reboot reset TX1/TX2 gain to `-10 dB`, while all TX2 DDS scales remained zero. Restored both TX gains on each radio to `-80 dB`; independent readback returned `-80.000000 dB` for TX1/TX2 and `0.000000` for `altvoltage4..7` on both. No RX buffer, tune, or capture was opened during these checks. |
| 2026-08-15 | `.20` single canary | Armed exact station digest `sha256:860a1fb319b638c13efdebdf4f9514def4ec0a288ca1c3d0bccf2967cc966319` / plan digest `sha256:df1fe7e3bfb38cec07afa067c6347425cf283294d1745a49f067c485c8a3ed9a`. One cycle completed with forward progress in 9.5 s. Published `rec_01M03GV4T9MZ52MJHA5JJ5SDQE`: data object `sha256:7f84fa3ab008dd1be92c3d7d34e69f5a5427a40b16bbd96d6a7fcdd20258a9b1` (16,777,216 bytes), metadata object `sha256:5ff82c1af71900da21bf948f734f1dc99693d564103be775ded179ce81666533` (28,889 bytes), eight segments, dashboard state `pending`. Post-capture TX1/TX2 remained `-80 dB` and all TX2 DDS scales remained zero. |
| 2026-08-15 | analysis readiness correction | Exact `.20` analysis submission created ready job `job_476f4bf06e1e093ec737507480f58e87a20bf8c1e03b1d179355798b2c26013c`, but worker startup failed closed before claim because `leo_analysis` lacked SELECT on immutable `schema_migration` receipts. Applied the smallest isolated qualification-database correction, `GRANT SELECT ON TABLE public.schema_migration TO leo_analysis`; both the inheriting login and explicit role then read all 21 receipts. This must become migration `0022` plus ACL/preflight tests before promotion. No recapture or job replacement occurred. |
| 2026-08-15 | `.20` local analysis | Retried the preserved job after the receipt grant. Exactly one analysis unit completed, one durable FeatureSet projection completed, the latest dashboard recording state advanced `pending` → `complete`, 48 exact Gauss feature rows were visible through `leo_dashboard`, and `capture_analysis_drain_ready()` returned true. Capture and analysis remained separate processes under the shared mode lock. |
| 2026-08-15 | complete | Made the analysis receipt correction reproducible as `0022_analysis_migration_receipt_read.sql`: only `leo_analysis` receives SELECT on immutable `schema_migration`; INSERT remains denied, and `leo_capture`/`leo_dashboard` remain unable to read it. Applied 0022 to the isolated Gauss qualification database, advanced all exact-head qualification manifests/checks to 0022, and validated the real inheriting `leo_gauss_analysis_login` sees all 22 receipts before `SET ROLE` and passes the complete offline-analysis preflight. The focused migration/deployment/qualification suite passed 85 tests on a separate fresh database. The two already-succeeded analysis jobs and latest complete recording projection were unchanged; admission remained true. No radio was contacted. |
| 2026-08-15 | `.21` single canary | Armed exact station digest `sha256:42e0e27718f0efd4ea05fd3c8b511922ef9b9f9aabc03e6b376d659437c266f9` / plan digest `sha256:3f75935fcad427fef273fa44ad42d9f57e2f1390c8933268d3ed4244245b57a5`. One cycle completed with forward progress in 9.5 s. Published `rec_01M03H25Y68CZGJ4KE0E9MX35F`: data object `sha256:e9f12d66f651fd90106d6f28c197e0e57a6010b594a51232cdef91c3ecaf63ca` (16,777,216 bytes) and metadata object `sha256:9cc4a8fa1220b7c44befb6b7207b2417cce86389595b193e159946be8a7569b2` (28,375 bytes). Post-capture TX1/TX2 remained `-80 dB` and all TX2 DDS scales remained zero. |
| 2026-08-15 | `.21` local analysis | Exact submission created job `job_1a1c98be06f6d9aa6c04695aa0fccad6eb9f12472dfd34d9d6f22e26f59cc6d4`; one separate local analysis unit and one durable projection completed. Dashboard V1 then showed both `.20` and `.21` recordings in `complete`, each with eight segments and 48 feature rows. Loopback storage health reported available with 922,295,103,488 bytes free. Dashboard V2 correctly had no batch yet. |
| 2026-08-15 | live dashboard | Started the real read-only dashboard on `127.0.0.1:8090` with the scoped `leo_dashboard` credential. API checks returned both exact recording IDs as `complete`; a real headless Chromium session, with no request interception, rendered two recording buttons and independently opened each detail view with exactly 48 feature list items. UI status was `Current catalog views loaded`; batch state was correctly empty before the dual run. |
| 2026-08-15 | dual preparation | The single-canary station plans are terminal and idempotent; reusing them in a dual batch would replay the old recordings instead of opening both radios. A new checked independent-dual plan pair with new identities/digests and disjoint mutable state is therefore required before the first live dual arm. Preparation is delegated without radio contact while the verified loopback dashboard remains available. |
| 2026-08-15 | independent dual attempt 1 | Created immutable batch `cbatch_gauss_independent_20260815_v1`, digest `sha256:a4d58e4800f418b3b6e44f7f582cadfa0c102f69366b249094dd714f9cf30f28`, pair digest `sha256:3fac0b9bcc27c6746c61838a556b5e31ce15e9e6b7db805b859bddc24c1682c2`. Offline validate/show passed and the drain/RF gates were green. The live executor returned terminal failure for both attempts after about 2.4 s; the public snapshot preserved `capture_runner_failed` with no recording or observed-start claim. Private per-radio spool evidence retained the underlying identical failures: `RadioDisconnectedError: Pluto receive disconnected: [Errno 5] Input/output error`, about 2.004 s after allocation. No CAS/catalog recording was published. The batch is immutable and will not be recaptured. |
| 2026-08-15 | independent dual failure safety | Post-failure readback verified TX1/TX2 remained `-80 dB` and all TX2 DDS scales remained zero on both radios. Dashboard V2 rendered the failed two-attempt batch as `ineligible`, `analysis_state=unavailable`, with no recording IDs and no observed skew; capture admission remained ready. Investigation is focused on simultaneous IP receive/runtime concurrency because both radios had already completed their equivalent single canaries. |
| 2026-08-15 | dual isolation diagnostic | Simultaneous ordinary-buffer `iio_readdev` in two separate OS processes succeeded on `.20` and `.21`, each returning 1,048,576 bytes for 262,144 samples with exit zero. A follow-up using the exact pinned Python 3.11.16 + `adi.ad9361` + SPF `IioMetadataRx` stack in two separate OS processes also succeeded concurrently: each returned shape `[2,262144]`, scan mask `0x0f`, 2,097,152 metadata-described IQ bytes, and valid sample time in about 4.46 s. This proves the radios, 1 Gb/s link, concurrent iiOD metadata path, and V5 firmware are healthy; the failed production run is isolated to two metadata sessions sharing one Python process. Post-diagnostic TX mute remained exact. |
| 2026-08-15 | exclusion hardening | The external transient `pluto-plus-gauss.service` was relaunched again after the diagnostics. It was stopped and a reversible user-runtime mask was installed at `/run/user/1000/systemd/user/pluto-plus-gauss.service -> /dev/null`; status is `inactive` / `masked-runtime`, port `8765` is closed, and no matching `plutod` remains. The mask is scoped to this pipeline window and can be removed with `systemctl --user unmask --runtime pluto-plus-gauss.service`. |
| 2026-08-15 | process-isolation work | Capture lane is implementing per-radio spawn/exec isolation at the dual deployment boundary. The parent retains the one global mode lock, database admission and projection; each child imports the pinned runtime, owns only its radio/context/SPF session, reports ready after preflight, honors independent/common release, returns exact public evidence, and is bounded/cleaned on crash or timeout. Public capture-batch contracts remain unchanged. |
| 2026-08-15 | dual preparation complete | Added the reviewed independent-dual station pair `gauss-dual-independent-radio-20-pluto-5d4d.station.json` and `gauss-dual-independent-radio-21-pluto-19f2.station.json`. They retain the exact qualified V5 runtime, firmware, serials, hardware snapshots, receiver mappings, shared CAS, and shared pipeline-mode lock, but use new plan/activity/eight-segment namespaces plus isolated dual-only recording roots, spool databases, and instance locks. `.20` pins plan digest `sha256:a5a069532b9a83f9b6d54d2b70a86ca4c5b17ed94d8eefacacf18cadd0650c14` and station digest `sha256:f03d253310cec3f2306b4aa51fb26c1a575d96921096d1bbfab599099c0877d7`; `.21` pins plan digest `sha256:547b83e59bd5f76e7f2cf4ebf9f0392c39750323aba6969fe1d468b1979769ab` and station digest `sha256:52cb027fa5e3f5a66e5cf8793a3d63dc9d7479fafd06da01764575dffa52ae27`. Each of the same eight tunings requests exactly one 262,144 paired-sample hardware refill. Offline operator validation passed for both files; the relevant capture/integration regression passed 47 tests, with Ruff and strict mypy clean. No batch was armed, no radio or database was contacted, and the completed single-canary state was not read or mutated. |
| 2026-08-15 | dual failure diagnosis | Immutable spool timestamps place the `.20` and `.21` allocations 2.866 ms apart and their identical EIO failures only 0.101 ms apart, after 2.005875 s and 2.002908 s respectively. The independent executor releases each attempt immediately after its own preflight; requested batch start timestamps do not stagger the radio work, so both first segments overlapped. Each station has a distinct Redux provider, libiio context, device, metadata reader, spool, and recording root, and the configured 5 s I/O timeout does not match the observed interval. Existing qualification covers this standard-libiio metadata path one radio at a time and a different custom direct-IP protocol concurrently, but not simultaneous two-radio standard-libiio metadata refills. The stored exception cannot distinguish metadata-buffer open, first refill/protocol, firmware metadata construction, or a subsequent sample-counter read because those `OSError` phases are currently collapsed into one `RadioDisconnectedError`. No root cause is claimed. The next safe live investigation requires fresh immutable diagnostic plans: prove sequential one-refill recovery, keep both contexts open while serializing first refills, then test concurrent single refills in a bounded 32K/64K/128K/262144 ladder with phase-specific diagnostics and a stop on the first EIO. Coordinated full capture is not the next step, and the failed batch/plan identities remain preserved and unreused. |
| 2026-08-15 | superseded dual diagnostic hypothesis | The ordinary two-process result initially left concurrent metadata OPENM/READBUFM versus same-process runtime interaction unresolved. The subsequent exact pinned `adi.ad9361` + SPF `IioMetadataRx` two-process run (recorded above) passed both radios concurrently at the production 262,144-sample geometry. That later evidence supersedes the earlier proposed size ladder and isolates the failed batch to same-process concurrency; another full threaded or coordinated attempt is not approved. |
| 2026-08-15 | process-isolated dual ready for review | Replaced only the production dual attempt runner with one fresh `spawn` interpreter per radio; public batch/attempt contracts and CLI remain unchanged. Each child constructs its exact station/libiio/SPF cycle, completes preflight before READY, and honors the existing independent or coordinated release. The parent retains the shared mode lock, drain admission, terminal SQLite batch state, and initial dashboard projection. Children inherit no catalog secret through argv/environment, scrub ambient variables to the loader/locale allowlist, redirect native output, arm Linux parent-death SIGKILL, and are supervised through bounded CANCEL/TERM/KILL plus abort-all before projection and lock release. The fresh attempt-2 checked stations preserve the exact radios/runtime/eight tunings/one 262,144 paired-sample refill but use new plan/activity/segment/state/spool/instance-lock identities. `.20` pins plan `sha256:04575e5cfe491fec0afe5165bd39e1b034872b518b30a9976ef8f241a25ad27e` and station `sha256:2e35f6f92161ffb1b766b02889a07a4e9137b948ba90f9d2e26cb404062b3314`; `.21` pins plan `sha256:eaf951c634ff14946cc73a4c7799d786dcba3a7727b5484def4ea5738c726b01` and station `sha256:b12a5365086f115c71f5ff55297623b6f23a2edb147fb83e26e6d79f656a2fc3`. The focused capture/integration suite passed 49 tests; Ruff, formatting, and strict mypy across 230 source files were clean. No batch was planned or armed, and no radio, database, CAS, or live state was contacted. |
| 2026-08-15 | independent dual attempt 2 | Stopped and runtime-masked a newly introduced `pluto-plus-gauss-v2.service`, then re-attested both radios, V5 firmware, TX mute, empty child/process set, and database admission. Created batch `cbatch_gauss_independent_20260815_v2`, digest `sha256:797a42a16509e20dff34c69d794713afdeefcb13a033bafff7a404ae9af712d3`, pair digest `sha256:a73ce130801ff0f43a2d858c86d5be005e2fdd2ab2fd4257e080ac0c8a296372`. The process-isolated live capture completed in 7.4 s with two successful terminal outcomes and no surviving child. `.20` published `rec_01M03K1QDB1VGQ8WYC42N1W1VK`, data `sha256:7e9559aa12638798a0e61e623e62c89248906f08c4e655f9ff2b69f6dc5227eb` (16,777,216 bytes), metadata `sha256:8972ec605579dea7c775dbbbc28b8d3b0d311c36ea59f501c4a2110a8587b9b1` (29,355 bytes). `.21` published `rec_01M03K1QDD95S2G7NR3M2NPPVA`, data `sha256:7645b0ba7bde355cd894b5378f5bb889e8c8812563b2d15716ffde242a192b8e` (16,777,216 bytes), metadata `sha256:1b727b2cc9115400f63e0653177728dfa0618c4428245e70661ecf1361f31782` (29,831 bytes). Measured first-sample skew was 62,272,015 ns; mode is independent and makes no synchronization claim. Post-capture TX1/TX2 and DDS mute remained exact. |
| 2026-08-15 | independent dual deferred analysis | Exported the strict public snapshot to `/home/mouse9911/.local/state/leo-flow/v5-scan/dual-independent-attempt2-20260815-v1/cbatch_gauss_independent_20260815_v2.snapshot.json`, SHA-256 `942c7377cf8aea733dea9df0fe93177ac63b5f824d5585f73be85f04c56c043b`. Closed-batch submission verified both exact public recording refs and enqueued jobs `job_9106b572ef375932310e9b15d6a81e57d2340f2f0bbb8c9303c2d898f514e698` and `job_e99ce666eca2667ff98028e3379a1e361318c7cc6693a944cf6ecd270b63307f`. Bounded post-capture drain processed exactly two analysis jobs and two durable feature projections; both `analysis_no_claimable_work` and `feature_projection_no_claimable_work` were true. Per-recording analysis ran only after the capture batch was terminal. |
| 2026-08-15 | independent dual dashboard complete | Dashboard V2 advanced both successful attempts from `pending` to `complete` with `analysis_result_available=true`, retained the measured 62,272,015 ns skew, and labeled the batch `eligible` with coordination claim `none`. Dashboard V1 reports each new recording as `complete`, eight segments, and exactly 48 feature rows. A real loopback Chromium session, without interception, rendered one attempt-2 batch card, two complete/available attempt rows, the explicit text `Independent — no synchronization claim`, and 48 feature items for each exact recording. `capture_analysis_drain_ready()` returned true after completion. |
| 2026-08-15 | independent dual replay | Replayed the exact terminal capture arm after analysis. Operator returned `replay=true`; both spool mtimes/sizes were unchanged (`40960` bytes, mtime second `1786827044`), CAS object count remained exactly 12, and no `v5-capture-*` child existed. Exact closed-batch submission returned the same two job IDs, and bounded drain processed zero analysis jobs and zero projection items with both no-claimable-work flags true. The stale initial capture projection did not regress either COMPLETE dashboard attempt. |
| 2026-08-15 | external-owner note | A third externally named transient unit, `pluto-plus-gauss-v3.service`, was started at `20:51:21Z`, after the successful dual capture had already closed at about `20:50:44Z`. It therefore did not cause or overlap the successful radio acquisition, but it again holds the three configured contexts and demonstrates that name-specific runtime masks are not a complete ownership mechanism. No further live capture is admitted while it runs. |
| 2026-08-15 | final verification | The completed implementation passed `ruff check .`, `ruff format --check .` across 541 files, strict mypy across 230 source files, and `git diff --check`. The full local suite with the documented user-local Chromium runtime passed `648 passed, 767 skipped` in 22.02 s; skips are the Docker/externally-managed PostgreSQL variants, while the live PostgreSQL 16 migration, role, capture, analysis, projection, dashboard, replay, and real-browser paths were exercised separately above. Final read-only runtime checks found PostgreSQL running on loopback port 55433 and dashboard storage health available on port 8090 with 922,295,103,488 bytes free. The temporary runtime masks created for the original and v2 external Pluto services were removed. The v3 external service remains active, so the radios are not currently exclusive to Redux. |
| 2026-08-16 | completion audit | Re-audited the current worktree against the requested installed CLI, web interface, Playwright E2E, and `.15`/`.20`/`.21` development-radio surface. Corrected the single/dual CLI program names and help, documented every dual subcommand, clarified analysis validation as offline, and moved live-only PostgreSQL imports behind the armed command boundary. A clean Python 3.11.16 wheel installation with no optional dependencies successfully ran `--help` for `leo-v5-capture`, `leo-v5-dual-capture`, and `leo-gauss-analysis`; checked station validation and plan display covered all three development IP profiles without radio contact. |
| 2026-08-16 | dashboard release surface | Added the installed `leo-dashboard` wrapper, pinned it to the exact dashboard plugin/config contract, promoted the systemd unit to that command, and retained the generic service composition internally. The built wheel exposes all four operator entry points and contains the packaged HTML/CSS/JavaScript assets. Dashboard component and service tests cover exact forwarding and the systemd release command. |
| 2026-08-16 | historical web results | Added bounded seven-day and 30-day read-only windows. Playwright E2E now proves `.15` as a single recording with FeatureSet results and `.20`/`.21` as both recordings and a two-attempt batch, using real same-origin GET requests with no interception. Restarted only the read-only dashboard through `leo-dashboard`; a live Chromium session selected 30 days and found exact batch `cbatch_gauss_independent_20260815_v2`, both attempts, both recording IDs, and the explicit independent/no-synchronization claim. Dashboard storage health remained available with 922,295,103,488 bytes free. |
| 2026-08-16 | final release verification | Clean-wheel entry points and static assets passed; 90 focused packaging/CLI/systemd/station tests and 50 dashboard/service/browser tests passed. Repository-wide evidence: `653 passed, 771 skipped` in 23.54 s, where the skipped cases require Docker or an explicitly managed disposable PostgreSQL database; the real PostgreSQL-to-HTTP-to-Chromium vertical was separately rerun and passed during the dashboard audit. Ruff, format across 542 files, strict mypy across 231 source files, `git diff --check`, and `uv lock --check` are clean. The exact native V5 imports still pass only with their qualified `PYTHONPATH`/`LD_LIBRARY_PATH`, as designed. No capture was armed. |
| 2026-08-16 | current radio ownership | `pluto-plus-gauss-v3.service` is failed/stopped, but successor transient `pluto-plus-gauss-v4.service` started at `04:09:19Z`, listens on `0.0.0.0:8765`, and has `.15`, `.20`, and `.21` in its arguments. Redux therefore does not currently have exclusive radio ownership and no new live capture is admitted. The read-only Redux dashboard is running on loopback port 8090 through the new installed command. |
| 2026-08-16 | campaign objective | The active goal expanded to an eight-hour coordinated `.20`/`.21` campaign with equal successful coverage of the nine cells `(1.25, 2.5, 5 MS/s) × (40, 80, 160 ms)`, followed by local analysis and dashboard publication. The frozen interpretation is an eight-hour wall-clock admission window, not eight hours of RF dwell. Target cadence is 24 complete nine-cell rounds: 216 successful coordinated batches / 432 analyzed recordings, nominally one slot every 400/3 seconds and one balanced round every 20 minutes. No catch-up burst is allowed. |
| 2026-08-16 | campaign semantics | Each cell uses analog bandwidth equal to sample rate and exact per-tuning sample counts `50k/100k/200k`, `100k/200k/400k`, and `200k/400k/800k`. The 1.25 MS/s arm deliberately clips the 1.875 MHz published pilot band; Redux currently rejects that geometry, so implementation must retain default rejection and require an explicit opt-in whose immutable segment tags record clipped=true, negative guard, outside Hz/fraction, and a non-pooling warning. This follows `leo-tracker` only as a numerical/scientific oracle, never a runtime dependency. |
| 2026-08-16 | campaign execution plan | Every unit uses process-isolated `.20`/`.21` workers, a common software release, exact V5 first-sample timestamps, and a development eligibility bound of 100,000,000 ns. This is measured software coordination, never hardware synchronization. Capture must close and publish before bounded local analysis/projection starts; the next slot is admitted only after the shared mode lock and PostgreSQL drain gate are clear. A failed/excess-skew terminal batch is preserved, stops the campaign, and can only be retried with a fresh immutable identity after red/green diagnosis; successful distribution does not advance until the cell succeeds. |
| 2026-08-16 | campaign admission status | Capacity and data gates are currently green: CAS is 65 MiB with 922,123,661,312 bytes available, the shared Redux mode lock is free, and `capture_analysis_drain_ready()` is true. Radio ownership is not green: active transient `pluto-plus-gauss-v4.service` owns `.15`, `.20`, and `.21`, listens on `0.0.0.0:8765`, and has active API clients. It will not be stopped until the campaign implementation, offline matrix, durable resume, one-round qualification plan, and exact arm receipts are review-ready. No radio was contacted or capture armed. |
| 2026-08-16 | campaign numerical audit | The 24-round target is exactly 216 batches / 432 recordings / 3,456 recording segments. Across the nine-cell matrix this is 470,400,000 sample indices per radio; the fixed paired-receiver CI16 layout is eight bytes/index/radio, yielding 3,763,200,000 raw bytes per radio and 7,526,400,000 raw bytes total before metadata/artifacts. Reserve at least 15,052,800,000 bytes for CAS plus possible staging duplication, then add database/artifact/safety margin. Each of the 432 radio attempts requires a fresh plan ID; plan IDs are spool idempotency keys and cannot be reused between rounds. |
| 2026-08-16 | campaign deadline audit | Exact target time for slot `i` is `start_utc_ns + floor(i * 400,000,000,000 / 3)`, avoiding cumulative rounded sleeps. The existing dual executor uses requested UTC only as evidence and releases as soon as both workers are ready; a campaign-specific not-early/bounded-lateness gate is required before common release. Existing capture timeouts and analysis work-count bounds also need the per-slot deadline. Unit completion must verify the exact two recording-analysis successes and two exact projections; global no-claimable state alone is insufficient because parked work is terminal to the admission query. |
| 2026-08-16 | ownership audit | `pluto-plus-gauss-v4.service` has established TCP sessions to both `.20:30431` and `.21:30431`, holds the Pluto-utils daemon lock, and does not participate in the Redux pipeline lock. A free Redux lock therefore does not prove radio exclusivity. No Redux capture/analysis process or Redux lock holder was found. The failed disposable-database command from the release audit had left one password-waiting `createdb` process; it was terminated, and read-only catalog verification confirms database `leo_goal_audit_20260816` does not exist. No database object was created or removed. |

## Non-negotiable boundaries

- A radio is identified by observed serial and immutable `RadioId`, never by
  `.20` or `.21` alone. An IP/serial mismatch fails before capture.
- Each radio owns a disjoint local SQLite spool, staging directory, runtime
  directory, instance ID, and radio-scoped lock.
- Both captures may write through the same public CAS/catalog ports. They do
  not inspect each other's staging files or ORM state.
- Capture publishes recordings only. It never imports analysis, selects an
  algorithm, or writes detector results.
- Analysis reads only cataloged CAS objects. It never reads capture staging or
  treats filenames/marker files as a work queue.
- The dashboard is read-only and never opens raw IQ or CAS result objects.
- Public v1 contracts are not mutated. Any new serialized dual-batch or
  readiness contract receives a new version.
- Partial captures, failed attempts, spool state, and evidence receipts are
  preserved. Golden fixtures are not regenerated to conceal a failure.
- Component changes carry component-owned tests. Deployment files,
  cross-component integration tests, dependency changes, and ADR approval are
  integration-steward work.

## Intended host topology

```text
CAPTURE WINDOW

  V5 radio A (.20 + pinned serial)     V5 radio B (.21 + pinned serial)
           |                                      |
  capture instance A                     capture instance B
  spool/staging A                        spool/staging B
           |                                      |
           +---------- public recording ports ---+
                              |
                 Gauss-local mounted CAS
                 Gauss-local PostgreSQL
                              |
                   dual-batch completion gate

ANALYSIS WINDOW (no live capture admitted)

                 exact analysis submission
                              |
                   PostgreSQL fenced jobs
                              |
                    local analysis worker
                              |
                 FeatureSet CAS objects
                 atomic PostgreSQL projections
                              |
                 read-only dashboard service
```

The analysis worker may remain installed and healthy while capture is idle,
but jobs for a dual batch are not submitted until both capture attempts reach a
terminal state. This is the data gate that prevents the first completed radio
from starting CPU- and storage-intensive analysis while the other radio is
still acquiring.

## Baseline versus required Gauss work

| Capability | Current repository baseline | Required before promotion |
| --- | --- | --- |
| V5 capture | One immutable `.15` deployment, one radio, one refill per tuning | Parameterized, reviewed `.20` and `.21` station compositions with disjoint state and locks |
| Site readiness | Offline/fail-closed v0.1 manifest for exactly one capture | Use v0.1 for the first radio; integration-steward-owned revision for two capture instances |
| Analysis | Two durable lanes: recording -> `FeatureSet`, dataset -> `ModelSnapshot`; no production detector plugin | Site-owned exact plugin and frozen analyzer/config/dependency artifacts |
| Capture-to-analysis handoff | Exact analysis-side command for one recording | Keep manual through solo qualification; later add an idempotent closed-batch submitter |
| Analysis-to-dashboard handoff | Public projection writer exists, but the production worker does not run it and no runnable projection deployment is wired | Idempotent projection runner/outbox consumer using public FeatureSet refs; crash/replay must converge |
| Dashboard | Read-only recording, feature, model, and exact evaluation projections | Existing views after projection for the solo milestone; new public batch projection/UI for dual-radio status |
| Detector evaluation | Exact lookup exists for an already-published evaluation | Separate approved evaluation producer/projection if this report is required; recording analysis alone does not create it |
| Process ordering | Example singleton capture -> analysis -> dashboard ordering | Explicit dual capture admission and post-batch data gate; `After=` alone is not mutual exclusion |

## How capture actually runs

The checked V5 scan is not fed by a capture queue. Its systemd unit starts the
common service CLI with `--once`. That calls one injected
`OneShotV5PlanCycle.capture_and_publish_once()`:

```text
systemd --once
  -> preflight: lock, paths/mount, spool recovery, publisher, radio attestation
  -> load one exact embedded CapturePlan
  -> PlanCaptureEngine.execute(...)
       -> for each activity
            -> for each segment/tuning
                 -> optional scheduled-UTC wait
                 -> radio.acquire_segment[_with_metadata](...)
                 -> append one or more refills to the SigMF writer
       -> finalize manifest and mark the local SQLite spool complete
  -> reconcile complete recording into CAS + PostgreSQL
  -> close radio and process
```

The repeated calls are inside one plan: the engine iterates its tunings and
calls the radio's segment-acquisition operation for each. Restarting the unit
with the same plan does **not** repeat the RF capture. The SQLite spool detects
the durable recording and the cycle performs recovery/publication reconciliation
only. A routine survey therefore needs a new immutable plan ID for every scan;
restarting one old plan is not a cadence mechanism.

Redux does have a queue-shaped capture path, but for a different purpose. A
durable PostgreSQL `DwellRequest` can be claimed by the capture role and passed
to `OneShotDwellCaptureScheduler`, which gates it into one immutable plan and
invokes the same `CaptureEngine` once. The SQLite capture-plan repository is an
immutable plan store, explicitly not a scheduling queue. We should retain that
distinction in the Gauss survey admission design.

## Single, independent-dual, and synchronized-dual modes

Yes, Gauss should support all three modes. They should be two orchestration
paths over one capture implementation, not two copied versions of the radio
function:

| Selected mode | Owner and execution | Intended use |
| --- | --- | --- |
| Single | One radio-specific coordinator invokes the common plan engine | Canary, debugging, receiver qualification, ordinary solo scan |
| Independent dual | Two isolated single-radio coordinators run concurrently | More sky coverage when sample alignment is not part of the claim |
| Synchronized dual | One dual coordinator owns both radio sessions and advances them through a rendezvous at every tuning | Cross-radio experiments whose declared method requires bounded start skew |

The present `PlanCaptureEngine.execute()` and `RadioDevice.acquire_segment()`
are sufficient for single and independent-dual operation. They are not a
strong synchronized-capture primitive: `acquire_segment()` combines retune,
settle, and stream start, and matching `scheduled_utc_ns` values alone cannot
prove that two IP-attached radios actually began sampling together.

For synchronized mode, add a new capture-owned coordinator and a new narrow
radio-session port without changing `CapturePlan/v0.1` or duplicating its
writer, continuity, spool, and publication logic. The coordinator should:

1. acquire one site-mode lock plus both radio locks, then attest both serials;
2. open two independently identified recording sessions;
3. configure the same declared arm/tuning on both radios concurrently;
4. read back settings, wait for settle, and rendezvous both ready sessions;
5. issue bounded concurrent acquisition at the declared target time;
6. retain per-radio V5 refill/sequence evidence and measured start-time/skew
   evidence with its uncertainty;
7. finalize and publish two ordinary single-radio recordings independently;
8. close a new versioned batch result only after both attempts are terminal.

The batch contract should contain an immutable batch ID, two exact plan refs,
mode (`independent` or `synchronized`), arm/order compatibility, requested
start/skew bound, observed starts/skew evidence, and the two terminal outcomes.
If one radio fails, the successful recording remains valid solo evidence while
the batch is explicitly ineligible for paired analysis.

Use distinct deployment compositions/units for single and synchronized modes,
with mutual exclusion through the shared site-mode lock. This makes the
operator's scientific choice explicit and reviewable; it is not an ambient
boolean that silently changes acquisition semantics. Until measured Gauss
evidence proves a bound, call the IP-based mode *coordinated* rather than
hardware-synchronized.

## Coordination model

The root agent is the coordinator and the sole editor of this progress log. It
keeps only the current checkpoint, accepted evidence, active work packets,
blockers, decisions, and operator approvals in the main conversation. Detailed
investigation, implementation, test output, and review are delegated to
component-scoped agents.

| Role | Owns | Does not own |
| --- | --- | --- |
| Root coordinator | priorities, user gates, packet assignment, evidence acceptance, this log | component implementation or unapproved live operations |
| Capture worker | station/radio specifications, single capture, independent dual, coordinated-dual sessions, capture tests | analysis, dashboard, or deployment integration |
| Analysis worker | station plugin, recording jobs, projection runner, batch submitter, paired-analysis eligibility, analysis tests | capture internals or dashboard-private state |
| Dashboard worker | public projections, terminal states, batch presentation, read-only UI tests | CAS access or capture/analysis implementation tables |
| Integration steward | contracts/ADRs, dependencies, migrations, readiness/deployment/systemd artifacts, cross-component tests, promotion evidence | component-private implementation |

Only three worker slots run beside the coordinator, so the integration steward
rotates in when a wave reaches cross-component work. Agents receive disjoint
write sets; work that touches the same component files is serialized.

### Work-packet contract

Every delegated packet contains:

```text
Packet ID and baseline commit
Owner/component and allowed write set
Pinned input contracts and dependencies
Required outcome and explicit exclusions
Required component tests and acceptance evidence
External effects: none | read-only | live | mutating
User approval token, when required
```

Every handoff returns:

```text
Status: PASS | BLOCKED | FAILED
Files changed and contracts consumed/added
Exact tests and results
Evidence paths and digests
Known risks and decisions required
Integration-steward follow-up and recommended next packet
```

The coordinator accepts a packet only from reviewable diffs, passing required
tests, and named evidence. Subagents do not edit this file; the coordinator
records accepted results here so the main context stays compact.

### Compact status format

Routine coordination updates use only:

```text
Checkpoint: Cn — name
Overall: GREEN | AMBER | RED
Active: packet / owner / state / next evidence
Completed: evidence-backed result
Decision required: ID / choice / consequence
Blocker: ID / owner / condition to clear
Next promotion: required receipts
```

## Checkpoint roadmap

Independent dual capture is the first useful dual milestone. Coordinated dual,
automatic batch submission, and the combined batch dashboard are additive work
lanes and do not hold independent capture hostage.

| Checkpoint | Required outcome | Test/evidence gate | User gate |
| --- | --- | --- | --- |
| C0 — baseline frozen | Commit, rules, existing dirty files, current mechanisms, and gaps recorded | repository/status and review receipts | confirm intended scan purpose |
| C1 — offline foundation | readiness blocker fixed; parameterized one-radio composition; analysis identities and projection design ready | component tests, strict typing/lint/format, native readiness, static systemd verification | approve radio identities and scan arm |
| C2 — local platform | mounted local CAS, PostgreSQL roles/migrations/credentials, capacity, backup/restore, and loopback dashboard qualified | cross-role object hash/readback, role closure, mount/capacity and read-only dashboard receipts | approve Gauss provisioning |
| C3 — synthetic downstream | known/fake recording reaches local analysis, projection, and dashboard without a radio | recording -> job -> FeatureSet -> projection -> dashboard; retry/crash convergence | approve first live canary |
| C4 — radio A canary | `.20` passive capture is serial-attested, continuous, exact, and restart-safe | firmware/TX mute, V5 sequence/extent/hash, no duplicate recapture | approve full A scan |
| C5 — radio A end to end | one `.20` scan is captured, stopped, analyzed locally, projected, and visible | CAS reread, fenced job, FeatureSet, dashboard pending -> complete, resource measurements | approve B contact |
| C6 — radio B end to end | identical isolated proof for `.21` | swapped-IP rejection, disjoint locks/state, B capture-to-dashboard receipt | approve first dual run |
| C7 — independent dual | two ordinary coordinators capture concurrently; analysis waits for both terminal outcomes | multiple clean batches, no identity/path collision, continuity and capacity receipts | promote independent mode or approve coordinated trials |
| C8 — coordinated dual | two prepared sessions rendezvous after retune/settle and meet a declared measured skew bound | timing uncertainty, first-refill start evidence, partial-failure and excess-skew tests | accept skew bound and terminology |
| C9 — automatic local processing | a closed batch idempotently creates eligible jobs and the dashboard presents both results | submission/analysis/projection restart tests, ineligible-pair tests, no capture/analysis overlap | approve unattended cadence |
| C10 — operational promotion | failure, outage, low-capacity, rollback, retention, and cadence rehearsals pass | retained operational receipts and stable soak | approve steady service |

The current state is C0/AMBER. The first hard blocker is `B-RDY-01`: Gauss's
Python `fcntl` module lacks the file-sealing constants required by the new
readiness checker. C1 cannot close until the unmodified readiness suite passes
natively.

### Blocker-clearing recommendations

| Blocker | Recommended change | Why this choice | Clearing evidence |
| --- | --- | --- | --- |
| `B-RDY-01` — uv Python omits seal symbols | Add a private Linux compatibility helper in site readiness: use exported constants when present, otherwise the stable Linux UAPI values; add all seals, read them back with `F_GET_SEALS`, and invoke the loader only when the complete mask is present | Preserves the sealed in-memory, no-path-race design on every supported Linux Python build; the Gauss kernel has already proved the operation works | Missing-export test, exact seal mask, post-seal write gets `EPERM`, injected add/get failures never call loader, native seven-test readiness pass, lint/type pass |
| `B-PROJ-01` — completed FeatureSet is not scheduled for dashboard projection | Add a durable projection inbox/outbox record in the same PostgreSQL transaction as FeatureSet publication and recording-analysis job success; add a separately deployed leased reconciler that reconstructs one exact public `FeatureProjectionCommand`, invokes the existing idempotent writer, and completes or parks the item | Avoids a best-effort call and its crash window; preserves analysis success independently of a rebuildable dashboard projection; never scans CAS or private tables as a workflow | Atomic event/job/FeatureSet test, lease fencing, zero-observation case, crash before/after writer, conflict parking, role tests, synthetic recording-to-dashboard restart proof |

For `B-RDY-01`, switching `/opt/leo-flow` to system Python 3.14 is a valid
fallback, but it is broader: recreate the environment from the lock, prove all
hardware/server wheels and imports, record interpreter provenance, rerun the
full suite, and reverify every unit. It may unblock the host but would leave the
repository's supported `>=3.11` uv development path fragile. The private,
verified compatibility helper is therefore the preferred durable fix.

For `B-PROJ-01`, do not simply call the projection writer after the existing
analysis transaction. A crash after job/FeatureSet commit but before that call
would leave a succeeded result permanently shown as pending. Also do not make
dashboard projection success part of the scientific job transaction: the
dashboard is a rebuildable read model and should not decide whether analysis
succeeded. The transactional outbox plus replaying reconciler preserves both
properties.

## Parallel execution waves

| Wave | Delegated work that runs in parallel | Deliberately serialized work | Exit |
| --- | --- | --- | --- |
| W0 — offline foundation | readiness portability fix; capture station-spec refactor; station analysis/plugin design | integration steward reviews any public contract/ADR change | C1 |
| W1 — local platform | analysis plugin; FeatureSet projection runner; dashboard fixture/hardening | integration steward owns provisioning/deployment artifacts after user approval | C2 |
| W2 — synthetic pipeline | analysis component tests; projection crash/replay tests; dashboard component tests | integration steward owns the cross-component recording-to-dashboard proof | C3 |
| W3 — radio A | B-specific offline candidate preparation; dual fake/load harness | A attestation -> canary -> scan -> deferred analysis -> dashboard | C4-C5 |
| W4 — radio B | coordinated-session implementation against fakes; batch projection design | B live qualification follows explicit approval and repeats the ordered solo path | C6 |
| W5 — dual features | capture coordinator; analysis batch submitter; dashboard batch projection/UI | component changes remain isolated; integration waits for all required component gates | feature-ready |
| W6 — dual integration | fixture evidence review and operations documentation | integration steward owns deployment/cross-component tests; live independent precedes coordinated trials | C7-C9 |
| W7 — promotion | alert/dashboard review and evidence collation | outage, rollback, capacity, and cadence rehearsals | C10 |

The critical path to useful dual capture is:

```text
C0 -> C1 -> C2 -> C3 -> C4 -> C5 -> C6 -> C7 independent dual
```

Storage/PostgreSQL, analysis, projection, and dashboard work converge before a
radio is needed. Coordinated-dual development proceeds against fakes after its
capture-session design is frozen, but live C8 waits for both solo paths and the
independent-dual capacity proof.

### Initial delegated packets

| Packet | Owner | Outcome | Dependency/state |
| --- | --- | --- | --- |
| `WP-RDY-01` | readiness/capture worker | portable sealed-candidate loading with component tests; native readiness pass | ready; blocks C1 |
| `WP-CAP-01` | capture worker | immutable station specification and isolated `.20`/`.21` single compositions | needs frozen identities for final materialization |
| `WP-ANA-01` | analysis worker | site plugin seam and exact analyzer/config/dependency release plan | needs analyzer choice for scientific promotion |
| `WP-PROJ-01` | analysis/projection worker | durable idempotent FeatureSet-to-dashboard projection runner through public ports | ready for offline design/tests |
| `WP-INF-01` | integration steward | local CAS/PostgreSQL/credentials/capacity candidate and no-contact verification | design ready; provisioning needs user approval |
| `WP-SYNC-01` | capture worker | prepared-session port, dual coordinator, timing evidence, failure isolation using fakes | follows capture-session design/contract review |
| `WP-BATCH-01` | integration steward | versioned batch terminal/synchronization public contracts and ADR | required before submitter or batch UI integration |
| `WP-SUBMIT-01` | analysis worker | exact closed-batch idempotent job submitter | waits for `WP-BATCH-01` |
| `WP-DASH-01` | dashboard worker | public batch state/skew/result projection and UI | waits for `WP-BATCH-01`; existing solo views proceed now |

### Runtime exclusion and user decisions

Implementation lanes may run in parallel, but live capture and heavy local
analysis do not. Startup `After=` ordering is insufficient for repeated runs.
The integration steward must provide explicit capture and analysis operating
modes plus a durable data gate:

- capture admission drains/stops the worker before either radio opens;
- jobs do not become claimable until every expected capture attempt is
  terminal;
- one analysis worker initially processes eligible recordings after capture;
- the next capture window waits for jobs to finish or park and for projection
  replay to converge; and
- PostgreSQL is the control plane—never shell workflows, marker files, CAS
  directory scans, or capture staging paths.

The coordinator pauses only for decisions or authority that materially changes
the result:

1. intended first scan purpose and selected 2.5/5 MS/s arm;
2. permission to provision the local CAS, PostgreSQL, credentials, units, and
   dashboard proxy;
3. permission for read-only `.20`/`.21` attestation and each live canary;
4. requested synchronization bound and acceptance of *coordinated* terminology
   until measured evidence supports *synchronized*;
5. initial analyzer/result set and whether paired analysis is required;
6. dashboard exposure/authentication scope; and
7. unattended cadence, retention, and automatic-analysis policy.

Do not parallelize public-contract approval, first live A and B qualification,
capture with analysis load, the first combined capacity proof, or live
independent and coordinated promotion. Those evidence gates are what make later
concurrency safe.

## Promotion ladder

Every stage has entry criteria, a bounded action, required evidence, and a stop
condition. A stage is not complete because its command returned zero; its
receipts must also be reviewed.

### Stage 0 — freeze site identities and the first scan arm

Owner split: capture component for radio/plan composition; integration steward
for site/deployment artifacts.

Tasks:

- [ ] Assign stable logical identities for Gauss, radio A, radio B, and all four
      receiver chains.
- [ ] Read, without changing the devices, the exact serial currently answering
      `ip:192.168.1.20` and `ip:192.168.1.21`.
- [ ] Verify both devices report the exact V5 firmware release/commit,
      `iio,buffer-metadata=1`, paired scan mask `0x0f`, and native
      `I0,Q0,I1,Q1` CI16 layout.
- [ ] Record which physical LNB/feed is connected to every receiver and create
      authoritative, time-bounded hardware snapshots. Do not reuse the old
      `.15` hardware snapshot or generic receiver IDs.
- [ ] Decide whether the first scientific scan arm is the existing qualified
      transport arm or a new edge-pilot arm. The reference evidence says
      1.25 MS/s cannot contain the 1.875 MHz pilot allocation, while both 2.5
      and 5 MS/s remain defensible for different guard/cost tradeoffs; it does
      not justify a universal rate choice. Qualify the selected block-aligned
      arm on Gauss and do not silently edit the immutable `.15` plan.
- [ ] Give every new plan/ref/deployment artifact a new identity and digest.

Exit evidence:

- exact `.20 -> serial A` and `.21 -> serial B` mapping;
- receiver/LNB map and hardware-snapshot identities;
- reviewed scan-plan bytes and digest; and
- an explicit statement of whether the plan is transport-only or suitable for
  the intended edge-pilot analysis.

Stop if either IP is unreachable, an IP answers with the wrong serial, either
runtime/radio attestation differs, TX2 cannot be observed muted, receiver
wiring is unknown, or the scan arm has not been scientifically classified.

### Stage 1 — make one-radio Gauss deployment candidates

Target: radio A at `.20`; radio B remains unopened.

Tasks:

- [x] Refactor the capture-owned V5 composition so a reviewed immutable station
      specification supplies URI, expected serial, radio/receiver identities,
      hardware snapshot, plan, state root, and radio-scoped lock.
- [x] Keep `CaptureServiceConfig/v1` reference-only. Select the exact radio
      specification through an adapter reference; do not add ambient
      environment fallbacks or secret values.
- [x] Materialize a one-radio Gauss capture config and unit candidate with
      disjoint state names that will remain valid when radio B is later added.
- [x] Add capture-owned tests for wrong serial, swapped IPs, reused receiver
      identities, path/lock collisions, plan/radio mismatch, restart without
      recapture, continuity failure, and clean publication replay.
- [x] Have the integration steward update the site-readiness candidate and
      pinned digests for this exact one-radio topology.
- [ ] Run the offline readiness checker and static `systemd-analyze verify`;
      retain their receipts. These checks make no radio/database/service call.
- [x] Resolve the Gauss Python/file-sealing incompatibility recorded in the
      progress log, then rerun the new site-readiness test file without
      regenerating fixtures.

Exit evidence:

- one reviewed `.20` capture composition with no `.15` identity remaining;
- passing component tests, strict source typing, lint, and formatting;
- passing offline site-readiness receipt; and
- a destination-sorted install plan reviewed but not yet applied.

### Stage 2 — provision the local byte and control planes

Target: both capture and later analysis use local Gauss resources through public
ports.

Tasks:

- [ ] Mount a dedicated local filesystem at `/var/lib/leo-flow/objects` and
      record its filesystem and mount identity. A plain directory does not meet
      the current V5 production gate.
- [ ] Create the `leo-flow-cas` group and setgid/default ACL policy so capture
      and analysis can create/read objects while the dashboard has no CAS
      access.
- [ ] Set capacity warning, critical, reserve, and emergency thresholds using
      measured scan size and cadence for two radios.
- [x] Provision PostgreSQL 16 with all checked migrations and separate scoped
      logins/credentials for `leo_capture`, `leo_analysis`, `leo_dashboard`, and
      audit.
- [ ] Bind credentials with systemd `LoadCredential`; no DSN appears in JSON,
      environment variables, process arguments, or logs.
- [ ] Run storage-capacity, PostgreSQL role/identity, backup/restore, and
      fail-closed release-qualification checks.
- [ ] Because capture and analysis are on the same host, qualify the same local
      CAS mount through both service identities. Do not add NFS or a file-copy
      handoff.

Exit evidence:

- mounted-CAS identity and permissions receipt;
- capacity qualification;
- exact migration and role-closure receipts;
- backup/restore rehearsal; and
- successful known-object hash/readback through both capture and analysis
  service identities.

### Stage 3 — install analysis and dashboard before live capture

This makes the downstream path testable with fakes and approved existing
recordings before new radio data is produced.

Tasks:

- [x] Build and review the site-owned `leo_station.analysis_v1:PLUGIN` that
      binds the checked job repository, SigMF CAS reader, FeatureSet publisher,
      model publisher, and approved recording analyzer.
- [x] Freeze the analyzer artifact, configuration, dependencies, requested
      output schema, and their digests. Do not use `latest` or a mutable config
      filename as scientific identity.
- [x] Decide the initial result set: continuity/quality, PSD, per-receiver
      detector observations, and the bounded dashboard summary. Label
      uncalibrated scores as uncalibrated; do not turn them into detection
      booleans.
- [ ] Install one analysis worker instance initially. More workers add CPU and
      CAS contention and are justified only after timing measurement.
- [x] Configure dashboard v1 on loopback with the `leo_dashboard` credential.
      Verify it cannot assume capture/analysis roles and cannot access CAS.
- [x] Verify pending recordings are visible, successful FeatureSets appear
      through projections, and malformed/mismatched result identities fail
      before a dashboard row is exposed. If detector-evaluation reporting is
      in scope, qualify its separate producer and exact evaluation projection;
      do not imply that recording analysis creates it.
- [ ] For shared access, put an authenticated TLS reverse proxy in front of the
      loopback listener; do not bind the application to a wildcard address.

Exit evidence:

- approved analysis plugin/package provenance;
- exact analyzer/config/dependency digests;
- fake or previously published recording -> job -> FeatureSet -> dashboard
  proof on Gauss;
- dashboard authentication rejection, TLS, CSP, and read-only database proof;
  and
- measured analysis duration, peak RSS, and CAS read/write volume for one
  representative recording.

### Stage 4 — bounded live canary on radio A

This is the first stage authorized to contact a radio, and only after explicit
operator approval.

Preflight:

- [ ] Confirm host NTP synchronization and record the observation time.
- [ ] Confirm no canary, scan, dwell, transmitter, DDS tool, or other libiio
      client owns radio A.
- [ ] Confirm `.20` resolves to the pinned serial and the qualified runtime
      attestation passes.
- [ ] Read TX2 mute state: hardware gain at or below `-80 dB` and every TX2 DDS
      scale exactly zero.
- [ ] Confirm radio B is not contacted by the selected composition.
- [ ] Confirm local spool/CAS capacity exceeds the bounded attempt plus reserve.

Action and gates:

- [ ] Run exactly one passive, receive-only, multi-refill canary on radio A.
- [ ] Require one stream ID, consecutive buffer and FPGA sample sequences,
      complete stored offsets, zero gaps/flags/overflows, exact IQ extent, and
      recomputed object hashes.
- [ ] Confirm TX2 mute again after capture and confirm the context closes.
- [ ] Restart the capture process and prove the durable plan prevents radio
      reopen or duplicate publication.

Stop on any attestation, readback, continuity, extent, publication, capacity,
or TX-state failure. Preserve the spool and partial evidence; do not retry by
deleting state.

### Stage 5 — one-radio scan through deferred analysis and dashboard

Target: prove the complete operational path with radio A while radio B remains
idle.

Tasks:

- [ ] Materialize one new immutable scan plan for radio A; never reuse the
      canary or historical `.15` plan ID.
- [ ] Keep the analysis worker from claiming new work during live capture.
- [ ] Capture all planned segments, publish the data/metadata pair, and verify
      the dashboard shows the recording with analysis pending.
- [ ] After capture reaches a terminal state, submit one exact recording-
      analysis request from the analysis side.
- [ ] Run the local worker, verify its fenced lease and atomic FeatureSet
      publication, and confirm exact retry creates neither a duplicate job nor
      a conflicting result.
- [ ] Verify the dashboard transitions from `pending` to `complete` after the
      analysis job succeeds, and displays the intended bounded quality/detector
      summary, warnings, provenance, and immutable result identity.
- [ ] Record capture wall time, analysis wall time, object sizes, database row
      closure, CPU/RSS, CAS throughput, and remaining capacity.

Promotion requires a clean capture spool, a re-readable CAS pair, a succeeded
analysis job, a re-readable FeatureSet, correct dashboard projection, no radio
contact during analysis, and no scientific claim beyond the analyzer's frozen
calibration/evidence scope.

### Stage 6 — qualify radio B independently

Repeat Stages 4 and 5 for `.21` with radio B's own serial, identities, paths,
lock, hardware snapshot, plan IDs, and receipts.

Additional gates:

- [ ] Starting radio B's instance must not touch radio A's spool, lock, or
      staging directory.
- [ ] A deliberately swapped `.20`/`.21` configuration must fail on serial
      attestation before capture.
- [ ] Both radio-specific recordings and FeatureSets must be distinguishable
      in PostgreSQL and the dashboard by stable radio and receiver identities.
- [ ] Compare per-radio continuity and throughput before allowing concurrent
      acquisition; do not infer radio B capacity from radio A.

### Stage 7 — dual-radio capture with analysis held back

Owner split: capture component owns coordinated acquisition behavior;
integration steward owns dual-unit/batch deployment and cross-component proof.

Tasks:

- [ ] Choose the first dual mode explicitly:
      - independent concurrent scans, or
      - synchronized scans with a post-retune/post-settle rendezvous and actual
        sample-start evidence.
- [x] Keep the ordinary `PlanCaptureEngine` as the shared per-radio execution
      core. Add a distinct synchronized coordinator/session port rather than a
      second copy of segment writing, continuity, spool, or publication code.
- [ ] Materialize two radio-specific plans sharing a frozen experiment and
      batch/sweep identity. Each plan retains its own plan ID, radio ID, and
      digest.
- [ ] Start both captures with per-radio locks and bounded I/O deadlines.
- [x] Do not enqueue either analysis job when the first radio publishes. Close
      the batch only after both attempts are terminal.
- [x] If one radio fails, preserve and analyze the successful recording only as
      single-radio evidence. Mark cross-radio analysis ineligible; never infer
      a pair from similar timestamps or paths.
- [x] For synchronized mode, require compatible arms/orders, all expected
      segments, verified continuity, and measured sample-start skew inside the
      predeclared bound before creating a cross-radio job.
- [ ] Measure simultaneous radio throughput, local write latency, CPU, memory,
      packet errors, per-radio sequence gaps, and storage headroom.

Promotion requires multiple bounded dual batches with zero identity mixups,
zero unaccounted source gaps, no staging/lock collisions, no analysis overlap,
and reproducible pairing eligibility from immutable metadata alone.

### Stage 8 — automatic post-batch analysis

Current Redux requires an operator submission per recording. This stage adds
the missing routine handoff without weakening the component boundary.

Tasks:

- [x] Add an analysis-owned bounded batch submitter that queries public catalog
      projections for one exact closed batch; it never scans filesystem paths.
- [x] Submit one stable, content-derived recording-analysis job per eligible
      recording using the frozen analyzer/config/dependency identities.
- [ ] Optionally submit a separate cross-recording job only when both radio
      results satisfy the pairing contract.
- [x] Keep submission idempotent across process restart and repeated operator
      invocation.
- [x] Enforce the capture/analysis exclusion window in the reviewed deployment:
      capture admission closes before jobs become claimable; the next live
      batch is not admitted until the configured analysis drain/capacity gate
      passes.
- [x] Add analysis-owned tests for incomplete batches, duplicate submission,
      changed recording identities, one-radio eligibility, cross-radio
      ineligibility, and restart.
- [x] Add integration-steward tests for capture completion -> submission ->
      analysis -> dashboard transitions and failure recovery.

Do not implement this as a shell workflow, marker-file watcher, or CAS directory
scanner. PostgreSQL jobs are the control plane and CAS objects are the byte
plane.

### Stage 9 — dashboard acceptance and steady cadence

The existing dashboard can prove each recording and its FeatureSet. The batch
row and cross-radio presentation below are new dashboard component work and
must use new public projection contracts rather than joins against capture or
analysis implementation tables.

Tasks:

- [x] Present one batch row with two radio recording states, per-radio analysis
      state, warnings, and immutable result references.
- [ ] Present cross-radio results only when the pairing gate passed; otherwise
      show an explicit ineligibility reason.
- [ ] Exercise capture failure, analysis failure/retry, dashboard restart,
      database outage, CAS outage, low-capacity pause, and stale health receipt.
- [ ] Establish a cadence from measured worst-case capture, analysis, and
      publication time plus capacity reserve. Do not choose cadence from nominal
      sample rate alone.
- [ ] Set alerting for failed/restarted units, publication backlog, parked jobs,
      stale receipts, continuity failures, and capacity thresholds.
- [ ] Retain exact release, readiness, hardware, storage, health, and dashboard
      acceptance receipts for the promoted configuration.

Steady state is allowed only when a complete cycle is observable without
private state:

```text
planned -> capturing -> recording published -> batch closed
        -> analysis queued -> analysis running -> FeatureSet published
        -> dashboard complete (or explicit terminal failure/ineligibility)
```

## Initial sizing ledger

Fill this from the frozen plan rather than estimates before choosing cadence.

| Quantity | Radio A | Radio B | Combined |
| --- | ---: | ---: | ---: |
| Tunings per scan | TBD | TBD | TBD |
| Samples per tuning | TBD | TBD | — |
| Bytes per scan | TBD | TBD | TBD |
| Peak capture payload rate | TBD | TBD | TBD |
| Capture wall time | TBD | TBD | max/concurrent TBD |
| Analysis wall time | TBD | TBD | TBD |
| Feature/result bytes | TBD | TBD | TBD |
| Safe batches/hour | TBD | TBD | TBD |
| Required reserve bytes | TBD | TBD | TBD |

For orientation only, the historical one-refill eight-tuning plan is 16 MiB per
radio. A three-refill plan is 48 MiB per radio. These are not approved Gauss
values until the selected plan is frozen and its actual metadata/result/storage
overhead is measured.

## Acceptance evidence index

Populate paths/digests as work is completed. A blank entry is not a pass.

| Evidence | Identity/path/digest | Status |
| --- | --- | --- |
| Repository commit | `292840b` | recorded |
| Gauss site-readiness manifest | `deploy/site-readiness-v1/site-readiness.example.json` | candidate complete; live site receipt pending |
| Offline readiness receipt | V5 runtime `sha256:1544c390d66a2a53c9b86dc0cf7a2fab63e9fca0a08563638744121b107f431f`; station spec `sha256:35243baf15a5e97e658841c779f5cb4e6bbb4a2dbfeebf08ee2cfe5885032882` | complete for development `.15` without radio contact |
| Development radio identity/attestation | `deploy/v5-scan/development-radio-15.station.json`; serial `104000b29905000e17000800065934759d` | offline complete; live canary pending |
| Radio A (`.20`) identity/attestation | serial `1040005e0b100007100010000bf33a5d4d`; `radio_pluto_5d4d`; `rx_lnb_a`/`rx_lnb_b`; exact `v0.38-plutoplus-spf-libiio-metadata-v5` | live identity, metadata capability, and post-reboot TX-mute readback complete |
| Radio B (`.21`) identity/attestation | serial `10400056f695001322002d0010ad1719f2`; `radio_pluto_19f2`; `rx_lnb_c`/`rx_lnb_d`; exact `v0.38-plutoplus-spf-libiio-metadata-v5` | live identity, metadata capability, and post-reboot TX-mute readback complete |
| Radio A canary | `rec_01M03GV4T9MZ52MJHA5JJ5SDQE`; station `sha256:860a1fb319b638c13efdebdf4f9514def4ec0a288ca1c3d0bccf2967cc966319` | capture, deferred local analysis, durable projection, dashboard, and Chromium complete; 48 features |
| Radio B canary | `rec_01M03H25Y68CZGJ4KE0E9MX35F`; station `sha256:42e0e27718f0efd4ea05fd3c8b511922ef9b9f9aabc03e6b376d659437c266f9` | capture, deferred local analysis, durable projection, dashboard, and Chromium complete; 48 features |
| Local CAS qualification | `/home/mouse9911/.local/share/leo-flow/objects`; 12 objects after the successful live dual run | exact live publish/readback and replay stability complete; separate production mount/retention qualification pending |
| PostgreSQL migration/role closure | isolated PG16.10 `leo_gauss_qualification` on loopback `55433`; system ID `7674352851925897955`; exact migrations `0001`-`0022`; least-privilege capture/analysis/dashboard checks pass | live pipeline qualification complete; user-local process is not yet a promoted systemd service |
| Analysis plugin release | `deploy/gauss-analysis-v1/`; semantic digest `sha256:2166a8402f7840d03b43ff290079af79cc87c53502ae19db2ac20106f18e9f8c` | development approval complete |
| Analyzer/config/dependency set | `deploy/gauss-analysis-v1/science.json`; Python `3.11.16`; frozen `uv.lock` | complete for development approval |
| Radio A capture-to-dashboard run | `rec_01M03GV4T9MZ52MJHA5JJ5SDQE` | live complete: eight segments, 48 features, exact replay-safe projection |
| Radio B capture-to-dashboard run | `rec_01M03H25Y68CZGJ4KE0E9MX35F` | live complete: eight segments, 48 features, exact replay-safe projection |
| Dual capture batch | `cbatch_gauss_independent_20260815_v2`; digest `sha256:797a42a16509e20dff34c69d794713afdeefcb13a033bafff7a404ae9af712d3` | live independent capture, deferred analysis, dashboard, Chromium, and exact replay complete; measured skew 62,272,015 ns; no synchronization claim; coordinated trial pending |
| Automatic submission E2E | jobs `job_9106b572ef375932310e9b15d6a81e57d2340f2f0bbb8c9303c2d898f514e698` and `job_e99ce666eca2667ff98028e3379a1e361318c7cc6693a944cf6ecd270b63307f` | live complete: two recordings -> two jobs -> two FeatureSets -> two projections; replay processed zero new work |
| Dashboard/proxy acceptance | real loopback Dashboard V1/V2 + Chromium at `127.0.0.1:8090` | live loopback complete; authenticated TLS proxy pending |
| Capacity/retention/rollback rehearsal | TBD | pending |

## 2026-08-15 — isolated non-radio infrastructure lane

This work did not contact `.20`, `.21`, or any other radio. The contaminated
`leo_test` database on port `55432` was preserved without alteration. A separate
PostgreSQL 16.10 cluster and database were provisioned for the live-pipeline
qualification lane:

- cluster root:
  `/home/mouse9911/.local/state/leo-flow/postgresql-gauss-qual`;
- data, socket, and log:
  `data/`, `socket/`, and `postgres.log` beneath that root;
- loopback endpoint: `127.0.0.1:55433` with host SCRAM-SHA-256;
- database/owner: `leo_gauss_qualification` / `leo_gauss_admin`;
- cluster system identifier: `7674352851925897955`;
- migration inventory: exact repository hashes for all 22 migrations through
  `0022_analysis_migration_receipt_read.sql`;
- an incorrect capture password was explicitly rejected.

Four distinct non-elevated login principals were created. Capture, analysis,
and dashboard each have exactly one corresponding `leo_*` membership and no
direct ACL or owned-object exceptions. The audit login has only its reviewed
monitoring membership plus read access to migration receipts. The repository's
read-only audit functions reported `PASS` for cluster/receipt identity and for
all three runtime roles. No credential value was logged.

User-local runtime credential directories are mode `0700`; every file below is
mode `0600`:

- capture CLI:
  `/home/mouse9911/.local/state/leo-flow/credentials/gauss-capture/catalog-dsn`;
- analysis CLI:
  `/home/mouse9911/.local/state/leo-flow/credentials/gauss-analysis/catalog-dsn`;
- dashboard CLI:
  `/home/mouse9911/.local/state/leo-flow/credentials/gauss-dashboard/catalog-dsn`;
- qualification bundle:
  `/home/mouse9911/.local/state/leo-flow/credentials/gauss-qualification/`
  containing `capture-catalog-dsn`, `analysis-catalog-dsn`,
  `dashboard-catalog-dsn`, and `postgres-audit-dsn`.

The configured local CAS directory exists at
`/home/mouse9911/.local/share/leo-flow/objects` with mode `0700`. A truthful
storage-health projection was published through the analysis role: sequence
`1`, total `980666998784` bytes, free `922295103488` bytes at observation.
Capture admission returned `true` before and after that projection; job,
FeatureSet-projection, recording-projection, and batch-projection queues begin
empty.

The loopback dashboard was started with the checked V1/V2 deployment and the
dashboard-only credential. It became ready on `127.0.0.1:8090`; the storage
endpoint returned the projected capacity, V1 recordings and V2 capture batches
both returned empty pages, and `/` returned `200` with the expected defensive
headers. The smoke-test process then drained and stopped cleanly, so port `8090`
is intentionally not left listening.

Repeatable local commands:

```console
/home/mouse9911/.cache/leo-flow/postgresql-16/bin/pg_ctl \
  -D /home/mouse9911/.local/state/leo-flow/postgresql-gauss-qual/data \
  -l /home/mouse9911/.local/state/leo-flow/postgresql-gauss-qual/postgres.log \
  -o '-h 127.0.0.1 -p 55433 -k /home/mouse9911/.local/state/leo-flow/postgresql-gauss-qual/socket' \
  -w start

/home/mouse9911/.cache/leo-flow/postgresql-16/bin/pg_ctl \
  -D /home/mouse9911/.local/state/leo-flow/postgresql-gauss-qual/data status

CREDENTIALS_DIRECTORY=/home/mouse9911/.local/state/leo-flow/credentials/gauss-capture \
  .venv/bin/python -c \
  "from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider; from leo_flow.adapters.capture_analysis_drain_postgres import PostgresCaptureAnalysisDrainGate; print(PostgresCaptureAnalysisDrainGate(SystemdCredentialProvider().resolve('catalog-dsn')).ready())"

CREDENTIALS_DIRECTORY=/home/mouse9911/.local/state/leo-flow/credentials/gauss-dashboard \
  .venv/bin/python -m leo_flow.services \
  --config deploy/dashboard-v1/dashboard.json \
  --plugin leo_flow.deployments.dashboard_v1:PLUGIN --forever

curl --fail --silent http://127.0.0.1:8090/api/storage-health
curl --fail --silent \
  'http://127.0.0.1:8090/api/v2/capture-batches?start_utc_ns=0&stop_utc_ns=9223372036854775807'
```

The live qualification subsequently closed the `.20`/`.21` V5 firmware,
identity, TX-mute, receiver mapping, single-canary, independent-dual, deferred
analysis, projection, dashboard, and replay gates recorded above. Remaining
promotion work is operational rather than pipeline proof: supervise the
user-local PostgreSQL and dashboard processes with reviewed systemd units,
freeze the source worktree as a release, qualify the CAS mount/retention and
backup policy, place an ownership mechanism in front of every Pluto service
name, and choose a scientific maximum-skew limit before a fresh coordinated
batch. The active external `pluto-plus-gauss-v3.service` currently prevents
another Redux live capture.

## Coordinated nine-cell campaign preparation — 2026-08-16

The finite campaign path is implemented but has not yet collected campaign
radio data. It uses a separate nine-cell qualification followed by a main
24-round schedule: 216 successful coordinated batches, 432 recordings, and
exactly 24 successes in each `(1.25, 2.5, 5 MS/s) × (40, 80, 160 ms)` cell.
The main targets use `start + floor(i * 400000000000 / 3) ns`, with no catch-up
burst. Each attempt uses one MetadataBuffer refill per each of eight tunings;
the 1.25 MS/s arm is explicitly clipped-pilot/do-not-pool. Capture and local
analysis take the shared pipeline-mode lock as separate stages.

Implemented evidence includes the component-owned campaign policy/codecs,
FULL-sync/WAL SQLite journal, exact capacity accounting, process-isolated
software common release with a 100 ms measured first-sample skew bound,
deadline-aware capture/analysis, exact two-job/two-FeatureSet projection
receipts, and dashboard COMPLETE/result postconditions. The production
`leo-v5-campaign` wrapper consumes the no-secret checked runtime document at
`deploy/gauss-campaign-v1/runtime.json`; DSNs remain credential files and are
not placed in JSON, argv, or environment variables.

Migration `0023_campaign_projection_receipt.sql` was frozen by running the
entire PostgreSQL suite against a fresh PostgreSQL 16 database: 188 passed and
one intentional Docker-lifecycle rehearsal skipped. That database was then
removed. Only reviewed migration `0023` was applied to retained database
`leo_gauss_qualification`; the exact least-privilege analysis preflight passed
and the retained database reports 23 receipts ending at `0023`. Its capture
drain gate returned true. Local campaign capacity evidence was
922,078,224,384 available bytes, above the 15,052,800,000-byte double-copy raw
reserve plus the planned 10 GiB margin.

The fresh qualification definition is
`/home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_v5_20260816_v1/qualification.definition.json`,
with exact digest
`sha256:2bde2da882be4d76a802e20e403b3f26473a691f3e39053e36fc8a1bd3b9c2e6`.
Offline materialization validates all nine cells and 18 fresh plan identities
without DB, CAS, credential, journal, or radio access. It is not yet armed.

Release verification before radio ownership transition: 204 focused
campaign/capture tests passed; a clean Python 3.11.16 wheel exposed all five
installed commands; the full hardware-free suite passed 678 tests with 782
expected Docker/external-PostgreSQL skips; Ruff, formatting across 553 files,
strict mypy across 238 source files, `git diff --check`, and `uv lock --check`
all passed. Real PostgreSQL coverage is recorded separately above.

The current live stop gate is external ownership: transient
`pluto-plus-gauss-v5.service` still opens `.15`, `.20`, and `.21`, and the
campaign ownership gate correctly returns blocked. Before arming, it must be
stopped/runtime-masked and replaced by an exact `.15`-only service; process and
TCP evidence for `.20`/`.21` must then be clear. No qualification or main
campaign radio contact occurred during this preparation.

The ownership transition subsequently stopped and runtime-masked v5 and an
unexpected network-discovery successor v6. Exact replacement
`pluto-plus-gauss-15-campaign.service` now serves only `.15` on port 8765 and
reports one managed radio; `.20`/`.21` process/TCP ownership checks pass. Both
campaign endpoints were then observed read-only with exact V5 firmware and the
reviewed serials before any capture buffer was opened.

Real qualification v1 (`sha256:2bde2d...b9c2e6`) completed the three 1.25
MS/s cells end-to-end. Six real recordings have exact succeeded analysis jobs,
FeatureSet projection receipts, dashboard COMPLETE/result state, and live
Chromium representation. Its fourth cell (2.5 MS/s, 40 ms) stopped terminal:
`.20` recorded EBUSY during receive and `.21` retained the prior 959,687,498 Hz
tuning when 1,459,687,500 Hz was requested. The allocations began 89,077 ns
apart. No failed attempt produced an accepted recording.

Fresh qualification v2 (`sha256:3438c7...bf6bf8`) was not a replay and reused
no v1 IDs. It stopped on its first 1.25 MS/s cell when both radios returned EIO
on the first metadata capture. A following bounded sequential diagnostic used
fresh identity `sha256:a3af64...50abe1`, held the pipeline lock, stored and
published nothing, and captured one exact 50,000-sample MetadataBuffer refill
per radio successfully. The in-memory CI16 SHA-256 values were
`ad414ae4...e7abb5` for `.20` and `e2cf04bc...47addf5` for `.21`. This narrows
the live defect to intermittent concurrent metadata traffic; neither radio is
persistently wedged. Another unchanged qualification is not admitted. The next
candidate spaces `.21` dispatch by a deterministic 10 ms after the common
software release while retaining the measured 100 ms eligibility bound and no
hardware-synchronization claim; it must pass component tests before live use.

Fresh qualification v3
(`sha256:c57d88ca3d0d0a8524a6b6cfa7f936137d06863e022b0c5804fc289f45ca61c6`)
used that reviewed 10 ms secondary dispatch spacing and reused no prior unit,
batch, attempt, recording, plan, or spool identity. Its first 1.25 MS/s by
40 ms cell completed real `.20`/`.21` capture, deferred local analysis, exact
FeatureSet projection, and dashboard publication. The durable campaign status
therefore has revision 8 and the exact success vector
`[1,0,0,0,0,0,0,0,0]`. Its second cell stopped terminal before accepting any
IQ because both radios failed sample-rate readback: 1,250,000 Hz was requested
but 5,000,000 Hz remained visible. The failed recording-attempt identities are
`rec_01M04G6EW156Y70F10M0B1GRE3` on `.20` and
`rec_01M04G6EWC4JN1N77NZSCV3SPJ` on `.21`; neither became an accepted
recording. This shows that 10 ms command spacing alone does not close the
intermittent configuration-convergence boundary. Qualification v3 is retained
unchanged, no v4 identity has been armed, and the eight-hour main campaign
remains unplanned.

The next code gate is deliberately bounded and evidence-preserving: perform
each configuration write once, poll only an exact readback mismatch for at
most 250 ms at 10 ms intervals, and permit at most one 50 ms recovery retry
only when the first metadata-reader call fails with `EIO` or `EBUSY` before
any refill or callback has been accepted. Recovery closes the metadata reader,
destroys the receive buffer, reapplies and revalidates configuration, and
resets the capture-start evidence. Semantic metadata, byte-count, interleave,
callback, health, later-refill, and other transport failures remain terminal.
Successful manifests must expose the actual configuration-readback and
transport-attempt counts. A fresh live qualification is not admitted until
component and static verification of this policy is green.

That component gate is now green. The implementation is confined to
`PlutoPairedRadio` and its component-owned tests; it changes no published
contract or durable state schema. The focused Pluto tests passed 28 cases, the
full capture suite passed 215 cases, and capture-scoped Ruff plus strict mypy
passed. No radio or database was contacted by those tests. Operational
ownership, exact V5 identity/runtime, TX mute, drain, capacity, and fresh-plan
checks remain mandatory before a v4 qualification is armed.

Fresh qualification v4
(`sha256:c1aae43edd5a812207e10d0283fb56e4a3411caa7fed08de45ae78e762552ea9`)
was armed only after repository-wide verification (700 passed, 782 expected
Docker/external-PostgreSQL skips), both exact V5 runtime attestations, a true
database drain decision, a free shared mode lock, 922,019,045,376 bytes of
local capacity, and clear `.20`/`.21` process/TCP ownership. The subnet
discovery transient service was stopped and runtime-masked while the dedicated
`.15`-only service remained active.

The v4 1.25 MS/s by 40 ms unit completed real capture, exact local analysis,
FeatureSet projection, and dashboard publication. The following 1.25 MS/s by
80 ms unit also produced two valid real recordings, but its measured
first-sample skew was 103,102,777 ns, above the immutable 100,000,000 ns paired
eligibility bound, so the campaign correctly persisted it as terminal failed
and did not submit it for paired analysis. The `.20` recording is
`rec_01M04GVW0A383G375DKHAG3M00` (data digest
`sha256:9c9be6d4d8a8db99519fdc4f242258eafb614bc52c28d78305a9f2f1b5bfb615`);
the `.21` recording is `rec_01M04GVW0M3BF45JB6M5XMXQ5G` (data digest
`sha256:912fc41d78d4d0b8801df678f0dd3efb9df78cbc95ee4e377f900be9fccd4099`).
The exact observed starts were 1786858305144311094 and
1786858305247413871 UTC ns. Both first segment manifests report two
configuration-readback attempts and two metadata transport attempts; all
seven later segments on both radios report one and one. Thus the bounded
recovery worked and preserved real data, but independent recovery timing made
the pair scientifically ineligible. The prior v4 unit had 36,336,630 ns skew
and no retries on any segment. No bound was widened, no ineligible pair was
analyzed, and no terminal identity will be replayed.

The two v4 recordings were subsequently submitted through the normal public
closed-batch path as individual recording analyses. Exactly two analysis jobs
and two projection work items completed, both bounded drains reported no
currently claimable work, and the dashboard can expose the individual results
while retaining `paired_analysis_eligibility=ineligible` and
`paired_science_submitted=false`.

The next coordination revision moved only exact first-segment configuration
and readback ahead of child READY. MetadataBuffer open, READBUFM, spool
allocation, recording writes, and publication all remain after the common
release. The prepared request is exact and one-use, fails closed on mismatch,
is cleared on close, and is absent on durable replay. Repository-wide proof
after this change was 704 passed with 783 expected Docker/external-PostgreSQL
skips; Ruff, formatting across 553 files, strict mypy across 238 source files,
`git diff --check`, and `uv lock --check` passed.

Fresh qualification v5
(`sha256:079a0227da61c1d2a025930bfd24ea193e45f27ede65dddaea25506f9837e39e`)
then stopped terminal in its first 1.25 MS/s by 40 ms cell. `.21` produced and
published valid real recording `rec_01M04HBTRDAPSK14THTHBS2JRX`, data digest
`sha256:317dcc1986979421206db74375cd6993231cc15c867669355e3a0f9d412d2963`.
`.20` failed on a later tuning before accepting that segment: center frequency
1,459,687,500 Hz was requested while the preceding 1,440,312,500 Hz value
remained after the bounded 250 ms readback poll. Its failed spool evidence is
recording attempt `rec_01M04HBTR4VNP6ZDQTSZC2QQAM`, created
1786858826500583752 and failed 1786858830159166963 UTC ns. The `.21` solo
recording was analyzed and projected by one exact job/work item; the batch
remains paired-ineligible and no paired-science request was created. This
admits one further bounded driver change: before sampling only, a readback
timeout may destroy/close the unused receive state, wait 50 ms, perform one
final full configuration rewrite, and repeat exact bounded validation. Two
failed writes remain terminal; no later or post-sample configuration retry is
allowed.

The bounded rewrite implementation passed 36 focused Pluto tests, 230
capture/deployment tests, and a repository-wide gate of 708 passed with 783
expected Docker/external-PostgreSQL skips. Ruff, strict mypy across 238 source
files, formatting across 553 files, `git diff --check`, and `uv lock --check`
passed after mechanical formatting.

Fresh qualification v6
(`sha256:2265807e7e417d17a6e635cef13b4781686eb231177741e9702c2cdbaf8f4b53`)
completed all six 1.25 and 2.5 MS/s cells end-to-end: 12 real recordings, 12
exact local analysis jobs, 12 FeatureSet projection receipts, and dashboard
results. Measured first-sample skews were 8,851,138; 7,423,757; 14,465,136;
20,918,639; 12,480,209; and 4,665,430 ns. Every segment in every one of those
recordings reports one configuration write, one readback, and one metadata
transport attempt. This is direct evidence that coordinated IP control can
operate comfortably below the reviewed 100 ms bound when the first transport
path is healthy.

The first 5 MS/s by 40 ms unit stopped before release: `.21` reported
`capture_runner_failed`, `.20` was cancelled as
`capture_peer_startup_failed`, and neither side allocated a spool recording or
opened an accepted capture. A fresh sequential configuration-only diagnostic
then prepared `.21` at 5 MS/s/200,000 samples successfully while holding the
pipeline lock; it opened no MetadataBuffer and created no recording or
publication. Therefore the pre-READY failure is transient, while the current
spawn protocol deliberately suppresses its internal phase/cause. Per the
declared stop rule, no v7 qualification is admitted and no further host retry
is added. The next engineering boundary is phase-coded sanitized child failure
evidence plus review of the patched SPF/libiio/iiOD configuration and OPENM
paths; the eight-hour main campaign remains unplanned until one immutable
qualification completes all nine cells.

That phase-evidence boundary is now implemented without live I/O. A spawn
child can durably report only one member of a fixed sanitized set: child build,
cycle or host/spool/catalog/radio-attestation preflight, first-segment
configuration, capture engine, recording publication, recording resolution,
or child cleanup. No exception text, class, errno, path, credential, or device
value crosses the pipe; malformed or unknown messages remain the generic
`capture_runner_failed`. Child cleanup failure takes precedence over any
earlier phase. The dual executor preserves the validated code for the failed
attempt while coordinated peers retain the existing peer-startup fact. The
published-recording resolver is now composed lazily during observed preflight,
so its spool/catalog setup is not mislabeled as child build or run before the
cycle host guard. Verification passed 73 focused tests, the full capture
component suite (241 passed), Ruff and formatting on all affected files, and
strict mypy across all 238 source files. No radio, PostgreSQL service, or live
state was contacted.

Fresh qualification v7 and scientific invalidation (2026-08-16T06:19:55Z)
preserve a materially different result. Definition digest
`sha256:8fde686789a7c423f4100bebe6933591261c7f581ac813a7a7791df7129c8728`
completed unit 0 end-to-end with exact recordings, analysis, projection, and a
measured first-sample skew of 7,766,373 ns. Unit 1 then stopped terminal with
the fixed `capture_engine_failed` fact on both attempts. The read-only failed
spool evidence says both radios exhausted the bounded center-frequency
readback while requesting 1,209,687,500 Hz and retaining 1,440,312,500 Hz.
The v7 journal, recordings, failed attempts, CAS objects, database rows, and
dashboard projections are retained and must never be re-armed or rewritten.

A subsequent byte-level read-only audit invalidates the apparent v6 and v7
capture successes for scientific use. Every sample at every tuning in all 12
v6 recordings, both v7 unit-0 recordings, and the earlier independent-dual
attempt-2 recordings is one repeated CI16 tuple per radio: `.20` contains
`(1549, 137, 0, 0)` and `.21` contains `(208, 1838, 0, 0)`. Earlier
single-radio canaries contain varying values in all four components, so the
catalog, CAS, writer, and analyzer are not being treated as the source of the
constant payload. The narrow unqualified boundary is the pinned SPF/pyadi
ordinary-RX-to-MetadataBuffer transition. Historical SPF tests that validate
RF content perform a real ordinary dual-channel refill, discard it, destroy
the ordinary buffer, and only then open the metadata buffer; the Redux path
did not perform that content-bearing prime. Length-only metadata tests did not
detect this defect. All existing v6/v7 analysis and dashboard rows remain
valuable pipeline evidence but are explicitly not valid science results.

One bounded unpublished `.20` diagnostic then attested the exact radio and TX
mute under the shared mode lock, configured the first 1.25 MS/s tuning, and
failed opening the metadata session with EBUSY. It published no recording and
changed no campaign journal. This is consistent with a second lifecycle seam:
the pinned SPF open path creates and immediately destroys an unfilled ordinary
buffer before `OPENM`, while iiOD acknowledges `CLOSE` before its worker has
necessarily completed provider teardown. No further radio run is admitted
until the content-bearing prime, metadata-open transition, and a deterministic
teardown/completion boundary are component-tested, then proven with fresh
immutable diagnostic identities. The 100 ms skew goal is not widened to hide
this defect.

Hardware-free repair checkpoint (2026-08-16T06:30:45Z): Redux now injects an
explicit Gauss V5 signal-integrity policy through the attested production
radio provider. It requires every RX1/RX2 I/Q component to vary within each
refill and raises before the refill callback, so the known held tuples cannot
reach spool, CAS, analysis, or dashboard. Generic `PlutoPairedRadio` behavior
is unchanged and reports `signal_integrity=not_validated` unless a caller
selects a validator. The affected capture/deployment suite passed 251 tests;
Ruff and strict mypy across 238 source files passed.

The owning SPF checkout now has an unpromoted, hardware-free patch that
replaces its unfilled-buffer transition with exactly one real, discarded
ordinary dual-channel refill after release. It validates complex `(2, N)`
geometry, destroys the ordinary buffer in `finally`, and retries only an
`OPENM` EBUSY three times at 50 ms. Nine focused tests prove distinct
asymmetric prime and metadata payloads, exact ordering, cleanup, bounded EBUSY
behavior, and no retry for other errors. The owning libiio checkout has an
unpromoted server-side barrier: final-client `CLOSE` retains its reference and
waits until the RX worker destroys the buffer, closes the metadata provider,
marks the entry closed, and clears device data; non-final ordinary clients do
not wait. Its regression delays provider close by 200 ms, beyond the legacy
50 ms open retry, then immediately reopens. These dependency edits are not yet
committed, pinned, built into a host runtime, built into radio firmware, or
run on hardware. The libiio binding also has an explicit idempotent
`Buffer.close()` whose destructor delegates to the same destroy-exactly-once
path. Its Python test and independent strict C translation-unit compilations
passed; the full CMake build remains unavailable because this host lacks `m4`
and its whole-tree Werror encounters an unrelated libxml2 deprecation. The
installed V5 runtime and both radios remain unchanged, and no fresh
qualification is admitted yet.

The repair sources are now copied into Redux as immutable integration-owned
overlays rather than being identified by dirty dependency worktrees. Candidate
`gauss-v5-rx-integrity-close-barrier-1` binds the exact existing Gauss runtime
manifest digest `sha256:1544c390...f431f`, libiio base commit `c26258b...b1a`
plus patch digest `sha256:195bddce...f901d4`, and SPF base commit
`c40ee411...fd40` plus patch digest `sha256:c9113a6d...669b1`. Clean detached
copies of both exact base commits accepted their respective patches with
`git apply --check`. The candidate manifest also records exact base and
post-patch file hashes and requires distinct host-runtime and firmware release
identities, a radio flash, a fresh single-refill qualification, and a fresh
nine-cell qualification; prior constant-IQ results are explicitly ineligible.
The runtime source-manifest tests pass 5/5. No current runtime/station identity
was changed and no radio was contacted.

A separate host candidate was then materialized without altering the qualified
runtime. Candidate manifest
`deploy/v5-runtime/gauss-rx-integrity-candidate.manifest.json` has digest
`sha256:0a9cf278bf836655afbf7a9a324a21c5dc41235d1b251386a0013eb0f299f123`,
uses isolated runtime/SPF roots, and names a distinct future firmware release.
The exact candidate environment passed the real runtime verifier; imported
`iio.py` had digest `sha256:a138280c...1191`, reported base native version
`(0, 25, c26258b)`, exposed the explicit `Buffer.close`, and the patched SPF
file matched `sha256:3c7f87be...4058`. Runtime source/manifest tests now pass
6/6. This proves the host composition only. The radio-side iiOD bytes are not
built or installed, so hardware capture remains prohibited.

Radio-rootfs build checkpoint (2026-08-16T07:01:37Z): the exact libiio
close-barrier overlay was applied to pinned radio base
`c26258bfa33098c2b215e19cf85d448e89499b1a` inside the clean
`plutosdr-fw` source at `de830094a177daf4f577b60b9d3324b41f99ae58`.
Buildroot cross-compiled the metadata-enabled `iiod` and `libiio` for ARM
EABI5 hard-float, then the repository-owned rootfs target generated the
normal legal/MSD assets and packed an exact candidate rootfs. The immutable
receipt is `deploy/v5-runtime/rx-integrity-candidate.radio-rootfs.json`;
`rootfs.cpio.gz` is 7,115,868 bytes with digest
`sha256:a57ed73a07693b3ac94a456a87392897d49430dc8a6d9cc7aa10b0ed37642269`.
The archive stamps
`device-fw v0.38-plutoplus-spf-libiio-metadata-v5-rx-integrity-candidate1`;
its stripped ARM `iiod` digest is
`sha256:87a65a439b323ac6aa75cc52004f398796b4336e5c50c73c7a1c4c62a0995fd5`
and its `libiio.so.0.25` digest is
`sha256:29e24ce1f175a1c35ee25ee601ca95bca85689eb33686711fbe1df98e1eb827c`.
The source hash for patched `iiod/ops.c` matched the reviewed expected digest
`sha256:612162a0...aa01` before compilation. The runtime receipt test passes
7/7 with Ruff and diff checks clean.

This is a verified rootfs payload, not a flashable firmware release. No
attested `system_top.xsa`/`system_top.bit` was locally available, so no full
ITB/FRM was packaged; neither radio was contacted or changed. Both radios
remain on the prior V5 release, and fresh signal-integrity/nine-cell capture
is still blocked until a complete candidate firmware package is assembled,
reviewed, installed, and re-attested. The earlier direct Buildroot packaging
attempt stopped safely on a missing generated `LICENSE.html`; rerunning via
the repository-owned rootfs target closed that packaging seam without
altering source inputs.

Full radio-firmware packaging checkpoint (2026-08-16T07:12:14Z), which
supersedes only the rootfs checkpoint's packaging blocker: the exact published
V5 release assets were resolved and retained by digest. The release bundle is
102,428,236 bytes at
`sha256:013f6174bd989e0e9f5cfc36179141f38fe905f730561899f2361b7f834f4007`;
the release XSA is 801,344 bytes at
`sha256:05d5b9d17cab1c0efc208686309ffbb7f22cd341c302247b999421bf22211cb8`;
and the base V5 DFU is 12,743,875 bytes at
`sha256:948b46506febacb087f3955be86015e074f8c0e3370a9dfc6a942e735d97f882`.
The FPGA bitstream extracted from the XSA matched the exact base DFU. The
candidate FIT was then assembled from the three exact base device trees, exact
base FPGA bitstream, exact base kernel, and the candidate rootfs. Extracting
all six candidate FIT components proved the first five byte-for-byte equal to
the base release and the sixth byte-for-byte equal to the recorded candidate
rootfs.

The new immutable receipt is
`deploy/v5-runtime/rx-integrity-candidate.radio-firmware.json` at
`sha256:04127cf7571f74f3c1d9a992bbb6931ad0068b5d804c7bec9d75a1ddae8da57c`.
The candidate ITB is 12,712,727 bytes at
`sha256:7e78616a52deea6a4055e5ec51ab71b59c431e24b359e3ab16e0fac000717efd`;
its MD5 is `650a7c07ba6f30cf5b71437d6c25821d`. The DFU is 12,712,743
bytes at
`sha256:4118a4f3a7130e407f4314e76415bbcf9183501e74faae1824ef2b52be616503`
and has a valid 16-byte `0456:b673` DFU suffix with CRC `7d976188`. The FRM
is 12,712,760 bytes at
`sha256:df391788052ef9a647d0b7b530e33dffea874f53f0e2beff9cd5f0228c25b8bb`;
its footer contains the exact ITB MD5. The firmware receipt and its component
test pass 8/8 with Ruff, formatting, JSON, and diff checks clean.

This closes flashable-format assembly only. Neither `.20` nor `.21` was
contacted, flashed, rebooted, or changed during the build. Both remain on the
prior V5 release. Promotion still requires a reviewed sequential installation
with rollback bytes retained, exact IP/serial/release re-attestation, TX-mute
proof, a fresh single-refill signal-integrity gate, and then a fresh
synchronized nine-cell qualification. No earlier frozen-IQ recording becomes
eligible because this package exists.

Read-only pre-install audit (2026-08-16T07:13:37Z): qualified context-only
`iio_attr` returned
`v0.38-plutoplus-spf-libiio-metadata-v5` from both `192.168.1.20` and
`192.168.1.21`. No process held a TCP session to either radio's IIO port and
no Redux capture/mode lock was held. The only active `plutod` was the reviewed
`.15`-only instance and its command line named no `.20` or `.21` endpoint. The
exact prior V5 DFU remains locally available at
`sha256:948b46506febacb087f3955be86015e074f8c0e3370a9dfc6a942e735d97f882`
for rollback. This was observation only; neither radio was changed.

Host-prime/old-radio boundary diagnostic (2026-08-16T07:23:05Z): before any
firmware installation, a fresh `.20` station definition bound the patched
candidate host runtime to the still-installed base V5 radio release. It used
fresh plan
`plan_v5_hostprime_oldradio_pluto_5d4d_20260816_d1`, plan digest
`sha256:651c9443c5baff6a63c1b7b727f04feecdd416c78cadb1f4ba42d7fadd19e419`,
station digest
`sha256:5260b330bf34136b738a48b61d16d0704ad06bce6b3c7c55e74bbbce326a644a`,
and isolated state/spool paths. Offline station and exact candidate-runtime
validation passed; the database drain gate was true, no `.20`/`.21` owner was
present, and TX-mute attestation remained mandatory.

The armed capture stopped before publication with exit 4. Read-only spool
evidence for recording `rec_01M04Q2SGC9EA8TXEDYEQ20PC9` is exactly
`RefillError: Pluto IQ refill has a constant component`; it was allocated at
`1786864821772621566` UTC ns and failed at `1786864822507077149` UTC ns. No
recording reached CAS/catalog/dashboard. The terminal plan will never be
replayed.

A separate bounded, non-publishing boundary probe then compared an ordinary
pyadi refill with the following SPF metadata refill under the same lock,
admission, serial/firmware, tuning, and TX-mute checks. Both exact 2x100,000
matrices collapsed to the identical tuple `(1549,137,0,0)` for every sample;
each of the four components and the tuple had unique count 1, and both exact
CI16 byte streams had digest
`sha256:34ea489bdf8c3241555de0fb8f801e4256b80fa6a8ccc2d1735f1cbba000c267`.
The metadata frame itself remained structurally valid at first-sample sequence
`463903478757`. This disproves the narrower hypothesis that Redux publication
or only metadata-to-pyadi conversion creates the constant IQ: on the current
radio runtime, even the immediately preceding ordinary refill is already
frozen. It also proves that the host-only ordinary-prime patch cannot qualify
the current radio firmware. The candidate radio iiOD teardown build remains
the next controlled diagnostic; no firmware was installed in this checkpoint.

Two exact candidate-firmware qualification station files are now checked for
`.20` and `.21`. They bind candidate runtime digest
`sha256:0a9cf278...f299f123`, exact candidate firmware source+patch identity,
fresh hardware snapshots, fresh 2.5 MS/s x 40 ms one-refill plans, disjoint
state/spools/instance locks, and the shared CAS/mode lock. Their exact
plan/station digests are `.20`
`sha256:33d00b74...fa9ad50` / `sha256:a9febb7e...b12c97c` and `.21`
`sha256:e9889626...419b80` / `sha256:1404f324...9b1d8c`. The station loader now
accepts the candidate only as a second exact reviewed identity and requires
its exact runtime ID, manifest digest, patched libiio identity, and patched SPF
identity; mixed or unreviewed builds fail closed. Runtime/station/integration
coverage passes 28 focused tests with Ruff, formatting, strict mypy, JSON, and
diff checks clean.

Firmware-admission audit immediately afterward found exactly one physically
attached Pluto USB identity: `.15`, serial
`104000b29905000e17000800065934759d`, at `/sys/bus/usb/devices/3-8` with its
matching mass-storage and serial interfaces. Neither `.20`/`...5d4d` nor
`.21`/`...19f2` was present in USB sysfs or block-device identity links. The
guarded Pluto+ updater intentionally requires an exact USB serial/sysfs path
for volatile DFU and persistent QSPI operations. Therefore no firmware action
on `.20` or `.21` is currently admitted from Gauss. An SSH `/dev/mtd*`, remote
QSPI, boot image, or identity-unbound network shortcut will not be used. The
runbook now records the exact sequential volatile-canary-persistent-cold-proof
gates and uses the candidate station pair for later nine-cell/main planning.

Offline updater-preflight checkpoint (2026-08-16T07:30:07Z):
`leo-v5-capture verify-firmware` now requires the independently reviewed
firmware-receipt SHA-256 and verifies the complete local chain without loading
credentials or contacting PostgreSQL, CAS, or a radio. It hashes both linked
runtime/rootfs receipts and the ITB/DFU/FRM; validates exact artifact sizes,
the DFU `0456:b673` suffix and inverted CRC32, and the FRM's exact
`ITB + lowercase MD5 + newline` construction; rejects symlinks/non-regular or
oversized files; and refuses a build receipt that claims installation or
hardware qualification. The real command passed with receipt
`sha256:04127cf7...ae8da57c` and returned the exact previously recorded three
artifact digests. Mutation, false-proof, symlink, wrong-receipt-digest, CLI
sanitization, and normal operator coverage pass 22 focused tests; Ruff,
formatting, strict mypy, and diff checks are clean. `.20` remains absent from
USB, so no updater plan or firmware mutation occurred.

The combined candidate runtime/firmware/station/operator/integration/package
regression then passed 52/52 tests. Ruff and formatting were clean across all
eight affected source/test modules, strict mypy passed the three affected
source modules, and the complete scoped diff check passed.

The current-tree dashboard/browser regression also passed 54/54 tests in
2.29 seconds using real local Chromium and the documented user-local runtime
libraries. This covers the dashboard component, deployment composition, Gauss
campaign operator service surface, and real browser HTTP behavior; it does not
substitute fixture data for the pending fresh candidate-radio capture.

Third physical-admission audit (2026-08-16T07:31:17Z): the exact candidate
receipt/artifact verifier still passed and PostgreSQL capture admission still
returned true. No process held an IIO TCP session to `.20` or `.21`. USB sysfs
again contained only `.15` serial `...4759d` at `3-8`; `.20` serial `...5d4d`
and `.21` serial `...19f2` remained physically absent. This is the third
consecutive goal turn with the same hardware condition after all safe offline
firmware, station, CLI, package, dashboard, and browser work was exhausted.
The goal is therefore blocked at the guarded volatile-install boundary until
`.20` is attached to Gauss over USB and the candidate test is explicitly
authorized. No unsafe network/QSPI substitute was attempted.

Authorized SSH reboot and post-reboot capture evidence
(2026-08-16T10:27:30Z): the existing mode-`0600` Pluto password file was used
through a temporary askpass helper that was deleted immediately afterward; no
password entered argv, output, or this log. One Redux pipeline-mode lock covered
both commands. `.20` and `.21` accepted `/sbin/reboot`; their distinct ED25519
keys were added to the protected known-hosts store with fingerprints
`SHA256:BOH5bUg7e3fJxFkl5p0cJNKVEw/cxswYGPxBQOn5va8` and
`SHA256:dWXjoo8Vllzr5VJe7sPuj+JApX9vUblJ/m0hzT0I/Qc`. `.21` returned on the
first poll and `.20` on the sixth. Both again reported exact base release
`v0.38-plutoplus-spf-libiio-metadata-v5`.

Reboot restored TX2 gain to `-10 dB`, so the first fresh `.20` attempt stopped
in radio attestation before allocating any recording. Both radios were then
put back into the explicit passive state under the mode lock: TX channels
disabled, both hardware gains `-89.75 dB`, all DDS scales zero, and the exact
serial/release pair rechecked. Reusing that still-unconsumed preflight identity
then allocated terminal recording `rec_01M051PG605BP16RG554WSCXCH` and failed
configuration after the bounded two-write policy: requested `2,500,000` Hz,
read back `5,000,000` Hz. Plan
`plan_v5_hostprime_oldradio_pluto_5d4d_20260816_d2`, digest
`sha256:5abfb66e5bf40b9b4e54856a409e5000bb1b5338f935379646f03f4e1a944838`,
is terminal and will not be replayed.

A third fresh `.20` diagnostic selected the supported `5 MS/s x 40 ms` cell:
plan `plan_v5_hostprime_oldradio_pluto_5d4d_20260816_d3`, digest
`sha256:c17052be3f36a8d121aa90c4b3acf5b8886f9ccd2d8430a94899cfe711a6fc97`,
station digest
`sha256:d816abecfe1e74c03dafd5acb16c9b0c3b1de7426b6091ba62fe09c38aa9ef27`.
Configuration succeeded, but recording `rec_01M051RG42PTT3244DZ5BS6C3M` stopped
on exact `RefillError: Pluto IQ refill has a constant component` after about
0.903 seconds. It did not reach CAS, catalog, analysis, or dashboard. Thus a
full device reboot does not repair the frozen RX payload, and the result is
not a lower-rate artifact. Reachability/control health does not make the
current radio bytes scientifically usable. The integrity gate remains enabled;
an eight-hour repeated-tuple run is prohibited.

The same post-reboot `5 MS/s x 40 ms` real-radio gate then passed on `.21`
without weakening any capture or integrity policy. Fresh plan
`plan_v5_hostprime_oldradio_pluto_19f2_20260816_d1` has plan digest
`sha256:f42142d707107969515e33754e9a2c16f2daed4b1e1a18cb58a73a67347b26a0`
and station digest
`sha256:15a9e7b92735f90e2d0a31db9dabb7258cfa32472e6b8ddc3c067a45fd0449c1`.
It produced and published recording `rec_01M051WGPB0TPTS4FP20C1D9S4` across
all eight exact tunings. The public catalog/CAS identity is
`sha256:3bda860274ca0289614de11cc6ffc32e54e3025c855e5709077278990a9a66a5`;
the 12,800,000-byte data object is
`sha256:ae217532954ed60b44eb641a867c300f4730a93149fe0c7389427aa9dcfe00da`
and its metadata object is
`sha256:6a545a9f56b1be087af4434790f69b2f2eb43cc171c600c24b2e82d373c3340e`.
All eight segments have verified continuity, distinct payload digests, and
variation in every I/Q component of both receiver streams. Capture started at
`1786876150483519262` UTC ns and finished at `1786876155243440194` UTC ns.

Only after capture had closed, the exact published recording was submitted to
the local Gauss analysis queue as
`job_a80a9e0b01a9c5e4b555d76749dc2983efe7a1b9f9f4eb10c317ce418d68f9f0`
under science manifest
`sha256:2166a8402f7840d03b43ff290079af79cc87c53502ae19db2ac20106f18e9f8c`.
One bounded analysis unit completed, followed by one bounded durable feature
projection unit. The loopback dashboard then moved the recording from
`pending` to `complete`, reported the exact eight segments, and exposed 48
feature rows containing the approved `sample-quality` and `compact-psd`
results. No radio was open during analysis or projection. This is a successful
real `.21` capture -> CAS/catalog -> deferred local analysis -> dashboard
pipeline traversal. `.20` remains isolated by its preserved constant-IQ
terminal evidence; no `.20` result or dual synchronization claim is inferred
from the `.21` success.

Current scan-inventory audit (2026-08-16): no eight-hour main campaign was
materialized or run. The local durability layer contains 46 terminal recording
attempts: 32 cleaned/published recordings and 14 failed attempts. The public
dashboard exposes those same 32 recordings (15 from `radio_pluto_5d4d`, 17
from `radio_pluto_19f2`), all with completed analysis, totaling 294,308,864 raw
CI16 bytes, 256 eight-tuning segments, and 20.595564689 aggregate per-radio
sample-seconds. This duration is a sum across individual recordings, not
simultaneous wall-clock dwell.

The dual-batch stores contain 21 terminal batches: 19 coordinated and two
independent. Thirteen coordinated batches produced two recordings; 12 had
measured first-sample skew below the declared 100 ms limit and one measured
103,102,777 ns and was rejected by the qualification state machine. Five
coordinated batches failed on both attempts and one had a successful/failed
split. Of the two independent batches, one failed on both attempts and one
published a pair. The seven qualification journals contain 19 attempted cells,
12 complete analysis/projection receipts, and seven terminal failures. There
is no main-campaign journal, no 216-batch schedule execution, and no accepted
eight-hour receipt.

An exact public-catalog/CAS read of every stored recording further separates
inventory from usable science. Only three single-radio recordings have
variation in every I/Q component across every segment: initial `.20`
`rec_01M03GV4T9MZ52MJHA5JJ5SDQE`, initial `.21`
`rec_01M03H25Y68CZGJ4KE0E9MX35F`, and post-reboot `.21`
`rec_01M051WGPB0TPTS4FP20C1D9S4`. Together they contain 46,354,432 bytes and
2.312296514 aggregate per-radio sample-seconds. The remaining 29 published
recordings are constant or partly constant under the current integrity check.
None of the 13 coordinated recording pairs, nor the one successful independent
pair, contains two variable recordings. Therefore the current scientifically
usable synchronized-dual inventory is zero; older successful control-plane
receipts must not be presented as usable synchronized RF data.

Dashboard all-interface deployment (2026-08-16T15:19:51Z): at explicit
operator request, the default loopback-only HTTP policy remains unchanged and
a separate exact adapter `dashboard.stdlib-explicit-remote-http-v1` was added
for deliberate remote binding. The checked Gauss dashboard configuration now
selects that adapter at IPv4 wildcard `0.0.0.0:8090`. The prior unmanaged
loopback process stopped cleanly and the same dashboard was restarted as the
transient user unit `leo-dashboard.service` with `Restart=on-failure`; its
catalog credential remains supplied through the protected credential
directory rather than argv. The live socket is exactly `0.0.0.0:8090`, and
read-only storage-health requests succeeded through loopback,
`192.168.1.142:8090`, and Tailscale `100.105.69.63:8090`. The service has no
application authentication or TLS, so this is intentionally unauthenticated
cleartext telemetry on every permitted IPv4 interface. Component/deployment
coverage passed 16 tests; focused Ruff, formatting, strict mypy, and diff
checks passed before restart.

Dashboard capture-detail audit (2026-08-16): the live interface currently has
an in-page `Recent recordings` table and a selected-recording detail card. A
recording selection loads its radio, UTC interval, activity kinds, analysis
state, segment count, recording-object availability, and independently
projected feature rows. The latest valid `.21` recording exposes 48 approved
`sample-quality` and `compact-psd` rows there. There is no dedicated recording
detail route, waterfall/spectrogram canvas, or typed waterfall-tile API in the
current dashboard. The requested capture-detail-plus-waterfall experience is
therefore only partially implemented; a future waterfall must be produced by
post-capture local analysis and published through a narrow public projection,
not by making the read-only dashboard scan or decode CAS objects directly.

Starlink-pilot detection inventory (2026-08-16): the current production Gauss
analysis approval runs only `quality-compact-psd-v0.1`; it does not run a
Starlink pilot detector or emit a pilot decision. Across all 32 dashboard
recordings there are 1,536 projected feature rows: 1,024 `compact-psd` peak to
median ratios and 512 `sample-quality` RMS-magnitude values. There are zero
typed pilot-detection outputs. Consequently the defensible confirmed-pilot
count is zero while the number of pilots actually present is unknown. PSD peak
ratios must not be relabeled as detections without an approved detector,
threshold/calibration, and recording-level decision contract; 29 historically
published constant/partly-constant recordings are also ineligible evidence.

Continuous collection and capture-detail work began as three component lanes:
(1) a durable capture-first/deferred-analysis dual-radio collector that reuses
fresh immutable campaign identities and stops on integrity/skew failure, (2) a
bounded post-capture waterfall result with exact recording/segment identity and
no dashboard CAS access, and (3) an additive dedicated capture-detail dashboard
route with browser coverage. The analysis lane is also performing the requested
read-only review of `/home/mouse9911/gits/leo-tracker` reports and algorithms as
a numerical/reference oracle for pilot-detection strategy; it will not become a
Redux runtime dependency. Live continuous RF collection is not yet claimed or
started while these boundaries are under implementation and `.20` retains its
latest constant-IQ terminal evidence.

The requested `leo-tracker` pilot-strategy review completed read-only against
`reports/starlink-detector-evaluation/REPORT.md`,
`reports/sync-scan-cross-radio-2026-08-14/REPORT.md`, and
`src/leo_tracker/radio/beacon/`. The reference signal model has eight known
edge pilots at 234.375 kHz spacing in a 1.875 MHz block and a 750 Hz frame
cadence. Recommended Redux work is an additive, versioned suite rather than a
single winner: PSS/pilot template acquisition over bounded epoch/CFO cells;
`anchor-8`; differential 16/32; GLRT 32/64; and disjoint full-frame
acquire/verify/full statistics. Coherent sums belong inside a frame/symbol set,
with noncoherent frame combination and retained frame maxima. The measured
20 ms/5 MS/s leading group was statistically unresolved among full-frame and
GLRT methods, and detector ordering changed by capture arm. A 1.25 MS/s arm
clips the pilot block and measured about a 6.1 dB penalty versus 2.5 MS/s.

The porting boundary is deterministic templates reimplemented from checked
numerical vectors, immutable candidate certificates with exact recording,
segment, receiver, sample-window, epoch, CFO, method/config/dependency, score,
control, margin, searched-cell, and frame-support identities, plus calibrated
per-cell false-alarm artifacts and disjoint acquire/verify evidence. Explicitly
excluded are PSD/waterfall pixels or raw fires relabeled as detections, pooling
searched and conditioned results, searched rolled-code controls, selected-point
cross-edge nulls, within-Pluto channels treated as independent radios, pooled
LO corrections, detector agreement treated as truth, or any runtime import or
private storage convention from `leo-tracker`.

Capture-detail and waterfall live handoff (2026-08-16T16:12Z): migrations
0024 through 0026 were first exercised on a fresh disposable PostgreSQL 16
database; the focused inactive-gate, waterfall atomicity, detail/waterfall
projection, migration, security-definer, and real PostgreSQL-to-browser tests
passed 31/31, and the disposable database was removed. The exact same three
migrations were then applied to the isolated Gauss qualification database,
which now has 26 immutable migration receipts. Their SHA-256 values are
`0024=9049293d2f14ec0b2dd118c9f88d12a563752e48e10e0ce98c3870f008d41a49`,
`0025=91e5afe6e839e0de25098af1575d01d4b663a542e30534d3834aff0c9ff1a0b9`,
and
`0026=b646fd3349d220bdaa8ee5f5c74cf418b13a86c79ac2bb2af8d75d4e174dbb9a`.

The live `leo-dashboard.service` was restarted against the new V3 composition
and is healthy on the explicitly requested `0.0.0.0:8090` socket. The known
variable `.21` recording `rec_01M051WGPB0TPTS4FP20C1D9S4` was replayed through
the public capture projection, without changing its catalog or CAS objects, to
add its immutable recording-detail projection. After capture was already
closed, the separate local-analysis commands submitted exact waterfall job
`job_d54f8be3a514cbeeb6a7755aef06587d812616c9fc2b949b2fb494b61ddda4bf`,
processed it from the exact CAS recording, and completed its durable dashboard
projection under science manifest
`sha256:253ed2ab11e5bed02b1013890395a02257eb665d804c48ad06e1301edefce0ac`.
The live V3 API reports eight capture segments and a complete 16-tile
segment/receiver waterfall. A real headless Chromium request, with no routing
or interception, rendered the dedicated recording page with all 16 tile
choices, the canvas figure visible, 48 existing projected analysis rows, and
zero failed HTTP responses. No radio was contacted during projection,
waterfall analysis, or this browser proof.

Fresh continuous-collection admission remains deliberately separate from that
successful dashboard handoff. Offline qualification-v8 is materialized at
`deploy/gauss-campaign-v1/qualification-v8/qualification.definition.json`
with exact definition digest
`sha256:abd4416ae05ffbf40348c6dd42aecab47ff926ca07d56b542517bca552956d3f`,
18 fresh per-attempt station documents, a 100 ms paired-skew bound, and no
fabricated qualification assertion. Its status remains `not_asserted`; there
is no live 9/9 qualification journal or receipt and therefore no admitted
216-batch/eight-hour main continuous collection yet.

Qualification-v8 live result (2026-08-16T16:15Z): the explicitly armed,
bounded run acquired the campaign and shared mode locks and contacted the real
`.20`/`.21` radios only after offline identity validation, 912,040,624,128
available local bytes, no `.20`/`.21` process or TCP owner, and live PostgreSQL
`capture_analysis_drain_ready=true` plus `capture_analysis_inactive=true`.
The `.15` Pluto daemon remained isolated to `.15`. The first fresh coordinated
cell was 1.25 MS/s for 40 ms. `.21` succeeded and published real recording
`rec_01M05NP67HH85B1E1MY22NRVRY`; `.20` failed before a first accepted sample.
The bounded read-only spool diagnostic records exact `.20` failure
`RefillError: Pluto IQ refill has a constant component` for recording allocation
`rec_01M05NP677WCYGR97B1YJW84Q0`. Batch
`cbatch_qual_gauss_v5_20260816_v8_u000_s000_r00` is terminal and ineligible,
with no observed paired skew. The journal is revision 3 with zero successful
cells and zero accepted rounds. It will not be replayed, no qualification
receipt was emitted, and no main continuous/eight-hour collection was started.

The successful solo `.21` evidence was retained. Only after the failed pair had
closed, local Gauss analysis submitted exact job
`job_6a82908983d03ff3e274cea403239d771e13c066ea4c4dad6b8a7268aa670c9a`
and waterfall job
`job_99e370485ba86e2cb5106142743850df9a73c2a16ea5269e1132c0a38778aa0d`;
both processing and durable dashboard projection completed. Real Chromium now
renders that recording's dedicated page with 16 waterfall tiles and 48 feature
results and no failed HTTP response. Both PostgreSQL capture gates returned
true again afterward. The live dashboard inventory is now 33 published,
analysis-complete recordings: 15 from `.20` and 18 from `.21`; there are 1,584
distinct projected feature results, two recording-detail projections, and two
waterfall recordings. The newest failed/successful pair is visible as an
ineligible coordinated batch. Scientifically usable synchronized-dual inventory
remains zero.

Post-stop ordinary-DMA localization (2026-08-16): a bounded diagnostic ran
under the same nonblocking pipeline-mode lock and PostgreSQL inactive-analysis
gate, with no publication and TX disabled. On `.20`, four separate ordinary
dual-channel pyadi receive sessions at the failed cell geometry (1.25 MS/s,
50,000 samples, first edge tuning) returned the same SHA-256 digest
`7e3cc12200f76f5f920d39ca2d8f42ac83faf3760d727bd8d612d0f10c879759`;
all four I/Q components had exactly one unique value in every session. A
matched `.21` control returned a variable first session (unique component
counts 798/799/1905/1903), then a constant second session after buffer
destroy/reopen. This proves the failure exists in the ordinary radio receive
buffer lifecycle before MetadataBuffer, Redux interleaving, CAS, analysis, or
dashboard code. It also disproves a host-only policy of adding one or several
ordinary warm-up refills: `.20` never recovered across four fresh sessions.

The checked radio-side teardown-barrier candidate remains the smallest reviewed
repair boundary. Its complete offline receipt reverified successfully as
`sha256:04127cf7571f74f3c1d9a992bbb6931ad0068b5d804c7bec9d75a1ddae8da57c`
with exact DFU digest
`sha256:4118a4f3a7130e407f4314e76415bbcf9183501e74faae1824ef2b52be616503`.
The USB inventory still contains only `.15`; neither `.20` serial `...5d4d`
nor `.21` serial `...19f2` is physically attached. The reviewed installation
policy therefore cannot perform its identity-bound volatile USB load. No SSH
MTD write, unreviewed network daemon replacement, gate relaxation, or repeated
terminal qualification was attempted.

Additional local Pluto addressing (2026-08-16): SPF experiment and calibration
reports were used as the identity oracle, then the identities were independently
read from the attached USB gadgets. R17 is serial
`104000bac4950008230026001b440a003a` and R18 is serial
`1040007c4a94000211000b009186843ef2`. Both radios reported firmware
`v0.38-plutoplus-spf-libiio-metadata-v5`. Their wired-Ethernet addresses were
configured through the exported `config.txt` `[USB_ETHERNET] ipaddr_eth` field,
not through a direct `fw_setenv` command: R17 was assigned `192.168.1.17` and
R18 was assigned `192.168.1.18`. Each edit was made in the exact radio's VFAT
configuration image after serial verification, then processed by the radio's
normal `/sbin/update.sh` config parser after a real mass-storage detach. The
resulting environment value was verified before reboot. After reboot, qualified
V5 `iio_attr` probes over IP returned the exact expected serial and V5 firmware
from both `.17` and `.18`. This changes the physical wired-Ethernet addresses;
the USB gadget endpoints remain their separate USB transport. No RF capture,
PostgreSQL operation, or dashboard mutation was part of this addressing step.

R17/R18 USB-then-IP comparison preparation (2026-08-16, offline only): four
fresh immutable station documents were materialized for the same exact serial
pair. The current local USB inventory maps R17 serial
`104000bac4950008230026001b440a003a` to `usb:5.3.5` and R18 serial
`1040007c4a94000211000b009186843ef2` to `usb:3.14.5`; the corresponding IP
documents use `ip:192.168.1.17` and `ip:192.168.1.18`. The USB station
specification digests are
`sha256:28329cb184ea0fe81b3b131f3c8483998a44b7871296ae1fda7328480426c4f5`
and
`sha256:ca34fe0dbf88d9d60a98e1c64c083b2ee3e13dc501a585ce6dc98447ada3f154`;
the IP station specification digests are
`sha256:8728ffb9d1da699de4cd0b0dbd7060cabd318cc9181ddba29dcf90bf9ca23f94`
and
`sha256:4da1c842a8a2dd904acad4767815f9dd7d7d4ee3c36e084ae02bb3ff11648b49`.
Their plan digests, in the same USB-R17, USB-R18, IP-R17, IP-R18 order, are
`sha256:d3270702d6f39fa64dfa4bec22f5745066144ced113b4679b5a63da337c241d1`,
`sha256:64bec57896aab98274dc4c70a14cb797bb08691c455b373d4c1a8126d31ea00b`,
`sha256:8eb70516360b707d49bdb8628caa7ea3b5425a5f7cf3b70d4f8577302505be10`,
and
`sha256:19d0b75de3e705145b1d010b8834bc07e69e15d296e0cf759f0dc05a8019b3c5`.
All four use exact base V5 radio firmware with the pinned host-prime runtime,
2.5 MS/s, 2.5 MHz bandwidth, 100,000 paired samples (40 ms) for each of eight
edge tunings, and the full 1.875 MHz pilot band. Plan, activity, hardware
snapshot, mutable state, spool, and radio-lock identities are distinct; CAS and
the exclusive pipeline-mode lock are shared. Focused tests, Ruff, mypy, and
synthetic coordinated plan/validate checks passed with the unchanged 100 ms
measured first-sample eligibility gate. The synthetic batches were deleted.
No live batch was frozen: each production batch must be created immediately
before its run with a fresh requested UTC, and its emitted batch/pair digests
must be copied verbatim into that run's arm confirmation. No radio context,
credential, PostgreSQL connection, CAS path, or capture state was opened during
this preparation.

R17/R18 USB comparison v2 preparation (2026-08-16, offline only): the USB v1
station documents were retained unchanged and a new v2 pair was materialized at
the same exact current USB URIs, serials, geometry, firmware, and runtime. R17
uses plan `plan_v5_r17_usb_current_20260816_v2`, plan digest
`sha256:e899cbdc6f989cb08da7e757b28ee6742b58c3eadb13d952118f01144bafdbc6`,
and station specification digest
`sha256:460cf9b0c955e0bb8caeba9e0019d944db54ece984f9f7c4ce3c09e0a2837b3d`.
R18 uses plan `plan_v5_r18_usb_current_20260816_v2`, plan digest
`sha256:275ef7f27216d64e7da8c3d30466c43d6ffa010697e195188c0d3d1d9d0ca626`,
and station specification digest
`sha256:3828dc192c870bb824106b6527b9e2a42ae9c6eed0a6dba983768454526ec8f7`.
Both plan/activity IDs, hardware snapshot IDs, state roots, recording roots,
spool databases, and radio-lock paths are fresh and disjoint from USB v1. The
shared CAS and exclusive pipeline-mode lock remain unchanged. The IP v1 station
documents were not modified. A synthetic coordinated batch with the unchanged
100 ms measured first-sample bound passed offline plan and validation, then was
deleted; 23 focused tests, Ruff, and mypy passed. No production USB v2 batch was
frozen, and no radio, database, credential, CAS, or mutable capture path was
opened.

R17/R18 live USB-then-IP comparison (2026-08-16): admission began with no
`.17`/`.18` process, TCP session, or Redux lock owner, approximately 912 GB of
available local storage, and PostgreSQL reporting both
`capture_analysis_drain_ready=true` and `capture_analysis_inactive=true`.
The first immutable USB batch,
`cbatch_gauss_r17_r18_usb_20260816_v1`, stopped before sampling because both
attempts reported `capture_radio_attestation_failed`. Read-only localization
showed that both exact radios had all four TX2 DDS scales at zero but TX2
hardware gain at `-10 dB`, above the passive-capture ceiling of `-80 dB`.
That terminal batch was preserved and never replayed. After exact USB serial
verification, only TX2 hardware gain was set to and read back as `-89.75 dB`
on each radio.

A wholly fresh USB v2 batch was then planned and armed with batch digest
`sha256:830a0d07d6a3c589bc3ac6caa3ef2f4fd2f9b591eb2167426317538cc7c259a9`
and pair digest
`sha256:78191e2901a25a08290b8c924adb0f27deda61d135cb59d7070aba06bee90c10`.
Both real USB attempts succeeded. R17 published recording
`rec_01M05R6A8WF7CKQAZJN6J2WE9G`; R18 published
`rec_01M05R6A8W2EGG51E1GG2TVX0R`. Each recording contains eight 40 ms
tunings and 6,400,000 sample-data bytes. Their exact observed first-sample
timestamps differ by 11,194,008 ns, inside the declared 100,000,000 ns bound.
Only after capture closed, bounded local Gauss analysis completed exact jobs
`job_a5788da7128dc3a78db80ec92370f12cacd79ffdb85abf1cc673e1c36291af05`
and
`job_e97dcd35266772f12686eb0e8fa689abded3d06766081f0b4d0eeaa0536296e3`;
both durable feature projections succeeded and paired-analysis eligibility is
`eligible`.

After the strict capture-analysis drain gate reopened, a distinct IP batch for
the same physical radios was planned with batch digest
`sha256:fd9d18f441bc9c34fc12b964a4d30d5fde2b76574ef7a513ab9ddc72e3a8f0e0`
and pair digest
`sha256:4c97c945196b44dbe967c903f91d61c67c78d0bddd6232045daec82fdc1d6b9c`.
Both real IP attempts succeeded through `ip:192.168.1.17` and
`ip:192.168.1.18`. R17 published
`rec_01M05RAWPWY6Z2MVFBRW27YB0K`; R18 published
`rec_01M05RAWPVC1EK3ZBASK3M2TXX`. Each again contains eight 40 ms tunings
and 6,400,000 sample-data bytes. Their observed first-sample timestamps differ
by 727,842 ns, within the same 100 ms bound. Post-capture local analysis jobs
`job_e890a16e493ebf36f0456c0fe3e97e8868b211ecc9c36067ae29c62fae6efdf8`
and
`job_d216da05b1748b0acab5f52a9c2c8cd1df677495d46b5e97cf344b3eb7063298`
and both feature projections completed; paired-analysis eligibility is
`eligible`.

Separate bounded waterfall analysis and projection then converged for all four
new recordings: four processing calls and four projection calls made forward
progress, followed by explicit no-progress calls. Live dashboard APIs now
report eight capture segments, 48 projected feature results, a complete
16-tile waterfall, and HTTP 200 for each dedicated recording page. Real
headless Chromium, without interception, rendered all four pages with eight
segment rows, 48 analysis rows, 16 selectable waterfall tiles, a visible
canvas, and no failed HTTP response. The two batch APIs report measured skews
of 11,194,008 ns (USB) and 727,842 ns (IP), both attempts complete with result
availability, and `measured_software_coordination`. These are real paired-radio
captures and real data, but they are not a hardware-synchronization claim; the
ordinary dual operator still does not enforce its requested UTC marker. Final
PostgreSQL gates again report drain-ready and analysis-inactive, with no
`.17`/`.18` TCP or Redux lock owner.

R17/R18 IP qualification-v1 preparation (2026-08-16, offline only): a wholly
fresh nine-cell qualification was materialized under
`deploy/gauss-campaign-r17-r18-v1/qualification-v1/` from the checked R17
`ip:192.168.1.17` / serial `104000bac4950008230026001b440a003a` and R18
`ip:192.168.1.18` / serial `1040007c4a94000211000b009186843ef2`
source stations. Campaign `qual_gauss_r17_r18_20260816_v1` has exact definition
digest
`sha256:8a397b50d6f1a7d9801dacce532cf39e22a1cda8e328b97e9cf4cee7dcd37790`;
the 18-station materialization manifest digest is
`sha256:85c74f263d02653e8a268ce391984e124117b86776e6d86e6995cbf55e35210a`.
The manifest binds the candidate host runtime digest
`sha256:0a9cf278bf836655afbf7a9a324a21c5dc41235d1b251386a0013eb0f299f123`,
base V5 radio firmware/source, standard libiio IP transport, a 100 ms measured
first-sample skew bound, TX2 hardware gain no greater than -80 dB, and exact
zero scale on all four TX2 DDS channels. It asserts no qualification success.
Offline validation proves all nine matrix cells and 18 unique plan, activity,
station-specification, state-root, recording-root, spool-database, and
radio-lock identities, while preserving one shared CAS root and exclusive
pipeline-mode lock. The paired `.17`/`.18` runtime configuration parses at the
same checked boundary. The campaign validator, materialization validator, 32
focused campaign/runtime tests, Ruff, and mypy all passed. No journal,
qualification receipt, radio context, database, credential, CAS object, or
mutable campaign state was created or opened during this preparation.

Waterfall-aware full-drain hardening (2026-08-16, offline/disposable database):
additive migration `0027_capture_analysis_waterfall_drain.sql`, SHA-256
`c57cd825f3f2e7c4baf6bf3bc26822baf39fd9ddab2044c1259644db9ee74b38`,
extends `capture_analysis_drain_ready()` so waterfall-analysis jobs and
waterfall projection work in `ready`, `leased`, or `failed` state hold the
initial capture drain closed; `parked` and `succeeded` remain terminal. The
function retains owner `leo_routine_owner`, fixed SECURITY DEFINER search path,
and capture-only EXECUTE authority. Static/readiness checks passed 59 tests,
Ruff/format, and strict mypy. A fresh disposable PostgreSQL 16.10 database on
the separately authorized test cluster passed 25 migration/security/state and
14 production-path receipt tests, then was dropped with the original database
and login inventory restored. This evidence does not claim application to the
qualification database; live qualification remains held until migration 0027
is applied and independently verified there.

R17/R18 IP qualification-v1 live result (2026-08-16): the exact additive
migration 0027 bytes above were applied through the repository migration path
to the intended PostgreSQL 16 qualification database on port 55433 (system ID
`7674352851925897955`, database `leo_gauss_qualification`). All 27 receipt
rows and their repository hashes matched, the head was exact 0027, and a
second application was a no-op. The two admission functions remained owned by
`leo_routine_owner`, SECURITY DEFINER with fixed
`search_path=pg_catalog, pg_temp`; only `leo_capture` retained runtime EXECUTE.
The analysis, dashboard, and maintenance runtime roles could not execute them.
Login-role closures and all queue counts were unchanged by migration, and both
the full-drain and analysis-inactive gates returned true.

Immediately before arming, the candidate host runtime identified itself as
`gauss-pluto-v5-rx-integrity-close-barrier-1`; `.17` and `.18` attested their
exact planned serials and base V5 firmware, metadata capability, mask 15, two
channels, and `I0,Q0,I1,Q1` layout. Both TX2 gains were `-89.75 dB` and all
four TX2 DDS scales were exactly zero. No foreign process/TCP owner or lock was
present, both PostgreSQL gates were true, the fresh campaign state root was
absent, and 911,944,462,336 bytes were available against an exact
11,364,618,240-byte remaining-capacity requirement.

The immutable qualification definition
`sha256:8a397b50d6f1a7d9801dacce532cf39e22a1cda8e328b97e9cf4cee7dcd37790`
then completed 9/9 cells with one accepted success in every sample-rate/dwell
cell, no retry, and 18 unique real recordings. Every paired attempt passed the
100 ms measured first-sample bound. Cell skews in matrix order were
3,751,427; 10,124,090; 5,171,943; 2,601,098; 81,311,272; 5,596,626;
4,236,020; 2,357,483; and 4,357,719 ns. The maximum was 81,311,272 ns.
Independent PostgreSQL evidence showed exactly one succeeded feature job and
one succeeded waterfall job for each recording, succeeded feature and
waterfall projection receipts, 16 waterfall tiles of 262,144 cells, and a
complete dashboard row with eight capture segments, 48 features, available
object, and complete waterfall.

Post-run read-only safety again found both exact serials, TX2 at `-89.75 dB`,
all four DDS scales zero, no foreign owner/TCP session, and both shared and
campaign locks free. Full-drain and analysis-inactive were true and
911,537,942,528 bytes remained available. The qualification receipt was
created once, atomically and durably with mode 0600 at
`/home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r17_r18_20260816_v1/qualification.receipt.json`.
Its issued UTC ns is `1786901616817131121`; both its contract digest and exact
file SHA-256 are
`sha256:4ba33f46f28423d3d5d71ca75fb03e5f9259cd5989c5629de30d4e79f45c29d7`.
It binds the exact definition digest and counts `[1,1,1,1,1,1,1,1,1]`.

R17/R18 continuous-main preparation checkpoint (2026-08-16): after receipt
validation, a fresh 216-success/24-round main definition was written create-only
at
`/home/mouse9911/.local/state/leo-flow/campaigns/main_gauss_r17_r18_20260816_v1/main.definition.json`.
Its exact definition/file digest is
`sha256:54f81be4637b127a51f59e62de381a3e3cb49e2c532724898f3d21896f3dbf49`
and it binds qualification receipt
`sha256:4ba33f46f28423d3d5d71ca75fb03e5f9259cd5989c5629de30d4e79f45c29d7`.
Offline CLI validation, 32 focused continuous/campaign/runtime/deployment tests,
Ruff, strict mypy, and `systemd-analyze verify` passed. Exact rendered unit
copies were staged beside the definition; capture unit SHA-256 is
`f5a60a574e400a695d0135895a7e9e6fffba43d89dd7013ce8f42c6bd9e1806e`
and analysis unit SHA-256 is
`cfcc76234eb783378bb3063fcfd35a58f0405916c9edea8c0e64b6c9089cdd1d`.
They embed that exact definition path and digest.

This near-future main was deliberately not armed. Its preflight boundary was
`1786902083471736987` (`2026-08-16T17:41:23.471736987Z`), requested first
sample was `1786902098471736987`
(`2026-08-16T17:41:38.471736987Z`), and latest admissible first sample was
`1786902098571736987` (`2026-08-16T17:41:38.571736987Z`). Installation into
root-owned `/etc/systemd/system` required interactive sudo unavailable to the
automation account. No continuous journal, capture batch, radio context, or
service start was created. Preserve this definition and its exact rendered
units as missed/unstarted evidence; never start them after the timing boundary.
Install only the checked fail-closed unit templates with the literal
`REPLACE_WITH_EXACT_MAIN_DEFINITION_DIGEST` placeholder while disabled. After
installation authority exists, plan a wholly fresh main identity/state root
and render/install exact new units before repeating live admission.

R17/R18 continuous-main v2 staging checkpoint (2026-08-16, offline only): the
system unit destinations remained absent, with both service names inactive and
not found. A wholly fresh main `main_gauss_r17_r18_20260816_v2` was therefore
planned under
`/home/mouse9911/.local/state/leo-flow/campaigns/main_gauss_r17_r18_20260816_v2/`.
The create-only definition/file digest is
`sha256:4aa03245592b72b475f5559192368ac79a569f53b24641dfaee5794627b70571`
and it binds the exact successful qualification receipt
`sha256:4ba33f46f28423d3d5d71ca75fb03e5f9259cd5989c5629de30d4e79f45c29d7`.
Its preflight, requested first sample, and latest admissible first sample are
respectively `1786903677154100965`
(`2026-08-16T18:07:57.154100965Z`), `1786903692154100965`
(`2026-08-16T18:08:12.154100965Z`), and `1786903692254100965`
(`2026-08-16T18:08:12.254100965Z`).

Exact v2 capture and analysis units were rendered mode 0644 beside that
definition. They contain only the v2 state paths, exact definition digest,
qualification receipt, reviewed `.17`/`.18` station pair, candidate runtime,
100 ms admission, shared exclusive mode lock, and bounded two-phase commands.
Capture-unit SHA-256 is
`0e56f26c3c8aca35096a25d0720e18ea5c1c76a8d130ce8f7eabc0b5a7fab61a`;
analysis-unit SHA-256 is
`0c545a469ad5348e917ac54c8d88d259ca16a7776974f8d1d2af3949464b9765`.
Offline campaign validation and `systemd-analyze verify` passed. No unit was
installed, enabled, or started; no database, radio, CAS object, journal, or
capture state was opened during v2 staging.

Pre-launch continuous audit and v3 correction (2026-08-16, offline only): v2
was never installed or started and is permanently superseded. Audit proved
that its capture unit's 217-transition slice was insufficient: a service
started before preflight can spend one transition durably planning each batch
and returning `not_due`, then a second transition capturing it. A complete
216-batch capture phase therefore requires 433 transitions including the final
phase close. Restarting the 217-transition slice after 30 seconds could miss
the 100 ms bound. The checked R17/R18 capture template now uses 433; the
analysis template correctly remains at 217. A full durable 216-batch operator
regression reached `analyzing` in exactly 433 transitions, with 216 `not_due`,
216 captured, no analysis invocation, and elapsed simulated schedule below the
32,400-second service bound.

Two further admission gaps were closed before replanning. The continuous Gauss
composition now requires the exact ordered runtime IP pair to match the two
station endpoints before constructing a capture port; swapped and drifted
runtime tests fail closed. The private campaign definition now truthfully
serializes `analysis_after_each_capture=false` for a main planned with
`--deferred-analysis`. The deferred coordinator/operator reject legacy
per-capture definitions, while the one-shot coordinator/operator reject
deferred definitions before creating a journal. Existing one-shot and
qualification documents retain their original `true` bytes/digests. Finally,
both system units explicitly use `User=mouse9911` and `Group=mouse9911`, so
the 0700 user-owned state/CAS roots cannot acquire root-owned capture content.
They retain `NoNewPrivileges`, strict system protection, read-only home, and
write access only to the exact leo-flow state/share subtrees. Sixty focused
campaign/continuous/runtime/deployment tests, Ruff/format, strict mypy, and
`systemd-analyze verify` passed.

A wholly fresh deferred main `main_gauss_r17_r18_20260816_v3` is staged at
`/home/mouse9911/.local/state/leo-flow/campaigns/main_gauss_r17_r18_20260816_v3/`.
Its create-only definition/file digest is
`sha256:fea6cd328b65b2e08f19a0c0fc32fbddd2512d4a89c7f729446917406132a149`;
it binds the exact qualification receipt, 100 ms lateness/skew policy, and
`analysis_after_each_capture=false`. Preflight is
`1786904538415459090` (`2026-08-16T18:22:18.415459090Z`), requested first
sample is `1786904553415459090` (`2026-08-16T18:22:33.415459090Z`), and latest
admissible first sample is `1786904553515459090`
(`2026-08-16T18:22:33.515459090Z`). Exact mode-0644 units were rendered from
the corrected checked templates and verified byte-for-byte: capture unit
SHA-256 `7a5ead201710ce1d5f5962871299d05e7ee3b6e7fa48c43c79e664244b95fd2c`,
analysis unit SHA-256
`06557c4aaa973b0d8271c056fb7031680b04a353565cf683fb6ca2e02c85f778`.
Offline campaign validation and systemd verification passed. The `/etc`
destinations remained absent; v3 was not installed, enabled, started, or
allowed to contact a radio, database, credential, or CAS object.

Science-radio correction (2026-08-16): the user identified `.20`/`.21` as the
actual LNB-connected science radios and `.17`/`.18` as non-science hardware.
The `.17`/`.18` v3 definition and units above remain immutable offline evidence
only. They were never copied to `/etc`, enabled, started, or allowed to open a
radio. They are superseded and must never be used for the science campaign.

Fresh `.20`/`.21` post-hard-reboot qualification preparation (2026-08-16,
offline only): the audited checked source pair is the base V5 firmware plus
candidate host runtime used by the post-reboot diagnostics. R20 is
`ip:192.168.1.20`, serial `1040005e0b100007100010000bf33a5d4d`, radio
`radio_pluto_5d4d`, with LNB chains `rx_lnb_a`/`rx_lnb_b`; R21 is
`ip:192.168.1.21`, serial `10400056f695001322002d0010ad1719f2`, radio
`radio_pluto_19f2`, with LNB chains `rx_lnb_c`/`rx_lnb_d`. Both source specs
bind firmware `v0.38-plutoplus-spf-libiio-metadata-v5` at
`d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8`, host runtime
`gauss-pluto-v5-rx-integrity-close-barrier-1`, and runtime manifest
`sha256:0a9cf278bf836655afbf7a9a324a21c5dc41235d1b251386a0013eb0f299f123`.

The fresh qualification is under
`deploy/gauss-campaign-r20-r21-postreboot-v1/qualification-v1/`. Campaign
`qual_gauss_r20_r21_20260816_v1` has exact definition digest/file SHA-256
`sha256:043ea0ebe2dd40410b74141a57e8555df2a2b1031427a9a937d6d57c982aef44`;
its materialization manifest digest is
`sha256:6a4ea95c20abe4fc2fbbd04a232d4629eb5e769638469b6aa9c5a8e55659142b`.
It truthfully retains per-capture analysis for qualification, requires the
100,000,000 ns measured first-sample skew gate, metadata mask 15 with two
`I0,Q0,I1,Q1` channels, TX2 gain at or below -80 dB, and exact zero on all four
TX2 DDS scales. The 18 materialized stations have 18 unique plan, activity,
specification, state-root, spool, and radio-lock identities and zero plan-ID
overlap with failed qualification v8. Their mutable state roots, qualification
campaign state root
`/home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v1`,
and receipt output are all absent. The pair shares only the reviewed CAS root
and exclusive pipeline-mode lock.

The new `.20`/`.21` runtime digest is
`sha256:801614f2aae792ea2764d5e26c4676c956275dd4770c31fff7edea6e5785d506`.
Fail-closed future-main templates remain unarmed with a literal digest
placeholder. The capture template uses 433 transitions and SHA-256
`13746d21fcab1fbffb2ee6806c3c0d639cdd8342a037e27037312a10982cacc0`;
the deferred-analysis template uses 217 and SHA-256
`05c9e9fe999828d7baaddeeedad82b5582bb9f50cc2664b2d297c15d7bd351d9`.
Both run as `mouse9911:mouse9911`, use the exact ordered endpoint guard,
candidate runtime, bounded service policy, and restricted writable roots.
Offline campaign/materialization validation, focused qualification/runtime/
continuous tests, Ruff/format, strict mypy, and systemd verification passed.
No live preflight, radio, PostgreSQL, credential, CAS, qualification journal,
receipt, service installation, enablement, or start occurred.

## Rollback rule

SATPI01 `.20`/`.21` local recovery checkpoint (2026-08-16): the exact local
USB owner was `leo-sync-scan.service`, an enabled `Restart=always` unit whose
single `synccollect.py` process held both radios. With explicit user approval,
the unit was stopped and disabled; after a bounded observation it was
inactive/dead with `MainPID=0`, no USB owner, and no restart. No data was
deleted. Existing completed sweep files were then checked without opening the
radios: `.20` was constant on all sampled tuning/receiver blocks (RX0 exactly
`[51,-193]`, RX1 exactly `[0,0]`) while `.21` varied normally.

Fresh local receive-only USB diagnostics reproduced that split below Gauss and
Redux. Across four fresh libiio contexts and two 2.5 MS/s x 40 ms captures per
context, all eight `.20` matrices were byte-identical at digest
`sha256:eb5d585b6854fa6f7ed261038b696aa71b19ee2279b7c7fbe3b1bfe4c352a4fb`
and every I/Q component was constant. All eight `.21` matrices varied on both
receivers and had distinct hashes. Three-frame direct protocol-v2 USB capture
also failed `.20` (RX0 RMS `199.6246`, RX1 RMS `0.0` in every frame) while
`.21` passed with both receivers nonzero and valid gain/RSSI metadata. Both
gadgets reported clean protocol counters and the same build ID, but `.20`
alone had four radio-local `ad9361 ... Failed to read gain, state m/c at 0`
kernel messages. This places the `.20` fault inside the radio/FPGA receive
path, not IP, Redux, storage, analysis, or dashboard.

Both intended wired configurations are correct and carrier-up. Gauss reaches
`.20`/`.21` by ICMP and TCP/30431, and both radios reach the gateway. SATPI01
cannot ARP either radio and neither radio can ping SATPI01 `.186`, despite
accepting firewall/ARP settings, which isolates a separate intervening L2
port/VLAN/client-isolation issue. Direct USB from SATPI01 and IP from Gauss
remain the viable paths.

The verified rx-integrity candidate DFU
`sha256:4118a4f3a7130e407f4314e76415bbcf9183501e74faae1824ef2b52be616503`,
base V5 rollback DFU
`sha256:948b46506febacb087f3955be86015e074f8c0e3370a9dfc6a942e735d97f882`,
and receipt
`sha256:04127cf7571f74f3c1d9a992bbb6931ad0068b5d804c7bec9d75a1ddae8da57c`
were staged mode `0600` on SATPI01 and independently rehashed. Nothing was
loaded. The guarded Pluto+ Utils helper and site-specific exact-radio DFU
transition are not deployed there, so raw `dfu-util` is not an admitted
substitute. The full evidence, guarded volatile-only plan, rollback gate, and
shutdown caveats are in
`reports/satpi01-pluto-20-21-recovery-20260816/REPORT.md`.

Superseding hard-power-cycle checkpoint (2026-08-16T17:30:15Z onward): after
the user physically disconnected and cold-restarted both radios, Gauss
re-attested the exact `.20=...5d4d` and `.21=...19f2` serials, base V5 release,
and metadata capability. Cold boot reset both TX channels to `-10 dB`, so the
first read-only admission stopped before RX. With explicit authorization,
only TX0/TX1 gain was restored to `-89.75 dB`; all eight DDS scales were read
back at exact zero before and after testing.

The hard restart cleared `.20`'s frozen receive state without changing
firmware. At 2.5 MS/s x 100,000 samples, each radio passed four fresh ordinary
contexts with two buffer create/destroy cycles apiece: every I/Q component
varied, each receiver had roughly 16.4k-16.6k unique sampled pairs, and all
eight hashes per radio were distinct. Each then passed three fresh V3 metadata
contexts with three frames apiece: sequence `0,1,2` per context, exact 100,000
first-sample increments, scan mask `0x0f`, valid gain/RSSI/sample time, varying
dual-channel IQ, and nine distinct hashes per radio. A final simultaneous
two-process check passed three more metadata frames per radio; process starts
were 4.324679 ms apart. This is process-isolated concurrency evidence, not a
hardware-synchronization claim.

Final identity, firmware, TX gain, DDS, owner, and TCP readback passed with no
surviving owner. No capture was published and no database, CAS, reboot, or
firmware operation occurred. `.20` is healthy now, but the base V5 lifecycle
risk remains demonstrated: continuous qualification must retain the
capture-owned constant-component gate and should treat the staged candidate as
a fallback rather than install it preemptively.

Stop admission of new plans, let or bound the active operation according to its
documented shutdown policy, and preserve all spool/CAS/catalog/receipt state.
Rollback may select the last qualified one-radio deployment only after both
dual-radio capture instances are stopped and their terminal state is recorded.
Never recover by deleting spool databases, partial recordings, CAS objects, job
leases, projection rows, or golden fixtures.

## R20/R21 both-TX passive qualification-v3 checkpoint

Offline hardening checkpoint (2026-08-16T18:29Z): `.20` and `.21` are now the
sole active science endpoints in deployment composition. The active runtime is
exactly `ip:192.168.1.20` / serial suffix `5d4d` / receivers `rx_lnb_a,b` and
`ip:192.168.1.21` / serial suffix `19f2` / receivers `rx_lnb_c,d`. No `.17` or
`.18` service was installed or active, no matching process/TCP owner existed,
and the historical R17/R18 qualification remains terminal evidence only. Its
continuous runtime and service templates, the unstarted external R17/R18 main
staging, and the obsolete R17/R18 transport-comparison test were removed from
the active path. Historical qualification, database, CAS, and log evidence was
not deleted.

The capture-owned station/preflight boundary now requires both Pluto transmit
paths to be passive for science capture: TX1 and TX2 hardware gain must each be
at or below `-80 dB`, and all eight DDS scales `altvoltage0` through
`altvoltage7` must equal zero. The selected radio is destroyed/closed and no
metadata or capture read is released when either TX1 gain or any TX1 DDS scale
differs. Existing constant-I/Q, exact serial/firmware/runtime, four-component
scan layout, and 100 ms measured first-sample skew gates remain unchanged.

The earlier post-reboot qualification drafts v1 and v2 were never armed and
were moved recoverably to trash after their windows expired or their passive
policy provenance was superseded. The sole active immutable artifact is
`deploy/gauss-campaign-r20-r21-postreboot-v1/qualification-v3/`, campaign
`qual_gauss_r20_r21_20260816_v3`, with:

- requested start `2026-08-16T19:00:00.000000000Z`;
- preflight boundary `2026-08-16T18:59:45.000000000Z`;
- terminal start-lateness boundary `2026-08-16T19:00:05.000000000Z`;
- definition digest/file SHA-256
  `sha256:28c031da559146a508e28be5c74c2dde2a5e48d127440f4964d98426788fd1a6`;
- materialization digest/file SHA-256
  `sha256:6f01dcb0e2de781f0ebd1f1ea143b5ce84f966fda4febda72f2df76193d242fe`;
- source station specification digests
  `.20=sha256:9a7ff11e8010a1881a356142a3016472494042fcf3174b94a1064a1ec3781f77`
  and
  `.21=sha256:23a46c59f6ab91282261eea61de418416e71207310c1dddf9d7302a02985f71b`;
- 18 unique per-unit station specifications and a still-absent state root
  `/home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v3`.

Both offline station validations, campaign validation, exact materialization
validation, 128 focused capture/campaign/continuous/deployment tests, including
rejection of superseded single-TX planning, plus Ruff,
format, strict mypy, and systemd verification passed. The active service
templates remain unarmed and bind qualification-v3; capture is bounded to 433
transitions, analysis to 217, both run as `mouse9911:mouse9911`, and both still
contain the literal main-definition digest placeholder. Runtime, capture, and
analysis service file SHA-256 values are respectively
`801614f2aae792ea2764d5e26c4676c956275dd4770c31fff7edea6e5785d506`,
`466717f2ef001f8bc9993d6bc19f3c0b70dc33ab7f735a8e8d5574f9d3c4eb00`,
and `e4bac92824ae44047b1a4560006a7cad8ecead95c2bf1733645f2130ff00e982`.
No live radio, PostgreSQL, credential, CAS, journal, service installation, or
arming action occurred in this checkpoint. Independent artifact review plus
fresh live owner, firmware, TX, drain/inactive, capacity, lock, and state-root
gates remain mandatory before any arm.

Qualification scheduling correction (2026-08-16T18:45Z, offline only): an
independent audit proved the retired qualification coordinator used invocation
time as the first unit's requested UTC, so qualification-v3's advertised start
was not authoritative. It was never armed. A first replacement draft also
encoded a dynamic subsequent-unit policy that could not truthfully bind the 18
precomputed station artifacts; both rejected drafts were moved recoverably to
trash with no state root, journal, receipt, radio, database, or CAS contact.

The private campaign definition is now versioned V2 and has no legacy V1
decoder fallback. Qualification explicitly encodes
`unit_schedule=fixed_nine_cell_no_catch_up_grid`, the rational period
`400000000000/3 ns`, and `no_catch_up=true`. The coordinator derives every
requested UTC only from immutable definition start plus slot index. It creates
the first durable plan before its preflight boundary, never calls capture
before that boundary, passes the exact future requested UTC to the process-
isolated capture port, terminally misses an invocation later than its stored
requested UTC plus 5 seconds, and preserves that same schedule across restart.
Subsequent units are likewise explicit rather than invocation-relative.

The sole active artifact is now qualification-v5, campaign
`qual_gauss_r20_r21_20260816_v5`, under
`deploy/gauss-campaign-r20-r21-postreboot-v1/qualification-v5/`. Its first
requested UTC is `2026-08-16T20:30:00Z` (preflight `20:29:45Z`, late-stop
`20:30:05Z`); its ninth requested UTC is
`2026-08-16T20:47:46.666666666Z` (preflight
`20:47:31.666666666Z`, late-stop `20:47:51.666666666Z`). Exact digests are:

- definition `sha256:9a1091b07917a74e815cbba3a64283450d63f06a89513afeb135e7c9ffeb72fc`;
- V2 materialization `sha256:56292d23475d05399ff5026bc1b27f1b2b8ec2b4c07183d74d6b2f9e143ba164`;
- runtime `sha256:801614f2aae792ea2764d5e26c4676c956275dd4770c31fff7edea6e5785d506`;
- capture service `sha256:6fae1d36d650b384cade32e31ac31f6255e607a0265c62ba3d89deff7a6e40bd`;
- analysis service `sha256:09c421f0e8b3ba99184560040c7d942dd1c530681d184b8bfd948ee6501bb066`.

All 18 materialized stations bind their exact per-unit requested UTC, unique
plan/activity/state/spool/lock identities, both-TX passive policy, exact
`.20`/`.21` serial/LNB mapping, base V5 firmware, candidate host runtime, shared
CAS/mode lock, and 100 ms measured-skew gate. The v5 state root and receipt are
absent. Offline campaign and materialization validation passed. The focused
134-test set passed, including component and operator proofs for early wait,
exact preflight release, late terminalization, immutable resume, explicit
subsequent scheduling, and rejection of the legacy schema/policy; Ruff, format,
strict mypy, and systemd checks passed after the final formatting pass. Live
qualification remains held for independent re-audit and fresh live gates.

Qualification-v5 planned-only cancellation (2026-08-16T19:17:22Z): after
independent ARM review, the exact v5 process was started and durably wrote only
revision 1 / unit 0 `planned` / `not_due`, with zero successful counts and unit
digest
`sha256:ed6e2bbfebadb874cae6470966067058ec4afde83031d90ea4273862879bbee7`.
At the user's request to use an earlier fresh start, PID `3511381` received a
terminal interrupt while sleeping before the first `20:29:45Z` preflight. It
exited and was reaped with no child, mode-lock owner, `.20`/`.21` socket, RF
context, recording, analysis, catalog, or CAS work. The campaign lock was
released. The complete planned-only state was preserved, not deleted, at
`/home/mouse9911/.local/state/leo-flow/campaigns/canceled/qual_gauss_r20_r21_20260816_v5-planned-only/`
and the active v5 state path is absent. Fresh v6 generation and arming are held
until migration 0028 is applied and verified on the live qualification database,
because current analysis/dashboard source expects that migration head.

Live migration 0028 and dashboard rollout (2026-08-16T19:33Z–19:37Z):
before mutation, the isolated PostgreSQL 16.10 qualification cluster was
confirmed as system identifier `7674352851925897955`, database
`leo_gauss_qualification`, with exactly 27 matching receipts through 0027,
both capture gates true, 79 terminal jobs, 55 terminal feature-projection
items, 24 terminal waterfall-projection items, and no Starlink relation or
routine. No Redux capture/campaign process or pipeline-mode-lock owner was
present. Migration `0028_recording_starlink_candidate_pipeline.sql`, exact
SHA-256 `253b78a510595f176b9687c4edd52d2b9fef4fcb6733d3b1d3d0291d208e06ee`,
was applied alone through `apply_migrations`; the receipt and all objects
committed atomically. A second application was an exact no-op.

Post-migration evidence has exactly 28 receipts ending at 0028. Every existing
table count, job-state count, feature/waterfall work-state count, role
membership, and cluster identity was preserved; the three new Starlink tables
are empty. All ten new/replaced SECURITY DEFINER routines are owned by
`leo_routine_owner` with fixed `search_path=pg_catalog, pg_temp`. The eight
Starlink publication/work/receipt routines are executable only by
`leo_analysis`; the two capture admission routines are executable only by
`leo_capture`. Direct table privileges remain least-privilege, all four
runtime roles lack schema CREATE, and both
`capture_analysis_drain_ready()` and `capture_analysis_inactive()` remain true.

The sole transient user dashboard was then restarted in place: old PID
`3529282` drained and stopped normally; PID `3538760` resumed with the exact
same executable, checked config, working directory, dashboard-only credential
directory, and `0.0.0.0:8090` bind. Direct HTTP proof returned 200 for root,
inventory, capture batches, recording detail, 48 diagnostics, and a complete
16-tile waterfall. The old recording's Starlink route returns truthful 404
`not_found`, never 500. Real Chromium, with no request interception, rendered
22 `.20` rows by `.20`/`192.168.1.20` alias filtering and 22 `.21` rows by
`.21`/`192.168.1.21`, rendered a nonempty real waterfall canvas for
`rec_01M051WGPB0TPTS4FP20C1D9S4`, showed `Not evaluated` for Starlink, and
kept diagnostic features collapsed. It observed no failed request and no 5xx
response. No radio endpoint was opened during this rollout.

Immutable qualification Release A and live v6 outcome
(2026-08-16T19:43Z–20:02Z): the exact 809-file dirty working-tree byte
snapshot was stable across before, after, and final manifests and was packaged
as a wheel without claiming Git cleanliness. The read-only release is
`/home/mouse9911/.local/share/leo-flow/releases/qualification-release-a-61f5fbbb973e5b9e`.
Its source-snapshot digest is
`sha256:61f5fbbb973e5b9e1af181dc9f6080ebc81ad44b4e6ed2d1d2126707e6c1c24d`,
wheel digest is
`sha256:d5be102e41642d9e06613ff65d41183b8d3a4a9fe4e94fef6e3361a4a77f2cb7`,
release-manifest digest is
`sha256:793c87e7f8396de1ae5bbcbb1f7f2d57d7fc2dc659411a42747198a46fd6ce19`,
and validation-receipt digest is
`sha256:d898a5ad31c4db6238c80b8c6ddccd017272c53145ffcbd54e6d082f52c90fb3`.
It runs exact Python 3.11.16, imports `leo_flow`, `leo_station`, and `adi` from
the release rather than this checkout, imports libiio/SPF only from the pinned
candidate native paths, pins the exact `uv.lock`, migration 0028, science
approval, runtime, and `.20`/`.21` station bytes, and passed 262 relevant
offline tests plus runtime/science/station/campaign validation.

Fresh campaign `qual_gauss_r20_r21_20260816_v6` was generated only after the
live PostgreSQL 16 database proved 28/28 exact receipts through migration 0028,
both capture gates true, credentials mode 0600, about 911.5 GB free, and no
pipeline, station, process, or TCP owner. Its first requested RF release was
only 35 seconds after definition creation, with the operator started before
the 15-second preflight boundary. Definition digest is
`sha256:e2d0bd04a569d0d0aa47dbd4da1e6a3bbefe29e3d15507c415ddd204e7144b37`;
independently regenerated 18-station materialization digest is
`sha256:24a3bcaf456511c82304ffcbab450431e56dcdce544aa91fd9012376edf04030`.
The operator ran from Release A with `/tmp` as its working directory and no
checkout path in `PYTHONPATH`.

Eight cells completed capture, exact FeatureSet projection, two complete
16-tile waterfalls, and dashboard detail closure. Their measured first-sample
skews were 2,366,603; 2,595,063; 7,920,076; 5,644,025; 81,720,139; 9,880,314;
21,990,377; and 17,652,960 ns. The ninth 5 MS/s by 160 ms cell published both
valid eight-segment recordings—`.20`
`rec_01M062NW2Z8NN7H24P1AMT9CBP` and `.21`
`rec_01M062NW364BHV3WXKMFE54GS3`—but their metadata-derived first-sample skew
was 150,686,531 ns, above the immutable 100,000,000 ns limit. The campaign
therefore exited fail-closed with code 4 and journal revision 43 at 8/9. The
final host-side capture-start skew was only about 5.65 ms, but that does not
override the authoritative metadata timing gate. The failed unit was not
replayed, its two recordings remain preserved with analysis pending, and no
qualification receipt was issued. The process exited, locks were released,
radio sockets closed, and both capture-analysis gates remained true. Canonical
terminal evidence is
`/home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v6/evidence/live-qualification-terminal-audit.json`,
digest
`sha256:465a1a28ac221e8c802cca9eafb17f738df4ac65236078c2148b588b1d3b1ff7`.
Starlink analysis was not wired into Release A's qualification loop and is not
claimed for these recordings.

V6 synchronized-start diagnosis and offline remediation
(2026-08-16T20:02Z–20:12Z): preserved metadata proves the final-cell failure
occurred after the common release gate. Host capture starts differed by only
5,648,114 ns, while the first retained samples differed by 150,686,531 ns.
The `.20` first retained sample arrived 709.478 ms after its segment start and
the `.21` sample arrived 553.142 ms after its start. Later 800,000-sample
segments repeatedly show `.20` carrying roughly 130–170 ms more startup delay,
with `transport_attempts=1` throughout. The pinned SPF metadata adapter opens
after release and first primes one full ordinary dual-channel IIO block; at
5 MS/s the former 800,000-sample block is itself 160 ms. This establishes
large post-release buffer/transport priming—not the shared software gate—as
the failure mechanism. Per-radio delay compensation was rejected because the
signed v6 skew varied across cells and changed direction.

The private campaign materialization policy now caps every hardware refill at
40 ms of samples while preserving each cell's exact total sample count and
dwell. Thus 40/80/160 ms cells use 1/2/4 metadata refills respectively, with
the existing exact continuity verification on every refill. MetadataBuffer
open and retained reads remain behind the release gate, the 100 ms acceptance
limit is unchanged, and no radio, database, service, old recording, or v6
identity was opened or mutated. Historical v5 qualification artifacts remain
byte-for-byte pinned at definition SHA-256
`9a1091b07917a74e815cbba3a64283450d63f06a89513afeb135e7c9ffeb72fc`
and materialization SHA-256
`56292d23475d05399ff5026bc1b27f1b2b8ec2b4c07183d74d6b2f9e143ba164`;
current policy rejects their obsolete one-refill station materialization.
Offline component, driver, process-isolation, operator, materialization,
station, integration-candidate, package, lint, and format checks passed: 178
pytest cases total plus clean Ruff checks. Release B and any fresh live v7 run
remain intentionally deferred until the isolated Starlink integration is
coordinated into the main source snapshot.

Continuous-main schedule redesign and documentation (2026-08-16T20:12Z): the
real v6 timing envelope was reduced into `scan_capture.md`, reproducible public
plots/evidence under `reports/scan-capture-v6/`, and the quantified
`continuous_scan_schedule.md`. The frozen qualification v2 definition remains
byte-exact at SHA-256
`e2d0bd04a569d0d0aa47dbd4da1e6a3bbefe29e3d15507c415ddd204e7144b37`.
The new private main v3 policy schedules 936 pairs over eight hours on the
exact 400,000,000,000/13 ns grid, repeats every rate/duration cell 104 times,
and crosses it with the four-phase `L/L`, `L/U`, `U/U`, `U/L` geometry so all
36 combinations occur exactly 26 times. Exact raw payload is 32,614,400,000
bytes; admission with the checked 10 GiB margin requires 75,966,218,240 bytes.

The capture and deferred-analysis loop bounds are now definition-derived and
encoded into the definition: 1,873 calls for `capture-run` and 937 calls for
`drain-analysis`. The active checked two-phase units carry those exact values.
The inactive, uninstalled combined-run unit under `deploy/gauss-continuous-v1`
was removed, and the obsolete continuous `run` command was removed from both
the component parser and Gauss composition. Qualification still uses its
separate per-capture operator. No service, database, CAS object, radio context,
or live campaign state was changed. The schedule-related source passed Ruff,
mypy, frozen-v6 codec/digest verification, and 74 focused component/service/
deployment/package tests; the core focused subset was 58 tests after adding
exact geometry, capacity, transition, and materialized-station assertions.
Starlink was not wired in v6, so the deployment docs make its 36-slot canary
benchmark a mandatory input to the deferred-drain deadline rather than
claiming capacity from ordinary feature/waterfall timing.

Immutable qualification Release B freeze (post-freeze ledger entry,
2026-08-16): the coordinated Starlink detector-suite v0.2 integration,
migration 0029, denser main-campaign schedule, and 40 ms qualification refill
policy were frozen from 829 exact dirty-working-tree files without claiming Git
cleanliness. The before, after, and final source manifests were byte-identical
at SHA-256
`8f1374f55d35bb7c73cb26ac0ee13700df146b7a49fca8a0ccf859acd8d49b31`.
This ledger paragraph was intentionally written after that freeze and is not
part of its source snapshot.

The read-only release is
`/home/mouse9911/.local/share/leo-flow/releases/qualification-release-b-8f1374f55d35bb7c`.
Its independently rebuilt wheel exactly matched the integration-audit wheel at
SHA-256
`a8c559ff7cda2fad56e35ddc2d59de31c1cd3b37702dbaa48f46745753b7a786`;
the release-manifest digest is
`8d8e84ba0d2763345a6228ed69f38c069756d25a704b233060a464cafc97c864`,
and validation-receipt digest is
`6f921ec70c42413f0ca73a61dbb4a95010c7f0ab6eecb180a92d875de3eda1f0`.
It pins Python 3.11.16, exact `uv.lock`, migrations 0028 and 0029, final science
approval, reviewed runtime specification, candidate native runtime, and exact
`.20`/`.21` source stations. From `/tmp` with no checkout path in
`PYTHONPATH`, `leo_flow`, `leo_station`, and `adi` resolve inside the release;
`iio` and SPF resolve only from the pinned native paths. All six console
scripts load from the installed wheel.

Offline validation proved all 18 fresh qualification station plans use exact
total sample counts with the 40 ms block cap: 40/80/160 ms dwells have 1/2/4
verified refills, while the 100 ms first-sample skew gate remains unchanged.
Science validation pins detector-suite v0.2: 1.25 MS/s is terminal
`not_evaluated`; 2.5 and 5 MS/s are eligible; each analyzed stream has eight
methods; all results remain candidate-only and require calibration before a
detection claim. Source-wide evidence is 818 passed / 906 Docker-dependent
skipped, plus focused semantic/UI, schedule, packaging, strict mypy, Ruff, and
diff checks. Independent disposable PostgreSQL 16 evidence passed 202 database
tests and 14 production-path tests with exactly 29 receipts, replay, and ACLs;
the audit database was removed. Release construction contacted no radio, live
database, or service. Live application of exact migration 0029 and a wholly
fresh v7 definition/materialization/state namespace remain mandatory pre-arm
gates; no v6 identity may be replayed.

V7 qualification and preserved failure (2026-08-16T20:59Z–21:06Z): immutable
Release B captured five complete cells and then stopped fail-closed at cell 5.
Both cell-5 recordings were valid and synchronized within 7,146,859 ns, and
both FeatureSet and 16-tile waterfall products completed. The detector-suite
runtime processed the two eligible radio jobs serially: the first completed,
but the second was claimed with only 50.902881 seconds left before the fixed
slot deadline and was killed at that deadline. The campaign exited 4 at 5/9,
did not replay any capture identity, and issued no qualification receipt. This
isolated analysis scheduling, rather than RF capture or the 100 ms skew gate,
as the failure mechanism.

Immutable qualification Release C freeze (post-freeze ledger entry,
2026-08-16): the bounded concurrent detector-suite worker path was frozen from
830 exact dirty-working-tree files, again without claiming Git cleanliness.
Before, captured, after, and final source manifests are byte-identical at
SHA-256
`50ea558d74f174e53d8b8b570f97f7ebbbc98737c466fe7cdce88359fe8ad71b`.
This ledger entry was intentionally added after the freeze. The sealed release
is
`/home/mouse9911/.local/share/leo-flow/releases/qualification-release-c-50ea558d74f174e5`.
Its independently rebuilt wheel is
`47f2afe0f984d99bb3ef9d965dce6bc9e09940109d78eb025c53dd62be2f73fc`,
release manifest is
`0ff024e40d44b68996819af5bee97643f553797d09237e71d67b540d136b7c12`,
and offline validation receipt is
`2f5760693db8e8c517a384d6cd1c28514c78dda17be6c45d6557da7f1bb9866a`.
Release C preserves migration 0029, science, runtime specification, `uv.lock`,
the 40 ms 1/2/4-refill policy, and candidate-only Starlink v0.2 semantics.
The exact unfinished job count now starts one or two isolated, independently
fenced workers; peer failure remains fail-closed and all children are reaped.

Before v8, the normal fenced Release C worker reclaimed the expired v7
detector-suite lease and projected both preserved results. This completed
deferred analysis only: no v7 campaign journal, recording, or radio capture was
replayed. Live admission then reported both inactive and drain-ready. Exact 29
migration receipts through 0029, credentials, capacity, process/TCP ownership,
file locks, and direct `.20`/`.21` V5 serial/firmware/TX/DDS attestation all
passed.

V8 qualification completed 9/9 from the sealed release under fresh campaign
`qual_gauss_r20_r21_20260816_v8`, definition SHA-256
`8ef0c62df40da111b944552e761ea9c9443596dbb252c334e8eea0923a8a15dd`.
All 18 recordings have complete FeatureSets, complete 16-tile waterfalls, and
dashboard details. The six 1.25 MS/s recordings are exact terminal
`not_evaluated` results with zero streams/methods; the twelve eligible 2.5/5
MS/s recordings each have candidate state, 16 streams, and 128 method results.
Maximum observed first-sample skew was 41,125,184 ns, below the unchanged
100,000,000 ns limit. The former v7 failure cell completed fully with both
concurrent suites. Only after all closures were independently re-read was the
canonical qualification receipt atomically installed at SHA-256
`6a816b5da9be8cb86361610fe42004074512e64b0748de2be2b62e832cdbcb8d`.
Terminal audit evidence is
`/home/mouse9911/.local/state/leo-flow/campaigns/qual_gauss_r20_r21_20260816_v8/evidence/terminal-audit.json`
at SHA-256
`53aff643eb3aa4f008f1b227674540016fc91678cf3b7c563ef98a6fdbff6dfd`.
No calibrated Starlink detection count is claimed.

Release D staged-analysis and canary integration audit (2026-08-16, offline
only): migration 0030 adds finite 1..72-identity PostgreSQL claims and status
reads for the six deferred-analysis barriers. The 936-slot main drain is now
definition-derived as 26 exact 36-batch windows plus phase close (27
transitions), with at most eight spawn-isolated compute workers and four
projection workers. Every barrier requires an exact terminal count of 72 before
the serial campaign receipt journal may advance; zero, partial, oversized,
pending, parked, stale-generation, or escaped identities fail closed.

The separate `org.leo-flow.gauss-v5-supercycle-canary/v1` surface is a finite
36-slot/no-replay promotion experiment, not qualification and not a small main
campaign. It binds the exact v8 receipt, all nine rate/dwell cells under each of
the four edge geometries, 40 ms hardware blocks, 72 distinct recordings, the
same six migration-0030 barriers, and exact V4 dashboard closure. Its receipt
always carries `main_campaign_authorized=false`; candidate and
`not_evaluated` Starlink products remain non-detections. The Gauss composition
uses canary-only state paths, process-isolated dual capture, the host-wide
analysis mode lock, exact scoped job IDs, and PostgreSQL receipt/V4 pairs. No
canary definition was materialized and no radio, live database, service, CAS
object, campaign journal, or v8 evidence was mutated during this audit.

Release D also removes checkout/cache-bound service templates. Its inactive
user-service templates execute only a sealed release, vendor the reviewed
libiio Python/native bytes and five SPF modules, and run the offline release
verifier before every capture or analysis start. The verifier binds externally
armed manifest and validation-receipt SHA-256 values, a closed file/symlink
inventory, passing offline-only checks, and rejects missing, extra, changed, or
escaping entries. Live promotion still requires a frozen Release D artifact,
exact live migration head 0030, a fresh canary definition/state namespace, the
36-slot benchmark and receipt review, and only then a fresh 936-slot main
definition. This entry records implementation readiness; it does not authorize
either capture.

Immutable qualification Release D freeze (post-freeze ledger entry,
2026-08-16): the final migration-0030, 36-slot canary, release verifier, sealed
native runtime, and user-service-template source was frozen from 862 exact
dirty-working-tree files without asserting Git cleanliness. Source before,
snapshot, after, and final manifests are byte-identical at SHA-256
`4aa24176743c623cb05eb2027086233abd826eee04249dc75caf02574c69c132`;
ignored source `__pycache__` and bytecode were excluded. The read-only release
is
`/home/mouse9911/.local/share/leo-flow/releases/qualification-release-d-4aa24176743c623c`.

Two independent wheel builds match the audited SHA-256
`b118d4aa27a3bf25470aa21e70afea736a0ad5e150d1ae87a47ed2c80c7410fd`.
Release D vendors the complete CPython 3.11.16 base, isolated wheel runtime,
exact `libiio.so.0.25` with its internal symlink chain, patched `iio.py`, and
five reviewed SPF files. Its operative native manifest is
`ff83afdf06493d4ca9251ab95aff9a90a9b95fc9539d6adeb7f733c81374208a`;
the reviewed native source/host-ABI inventory remains
`f187d01f7473cfb5d5f5bf971d445f123f52536f9737ef778aab7bd14be32178`.
All operative station/runtime paths resolve inside the release rather than a
writable cache or checkout, and every sealed symlink resolves within the
release root.

The closed 7,112-entry inventory manifest is SHA-256
`99aba41c0e9780b4d76302acbda9b55c780d7f648ba5833e4fa27bbe68c5e755`;
the externally hashable offline validation receipt is
`0eae5444bba3fb5b928d23256179239fc23539ee36f24a33b1b682f765941f45`.
The verifier passed from `/tmp` with both hashes explicitly supplied. Eight
release entrypoints, exact v8 receipt binding, 72 offline canary station plans
with 40 ms 1/2/4 refills, migration 0030, science approval, relocated `.20` and
`.21` stations, and inactive main/canary unit templates all passed. No live
database, radio, service, campaign state, or object storage was contacted or
mutated. This freeze does not apply migration 0030 or authorize a canary/main
run.

Release D live canary pre-arm gate (2026-08-16): **stopped before arm**. The
closed-tree verifier passes when supplied the exact external digests
`sha256:99aba41c0e9780b4d76302acbda9b55c780d7f648ba5833e4fa27bbe68c5e755`
and
`sha256:0eae5444bba3fb5b928d23256179239fc23539ee36f24a33b1b682f765941f45`.
PostgreSQL has all 30 source-exact migration receipts at head 0030, with both
capture-analysis drain and inactive gates true. The proposed fresh namespace
`canary_gauss_20260816_c01` is absent, usable CAS capacity is 910,139,658,240
bytes versus 13,246,218,240 required, and no process or TCP connection owns
`.20` or `.21`.

The first Release-D-only live radio attestation then failed before radio
contact: sealed `config/r20.station.json` and `r21.station.json` name
`/tmp/leo-release-d-build.dhyjefyn/release/config/native-runtime.json`, their
expected Python/native/SPF paths use that deleted build directory, and sealed
`config/runtime.json.analysis_config` also uses it. This disproves the earlier
operative-path claim above even though the closed byte inventory is intact.
The immutable release was not modified. No canary definition, journal, radio
context, database row, CAS object, or service was created. A replacement
release must render its final installed path before hashing/sealing and must
verify that every operative absolute path exists under that final release and
contains no temporary build root.

Immutable Release E and capture-only canary (2026-08-16): corrected Release E
was built directly at its chosen final path
`/home/mouse9911/.local/share/leo-flow/releases/qualification-release-e-b74390de80c5`
before any inventory hashes were made. Its exact source manifest is
`b74390de80c584761a24bcdddc8982aa902a5431db78a1b76ef11e7c052fa254`
(862 files), wheel is
`9570646b280bf3d615d31ec8922432b29c714c1635890c502341f6c11ffdd90d`,
operative native manifest is
`050d7c1e14498151de3a43d253a20af9fe47a44e380d0cb037062ad125ccf3d6`,
closed inventory manifest is
`1ef81db183c4018f4ce08a3218195811d46fe3d465d4cbc428894bd195fc38b6`,
and offline validation receipt is
`bd871e63aee03a58cc49aea4e0494bd86a76192fdc9c67c5f9b98100ba15564d`.
The hardened installed verifier passed after the 7,112-entry tree was made
read-only. Independent scans found no temporary, cache, checkout, or
outside-release path in operative configs or `pyvenv.cfg`; imports resolved
inside E. All 72 offline canary plans, four geometries, 1/2/4 refill counts,
exact v8 receipt binding, and eight CLI smokes passed.

Fresh canary `canary_gauss_20260816_c01` (definition
`sha256:5f052eaf0839d980dd7e2c531165ef5029924f854055e382016656bf1c2bf394`)
ran as a supervised **capture-only** transient service with no analysis
successor. Release verification, exact 30 migration receipts/head 0030,
drain/inactive, capacity, namespace, ownership, and fresh `.20`/`.21` V5
serial/firmware/metadata/both-TX/all-DDS gates all passed. Slots 0--31 are
durable: 32 batches and 64 successful recordings, with worst paired
first-sample skew 49,751,612 ns (below 100 ms) and worst start lateness
289,494,621 ns (below 5 s), with no catch-up or replay.

The run stopped fail-closed at slot 32 and remains preserved. Radio A completed
and published `rec_01M06E2M4W0WEF74Y5FX2K6Y4Z`; all eight segments report
four contiguous refills and `all_ci16_components_vary`. Radio B allocated
`rec_01M06E2M55Q2Y91J7MH5V1J62D` but its local spool terminally records
`RadioDisconnectedError: Pluto receive disconnected: [Errno 32] Broken pipe`;
it has no observed start or published recording. The paired batch therefore
contains A `succeeded` and B `capture_engine_failed`, and the canary journal is
terminal `halted/capture_uncertain` at revision 99. No retry, remaining four
slots, analysis, canary receipt, main authorization, or detection count was
issued.

Post-failure `.21` passive restoration (2026-08-16T23:25Z): read-only
reachability showed both radios answering 3/3 ICMP probes and TCP/30431, with
no host kernel event. A fresh Release-E `.21` context retained its exact
serial, base V5 firmware, and metadata capability but failed closed because
both TX hardware gains had reset to `-10.0 dB`; all eight DDS scales remained
zero. `.20` still passed exact passive attestation. This makes the slot-32
broken pipe a persistent `.21` radio/context state reset, not merely client
transport teardown.

Under exclusive pipeline ownership, the exact `.21` identity and firmware were
rechecked, both TX gains were set to the AD9361 floor `-89.75 dB`, and every
`altvoltage0..7` DDS scale was explicitly set to zero and read back. No RX
capture, reboot, firmware operation, or `.20` write occurred. The context was
closed, then fresh independent Release-E attestations passed for `.21` and
`.20`: exact serial/V5/metadata, both TX gains below the passive ceiling, and
all eight DDS scales zero. The final external-owner check and pipeline lock
check were clear. The terminal canary remains halted and was not retried.

Post-power capture and analysis promotion (2026-08-17): after both Plutos were
power-cycled onto the replacement supplies, capture-only canary `c03` completed
36/36 synchronized batches and 72/72 unique recordings. Its worst paired
first-sample skew was 47,110,804 ns, and radio-local UDP sentinels preserved one
unchanged boot identity and iiOD process identity per radio with both TX gains
at `-89.75 dB` and all DDS scales zero. This passed the exact prior slot-32
failure point without a reset.

Main campaign `main_gauss_r20_r21_20260817_v3`, definition
`sha256:70c5406edc828150195a417e95bc26877163fbd851fddb7db0e77a25c126cd92`,
was then armed capture-only from sealed Release C. It carries 936 synchronized
slots on the exact `400/13` second cadence, all nine rate/dwell cells and four
same/opposite-edge geometries, with 40 ms hardware blocks and 1/2/4 verified
refills. At the 2026-08-17T03:56:36Z checkpoint it had 309/936 durable batches
and 618/618 unique successful recordings, no halt/replay/missed slot, a worst
skew of 79,516,148 ns below the 100 ms gate, and continuous unchanged
boot/iiOD/passive-state sentinels. Its final requested slot is
2026-08-17T09:17:40.398Z; this entry is an in-progress checkpoint, not terminal
evidence.

The first 36 main-v3 batches were independently analyzed in an isolated
PostgreSQL/CAS shadow. FeatureSet and waterfall compute/projection closed 72/72.
Starlink detector-suite v0.2 closed 72/72: 24 clipped 1.25 MS/s recordings were
terminal `not_evaluated`; 48 eligible 2.5/5 MS/s recordings produced 768 stream
suites and 6,144 method rows, exactly 768 for each of the eight report methods.
Pinned leo-tracker numerical-oracle comparisons agreed at floating precision
(2.5 MS/s maximum delta `2.37e-14`; 5 MS/s score/control/margin delta
`3.81e-14`, residual-CFO delta `4.15e-10 Hz`). These remain uncalibrated
candidates, not beacon detections. Durable evidence is under
`/home/mouse9911/.local/state/leo-flow/evidence/shadow-analysis-v3-20260817/`.

Release F was sealed from pushed source commit
`f34684c8427c7527fdbad9d61b6943a60380cd75` at
`/home/mouse9911/.local/share/leo-flow/releases/qualification-release-f-6f6b7d8e7c6d`.
Its closed manifest is
`9b58f50dd62c8b579bc4535bc9596476546df386b68e0a1c3920d6b171ea49db`,
validation receipt is
`bd6da13f2c1991c68638623d70de509bc1c6dbf1e4e41234631a0129f139cccd`,
and wheel is
`d0ab4b65078dff84c662e460be38eeeab91254042e5f6cd43b3fef99edb4c912`.
The 6,875-entry tree is read-only; two wheel builds matched; installed tests
passed 96/96; disposable PostgreSQL-16 lifecycle/online-analysis tests passed
22/22; both release-local station runtime validations and the exact 36-batch
online-window isolation proof passed. No live service used F during its build.

Live PostgreSQL was atomically promoted from exact head 0030 through
`0031_radio_lifecycle_detection.sql` SHA-256
`1716d173ef85e1e3ceaadd8dce5c0fbe4a018f4f9506a5bce081bbf05e8d7865`
and `0032_campaign_online_analysis.sql` SHA-256
`478dd1fb6e66745a94ca6208b02d6b4d6af23e2c7e0d11a4b6ccf8efab8f984a`.
Replay was an exact no-op; 32 receipts are present; all four new tables were
empty; routine ownership/search paths and capture/analysis gates passed. The
Release-C capture advanced cleanly afterward. Only the dashboard was restarted
onto committed main; inventory/detail/lifecycle routes were healthy. Current
main recordings remain truthfully `pending`, and absent Starlink-suite products
return `not_found`, never a false zero.

Because Release F has new exact station/runtime digests, v8 cannot authorize an
F capture. Fresh qualification `qual_gauss_r20_r21_20260817_f1` is therefore
planned for 2026-08-17T09:19:30Z at definition SHA-256
`a9a9aba6aad1eadbb4dc6efc7a7895d5c6028327dd9705ebb4421185b6cedbec`.
The definition and offline validation are preserved mode 0600 under
`/home/mouse9911/.local/state/leo-flow/plans/qual_gauss_r20_r21_20260817_f1/`;
the live state namespace remains absent. It may arm only after exact main-v3
terminal success and fresh Release-F, PG32, capacity, ownership, identity,
both-TX, and DDS gates. No compatibility substitution or main-v3 replay is
authorized.

Expanded isolated main-v3 backfill (2026-08-17): batches `u036` through `u323`
were snapshotted read-only and analyzed in a disposable PostgreSQL/CAS runtime.
FeatureSet, waterfall, and Starlink v0.2 each closed 576/576 jobs, products, and
projections; every clean projection succeeded on attempt one. The Starlink
closure contains 384 eligible recordings, 6,144 stream suites, and 49,152
eight-method rows. The 192 clipped 1.25 MS/s recordings are terminal
`not_evaluated`. No live radio, PostgreSQL 55433, dashboard, campaign journal,
or production CAS write occurred. The disposable runtime was removed after
evidence publication. Durable evidence is at
`/home/mouse9911/.local/state/leo-flow/evidence/shadow-analysis-v3-backfill-20260817/`;
its 223 payloads pass their recorded checksums.

The same evidence includes an independent `leo-tracker`-only reconstruction at
revision `0bb80d14759fd8496b74e7d3219a690be18565a6`. It recovered all 96 frozen
report thresholds from the exact 2,544-sidecar freeze and reproduced every
published fire count. A full same-rule replay then covered all 256 compatible
main-v3 recordings at 2.5/5 MS/s and 80/160 ms: 3,072 observations and 24,576
complete method scores. Current candidate-fire rates were consistently
2.27--2.88 percentage points below the matched frozen reference, with strong
shared temporal variation across windows. Same-IQ numerical-oracle comparisons
for all eight methods agree to floating-point precision (maximum
score/control/margin delta `1.17e-13`). These are exploratory candidate fire
rates, not calibrated beacon detections; no threshold or detection count was
promoted.

Main-v3 terminal and Release-F qualification (2026-08-17): campaign
`main_gauss_r20_r21_20260817_v3` reached exact terminal capture success with
936/936 synchronized batches and 1,872/1,872 unique successful recordings,
no halt or replay. Fresh Release-F qualification
`qual_gauss_r20_r21_20260817_f3` subsequently completed all nine cells and
18/18 recordings with maximum paired first-sample skew 56,015,509 ns. Its
post-capture closure audit proved 18 FeatureSet, 18 waterfall, and 18 Starlink
v0.2 jobs and projections terminal: six clipped 1.25 MS/s recordings were
`not_evaluated` with zero streams/methods, while twelve eligible 2.5/5 MS/s
recordings each produced 16 streams and 128 method outputs. The closure audit
digest is
`890182240bfe52da7bf96838be5d8201d5f8becfc516df8e952483ca02acea3d`.
Only then was the canonical f3 qualification receipt issued; its file SHA-256
is `83376a36aedcc2404242c134382926e32d722a2e2d280fa6a9aadb9e90343a68`
and its canonical receipt digest is
`911c78433bf489dbfa17f832ff862a5face00ec9d5ec2b523e8db7a8ec515e80`.

The first F/f3 main attempt, `main_gauss_r20_r21_20260817_v4`, is preserved
halted after three successful batches. A local read-only monitor's shell
command line contained the two target address literals; the existing external
ownership gate deliberately scans `/proc/*/cmdline`, rejected that monitor at
slot-three admission, and the no-catch-up state machine subsequently sealed a
`missed_slot`. This was a monitor/gate interaction, not an RF, radio, network,
or cadence failure. V4 was not replayed. All later monitoring avoids target
address literals in process command lines.

Fresh capture-only campaign `main_gauss_r20_r21_20260817_v5`, definition
`sha256:6e4148b4a2739fe7eff565f7364b7c865d42f6f3b6fd7986e679f6744b4054a7`,
then proved its first exact 36-batch window: 72/72 unique successful
recordings, no halt/retry/analysis-journal mutation, maximum skew 60,591,138 ns,
and maximum start lateness 264,907,274 ns. Radio-originated sentinels and a
literal-free ownership audit preserve unchanged boot/iiOD identities, passive
TX gains, and zero DDS scales.

That first V5 window was processed concurrently by the sealed Release-F
campaign-scoped online-analysis path. The production database and dashboard
now contain 72/72 complete FeatureSet projections (3,456 feature rows), 72/72
complete waterfalls (16 tiles and 262,144 cells per recording), and 72/72
Starlink v0.2 projections. Exactly 24 clipped recordings are terminal
`not_evaluated`; 48 eligible recordings contain 768 analyzed streams and 6,144
method outputs, with calibrated detection count intentionally absent. Capture
advanced during the analysis slice. The 60-second user timer was enabled only
after this exact closure proof. The host user manager cannot apply system-unit
namespace/capability sandbox directives and returned `218/CAPABILITIES` before
program execution; the user-unit templates now rely on the unprivileged user,
closed-tree release verifier, immutable absolute inputs, restrictive umask,
and resource limits instead of unsupported directives.

Registered historical backfill design (2026-08-17): source migration
`0033_registered_analysis_during_capture.sql` adds the narrow capture-only
`capture_registered_analysis_safe_v2` port. Unlike Release F's same-definition
v1 gate, v2 accepts a live compute/projection lease only when its source job is
already a member of an exact terminal 36-batch scope registered through 0032;
the scope may belong to an older campaign. Unregistered jobs, model work,
legacy Starlink v0.1 work, and unscoped projection leases still close capture
admission. The Gauss capture composition selects v2 only in the next sealed
release, allowing main-v3 production backfill to overlap a future campaign
without adding radio capability to analysis or pausing synchronized capture.
Release F and the active V5 services are unchanged.

The additive source passed 884 repository tests (975 environment skips),
strict mypy over 309 source files, Ruff/format over 690 files, two package
tests, and a fresh PostgreSQL 16 proof of 39 focused migration/scope/security/
production-path tests. The disposable database contained exactly 33 migration
receipts; 0033 SHA-256 was
`511ddb95f85a1f20dc6405ab8b033732e104686f54ea38782b806087cb66c522`,
the function owner was `leo_routine_owner`, its search path was fixed to
`pg_catalog, pg_temp`, and only `leo_capture` had runtime execution privilege.
The disposable database was dropped and verified absent. No live migration,
release, service, database, dashboard, radio, campaign journal, or CAS state
was changed by this source audit.

V5 recurring online-analysis checkpoint (2026-08-17): the sealed Release-F
timer completed its third exact window, success indices `72..107`, while the
synchronized capture service remained active. All 72 FeatureSet, 72 waterfall,
and 72 Starlink-suite compute jobs succeeded. Dashboard closure independently
proved 72 complete recording details with 3,456 feature rows, 72 complete
waterfalls with 16 tiles and 262,144 cells each, and 72 Starlink v0.2 views.
The latter contain exactly 24 clipped `not_evaluated` recordings and 48
candidate recordings with 768 streams and 6,144 method outputs; every
calibrated-detection count remains null. The analysis slice exited zero, with
no failed job, and capture had advanced beyond 117/936 with no halt. Durable
window evidence is maintained at
`/home/mouse9911/.local/state/leo-flow/evidence/main-gauss-r20-r21-v5-online-analysis/FIRST_WINDOW_VALIDATION.md`.

The online-analysis runbook now distinguishes the active Release-F/0032
same-campaign timer from future cross-campaign historical backfill. Only the
latter requires a sealed release selecting the v2 gate plus live migration
0033; Release F must never be used for that cross-campaign mode.

Release G offline freeze (2026-08-17): pushed source commit
`99b25fb279641c6c1a126a3659ee65c550bb3b4b` was sealed at
`/home/mouse9911/.local/share/leo-flow/releases/qualification-release-g-b1af076f87c42698`.
Its source-byte manifest is
`b1af076f87c426988b0d3fc807caea06311c7efd8f4413d059b9650aff0e908b`
(863 files), release manifest is
`73ababffa8fa738bb2343605d805699eec2afaf30635187d6e29d72f5607bad0`,
validation receipt is
`b1b9c39757c4a0445a4bfb34dbe4fd410375ac2b80d9e8ce3a44d9253a6635ed`,
and two independent wheels matched at
`f54a6ab2bb6f00e279fb1b234b6cd262eaf39d3219c9f576321aed840f4f462f`.
Both release-local station runtime validations passed; all imports and native
paths resolve inside the final G root, all seven host ABI hashes match, and the
7,503-entry closed inventory passes the hardened verifier with zero writable
files or directories.

A unique disposable PostgreSQL 16 database passed 17 focused 0033/online/
production-path tests with exact 33-receipt head and migration SHA
`511ddb95f85a1f20dc6405ab8b033732e104686f54ea38782b806087cb66c522`.
The v2 function ownership, fixed search path, and capture-only execution grant
were exact; the database was dropped afterward. Release G is offline-only and
not live-authorized: live 0033 rollout plus fresh G radio qualification remain
mandatory gates. Active V5 capture/online analysis continue unchanged on
sealed Release F.

The fourth V5 online-analysis window (`108..143`) subsequently closed with
72/72 successful jobs and dashboard projections in each of the FeatureSet,
waterfall, and Starlink v0.2 lanes. Its dashboard contains 3,456 feature rows,
72 complete 16-tile/262,144-cell waterfalls, 24 clipped `not_evaluated`
Starlink views, and 48 candidate views totaling 768 streams and 6,144 method
outputs. Detection counts remain null. The slice exited zero and synchronized
capture advanced to 164/936 without a halt during the analysis.

The fifth V5 online-analysis window (`144..179`) then closed under the same
sealed Release-F timer with exact window digest
`fc8fc8749aabe51507722ff0cfc7dff7080319b3579aff744db8efba1adfb02b`.
The slice exited zero after 9m06s with no parked work. Independent live
dashboard reads proved 72 complete FeatureSet projections with 3,456 feature
rows, 72 complete waterfalls with 1,152 total tiles and 262,144 cells per
recording, and 72 Starlink v0.2 views: 24 clipped `not_evaluated` and 48
candidate views totaling 768 streams and 6,144 method outputs. Every
calibrated-detection count remains null. Capture advanced from 182 to 199
during the slice and to 206 during the dashboard audit, with 412/412 unique
recordings, maximum observed skew 61,534,172 ns, and no campaign halt.

The sixth V5 online-analysis window (`180..215`) closed with exact digest
`bc1a050a6722cd4b6a303106cca21a30c6da77e6c98ec708de15a47777c1cb2d`.
The sealed Release-F slice exited zero after 9m15s with no parked work.
Independent dashboard reads proved 72 complete FeatureSet projections with
3,456 rows, 72 complete 16-tile/262,144-cell waterfalls, and 72 Starlink v0.2
views containing 24 clipped `not_evaluated` and 48 candidate recordings, 768
streams and 6,144 method outputs. All calibrated-detection counts remain null.
Capture advanced from 218 to 235 during analysis and to 238 during the audit,
with 476/476 unique recordings, maximum skew still 61,534,172 ns, and no halt.

Additive report-compatible suite calibration source landed at commit
`daf970c`. It preserves candidate-only v0.2 and defines exact non-poolable
method/radio/receiver/tuning/rate/probe/search cells, strict report-compatible
`reported_score > threshold` decisions, disjoint training/holdout whole-search
nulls, a one-sided Wilson FAR upper gate, and one-sided Wilson positive-
injection detection-probability lower gates. Method decisions explicitly remain
non-beacon evidence pending event clustering. Verification was 886 passed/979
environment-skipped, strict mypy on 311 source files, Ruff on 573 source/test
files, and 45 focused calibration/suite regressions. No calibration threshold
was promoted or applied to the live candidate dashboard.

The seventh V5 online-analysis window (`216..251`) closed with exact digest
`6d58e7bddd996235657e123c09496c23f7de74ba978c508208c54d01274c2a6c`.
The sealed Release-F slice exited zero after 9m19s with no parked work.
Independent dashboard reads proved 72 complete FeatureSet projections with
3,456 rows, 72 complete 16-tile/262,144-cell waterfalls, and 72 Starlink v0.2
views containing 24 clipped `not_evaluated` and 48 candidate recordings, 768
streams and 6,144 method outputs. All calibrated-detection counts remain null.
Capture advanced from 252 to 269 during analysis and to 272 during the audit,
with 544/544 unique recordings, maximum skew still 61,534,172 ns, and no halt.
