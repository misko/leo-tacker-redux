# SATPI01 Pluto `.20` / `.21` recovery investigation

Date: 2026-08-16 UTC

Scope: bounded receive-only investigation from `satpi01@satpi01`. No Redux
recording was published, no transmit source was enabled, and no radio was
rebooted or flashed by this investigation.

## Executive result

| Item | `.20` / `...5d4d` | `.21` / `...19f2` |
| --- | --- | --- |
| Physical SATPI01 attachment | USB `1-1`, libiio `usb:1.125.5`, gadget NIC `eth1`, console `ttyACM0` | USB `3-1`, libiio `usb:3.5.5`, gadget NIC `eth2`, console `ttyACM2` |
| Firmware | `v0.38-plutoplus-spf-libiio-metadata-v5` | same |
| TX safety | TX1/TX2 `-89.75 dB`; all eight DDS scales zero | same |
| Ordinary libiio RX | **Failed:** frozen in every tested capture and reopen | **Passed:** both receivers varied in every tested capture and reopen |
| Direct protocol-v2 RX | **Failed:** RX0 fixed, RX1 zero despite structurally valid metadata | **Passed:** both receivers nonzero and varying; metadata valid |
| Radio-local kernel evidence | Four `ad9361 ... Failed to read gain, state m/c at 0` messages | No corresponding gain-state-machine messages |
| Wired IP | `.20/22`, carrier up; reachable from Gauss | `.21/22`, carrier up; reachable from Gauss |
| SATPI01 LAN reachability | SATPI01 cannot ARP the radio | same |

The `.20` failure reproduces locally over direct USB, through ordinary libiio
and the custom metadata-capable USB protocol. It is therefore below Redux,
Gauss networking, IP transport, and publication. `.21` is healthy across the
same local tests. A byte/shape/metadata-success check alone is insufficient:
the retired collector had been publishing correctly sized `.20` files whose
sample content was constant.

## Ownership removal

The exclusive USB owner was identified before it was changed:

- system unit: `leo-sync-scan.service`
- command: `/home/satpi01/leo-tracker/.venv/bin/python3 /mnt/leo-nvme/leo-tracker/bin/synccollect.py`
- policy: enabled with `Restart=always`
- observed PID: `229743`
- historical systemd restart count: `1432`

The user authorized decommissioning this radio monitor. The unit was stopped
and disabled through systemd. After a bounded ten-second observation it was
inactive/dead, disabled, `MainPID=0`, absent from the process table, and held
neither USB device. It did not restart. No captured data was deleted, and the
last bounded scan found no recent partial directory lacking `sweep.json`.

The exact reversible restoration operation, if this obsolete collector is
ever intentionally reinstated, is `sudo systemctl enable --now
leo-sync-scan.service`. It must not be restored while Redux or another owner
uses these radios, and it needs a signal-integrity admission check before it
can be trusted.

## Existing-corpus content check

Five recent completed sidecars were selected without opening a radio. Three
were available by the time the bounded command resolved. Every file size
matched its declared shape. Across all sampled `.20` tuning/receiver blocks,
however:

- RX0 was exactly the repeated IQ tuple `[51, -193]`;
- RX1 was exactly `[0, 0]`;
- sampled unique-pair count was one and standard deviation was zero;
- the failure persisted across changing tuner labels and capture files.

The paired `.21` files had at least 8,149 sampled unique IQ pairs per tested
receiver/tuning block, nonzero standard deviation, and changing content.

## Fresh ordinary libiio result

Each radio was tested separately at 2.5 MS/s with a 40 ms, 100,000-paired-sample
dual-receiver refill. Four fresh libiio contexts were opened per radio and two
buffers were collected in each context, exercising both buffer teardown and
context close/reopen.

- `.20`: all eight complete matrices had the same SHA-256
  `eb5d585b6854fa6f7ed261038b696aa71b19ee2279b7c7fbe3b1bfe4c352a4fb`.
  RX0 was `[51,-193]` throughout and RX1 `[0,0]` throughout.
- `.21`: all eight matrix digests differed. Each receiver had approximately
  16.5k unique pairs in the bounded sample, nonzero I/Q spread, and changing
  first/last tuples.

This is a stronger reproduction than the prior Gauss failure because it uses
local USB and survives repeated host context teardown/reopen.

## Fresh direct metadata result

`spf.sdrpluto.direct_usb_smoke` requested three protocol-v2 frames of 65,536
samples per receiver from each exact serial.

- Both gadgets reported protocol range 1-3, feature mask 247, valid hardware
  identity, sequential buffer/sample counters, valid gain metadata, and valid
  RSSI metadata.
- `.20` returned identical per-frame RMS values: RX0 `199.6246337890625`, RX1
  `0.0`; its per-frame nonzero flags were `[true,false]`, so the smoke command
  correctly exited 1.
- `.21` returned both receivers nonzero, frame RMS values near 520-548, and
  exited 0.
- Runtime status on both gadgets was `IDLE`, with no reported IIO refill, USB
  submit, short-write, starvation, control, or stop-timeout errors. Both had
  gadget build ID `ab270f9e3128187372f27de887be65353f9e195d`.

Valid metadata and clean gadget error counters therefore do not rehabilitate
the `.20` payload.

## Radio-local evidence

Serial consoles were bound by USB topology before login (`ttyACM0` -> `.20`,
`ttyACM2` -> `.21`). Radio-local inspection found:

- both run kernel `5.15.0-gd798b0d821b8` and the same V5 release;
- both run `iiod`, `sdr_usb_gadget`, `sdr_ip_gadget`, and the normal gadget
  supervisor;
- both RX DMA interrupt counters were active and of comparable magnitude;
- `.20` alone logged four AD9361 gain-state-machine read failures;
- `.20` is configured as `192.168.1.20/22`; `.21` as
  `192.168.1.21/22`; both Ethernet links report carrier and `UP`.

No destructive register access, service restart, QSPI/MTD access, or firmware
write was attempted.

## Network finding

Gauss can ping both `.20` and `.21` and establish TCP connections to both IIO
ports (30431). Each radio can ping the LAN gateway. SATPI01 cannot obtain an
ARP response from either radio (`arping`: 0/3 for each), and neither radio can
ping SATPI01 at `192.168.1.186`. SATPI01's firewall input policy is accept and
its relevant `arp_ignore`/`arp_filter` values are zero.

This is consistent with an intervening L2 port/VLAN/client-isolation path
between SATPI01 and the radios. It is not evidence of a wrong `.20`/`.21`
`config.txt`: their IPs, masks, carrier, gateway path, and Gauss path are all
working. Use direct USB from SATPI01 or IP from Gauss until the switch/VLAN
path is inspected.

## Candidate recovery readiness

The existing candidate package was reverified on Gauss before staging:

| Artifact | Size | SHA-256 |
| --- | ---: | --- |
| candidate DFU | 12,712,743 | `4118a4f3a7130e407f4314e76415bbcf9183501e74faae1824ef2b52be616503` |
| base V5 rollback DFU | 12,743,875 | `948b46506febacb087f3955be86015e074f8c0e3370a9dfc6a942e735d97f882` |
| firmware receipt | 4,687 | `04127cf7571f74f3c1d9a992bbb6931ad0068b5d804c7bec9d75a1ddae8da57c` |

The bytes are staged, mode `0600`, under
`/home/satpi01/.local/state/leo-radio-recovery/firmware/rx-integrity-candidate1/`.
Remote hashes match the source. Nothing was loaded into a radio. SATPI01 has
`dfu-util 0.11`, 16 GiB free on `/`, and exact USB identity paths for both
radios.

The remaining execution blocker is important: the reviewed Pluto+ Utils
guarded firmware helper and its site-specific exact-radio DFU-transition
adapter are not deployed on SATPI01. Raw `dfu-util`, SSH MTD, and network QSPI
must not substitute for that identity seam. A safe maintenance continuation
must either deploy/review that helper on SATPI01 or physically attach `.20` to
the already prepared Gauss updater host.

After that boundary is closed, the smallest guarded sequence is:

1. Keep `leo-sync-scan.service` disabled and prove no USB/IIO owner.
2. Bind `.20` to serial `1040005e0b100007100010000bf33a5d4d` and USB sysfs
   path `1-1`; rehash the candidate and rollback DFUs.
3. Create a new, short-lived `volatile_dfu` plan bound to that identity,
   current base V5 release, and candidate hash. Execute no persistent update.
4. Bound re-enumeration; require the same serial and candidate release.
5. Reapply and read back passive TX state because reboot resets gains.
6. Run the same local USB 2.5 MS/s x 40 ms ordinary dual-RX test through at
   least one buffer destroy/reopen. Require all I/Q components to vary, both
   receivers nonzero, distinct frame hashes, valid geometry, and no new
   kernel/gadget error counter.
7. From Gauss, run the already checked candidate `.20` one-refill station plan
   so the patched host/runtime/metadata chain and capture-owned constant-IQ
   validator are exercised together. Do not reuse any failed plan identity.
8. On any failure, retain diagnostics and use the exact base V5 DFU through a
   separate guarded volatile rollback plan. A power cycle also returns a
   volatile candidate to persistent base V5.
9. Only a successful volatile canary permits consideration of a separately
   planned `persistent_qspi` update; `.21` should not be changed until `.20`
   passes.

## Ranked diagnosis and recommendation

1. **Most likely: `.20` radio-side RX/FPGA/DMA state is wedged.** Local USB,
   ordinary libiio, and direct metadata all return the same frozen payload;
   radio-local AD9361 errors distinguish `.20` from healthy `.21`.
2. **Likely contributing defect: base V5 final-client teardown lacks the
   reviewed iiOD close barrier.** The existing candidate directly targets this
   lifecycle and is the smallest already built/reviewed intervention.
3. **Not causal for frozen IQ: Gauss IP, Redux, storage, and analysis.** The
   failure exists before those layers.
4. **Separate operational issue: SATPI01-to-radio LAN L2 isolation.** Fix the
   switch/VLAN path if SATPI01 must use the wired IPs; it does not block local
   USB recovery or Gauss IP operation.

Recommended recovery order: complete the exact-radio guarded updater seam,
volatile-load only `.20`, run the strict USB and Gauss canaries, roll back on
any mismatch, then decide whether persistence is justified. Preserve `.21`
as the healthy comparator throughout.

## Manual-shutdown readiness checkpoint

At `2026-08-16T17:28:11Z`, immediately before the user began a manual shutdown:

- the radio collector remained inactive/disabled with no USB, TTY, or radio
  TCP owner;
- no firmware operation or diagnostic capture was in progress;
- an unrelated `leo-sync-import.service` start job had an `importsync.py`
  process in uninterruptible `D` state, and the normal storage-reconcile
  watcher was active;
- both Pluto descriptors say `Bus Powered`, `MaxPower 500mA`, with USB runtime
  power active.

Consequently an orderly OS shutdown is required so systemd can stop services
and sync the NVMe/NFS paths. Do not pull SATPI01 power merely because SSH
drops. Also assume the radios receive power from SATPI01 USB: Pi shutdown may
leave VBUS energized, so a definite radio reboot requires physically removing
and restoring the radios' USB/external power after SATPI01 is fully halted.
Gauss IP connectivity will remain only if the radios remain independently
powered; it will disappear while they are actually power-cycled.

## Representative read-only commands

```console
ssh -o BatchMode=yes satpi01@satpi01 'iio_info -s'
ssh -o BatchMode=yes satpi01@satpi01 'lsof /dev/bus/usb/001/125 /dev/bus/usb/003/005'
ssh -o BatchMode=yes satpi01@satpi01 'systemctl status leo-sync-scan.service --no-pager'
ssh -o BatchMode=yes satpi01@satpi01 'iio_attr -u usb:1.125.5 -C'
ssh -o BatchMode=yes satpi01@satpi01 'iio_attr -u usb:3.5.5 -C'
ssh -o BatchMode=yes satpi01@satpi01 'arping -I eth0 -c 3 -w 5 192.168.1.20'
ssh -o BatchMode=yes satpi01@satpi01 'arping -I eth0 -c 3 -w 5 192.168.1.21'
```

## Superseding post-hard-reboot result from Gauss

The user physically disconnected `.20` and `.21` from SATPI01 and hard
power-cycled them. SATPI01 was then treated as decommissioned and was not
contacted again. A bounded Gauss-only poll initially saw both endpoints down;
both returned by `2026-08-16T17:30:15Z`.

Both identity gates passed: `.20` returned serial `...5d4d`, `.21` returned
serial `...19f2`, and both returned the exact base V5 release plus
`iio,buffer-metadata=1`. As expected after a cold boot, TX0/TX1 gain had reset
to `-10 dB` on both radios. Testing stopped before RX. Under explicit user
authorization, and only after repeating identity/release and foreign-owner
checks, TX0/TX1 were restored to the established passive `-89.75 dB`. All
eight DDS scales were read, not written, and remained exactly zero.

The hard reboot recovered `.20`'s IQ path without a firmware change:

| Gate | `.20` | `.21` |
| --- | --- | --- |
| Ordinary RX | pass: 4 new contexts x 2 buffers | pass: 4 new contexts x 2 buffers |
| Ordinary variation | about 16.4k-16.6k sampled unique IQ pairs per receiver; every I/Q component varied | about 16.6k sampled unique pairs; every component varied |
| Ordinary hashes | 8/8 distinct | 8/8 distinct |
| V3 metadata RX | pass: 3 new contexts x 3 frames | pass: 3 new contexts x 3 frames |
| Sequence | `0,1,2` per session; first-sample counter advanced exactly 100,000 | same |
| Metadata | scan mask `0x0f`; gain, RSSI, and sample-time valid on 9/9 frames | same |
| Metadata hashes | 9/9 distinct; both receivers varied | 9/9 distinct; both receivers varied |

A final simultaneous test used two separate OS processes, one per endpoint.
Their host process starts were 4.324679 ms apart. Each process captured three
metadata frames; both returned sequence `0,1,2`, exact 100,000-sample counter
increments, valid metadata, changing dual-channel IQ, and three distinct
hashes. This is a concurrency health check, not a hardware synchronization
claim.

Final safety and ownership readback passed: both radios retained TX0/TX1
`-89.75 dB`, every DDS scale was zero, exact identities/releases were intact,
and no Gauss TCP/process owner remained. Nothing was published or written to
PostgreSQL/CAS.

This supersedes the earlier immediate diagnosis in one important respect:
`.20` is not presently frozen and does not require a candidate firmware load
to resume bounded qualification. The evidence now supports a radio-side state
wedge cleared by a true hard power cycle. The base V5 teardown/lifecycle risk
is still real because the same image previously re-entered a frozen state and
reported success-shaped buffers. Continuous operation must therefore retain
the capture-owned per-component variation gate, preserve the candidate as a
fallback, and prove a fresh repeated/reopen qualification before long-running
collection.
