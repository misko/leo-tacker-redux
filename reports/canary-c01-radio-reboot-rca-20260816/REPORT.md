# Canary c01 slot-32 radio-side reboot RCA

Date: 2026-08-16 UTC  
Scope: read-only post-failure evidence from Gauss and the exact `.20`/`.21`
radios. No reboot, configuration write, RX capture, firmware action, or retry
was performed while collecting this snapshot. The separately authorized `.21`
passive-gain restoration had already completed before SSH collection.

## Conclusion

`.21` performed a whole-system reboot during slot 32. This was not only an
iiOD restart, link-only flap, or client-side context teardown.

A host-clock-bracketed `/proc/uptime` read estimates `.21` boot at
`2026-08-16T23:21:25.377815040Z` with `±118,229,895 ns` uncertainty. Slot 32's
requested start was `23:21:27.958090604Z`, so the radio rebooted about 2.58 s
before the common requested start. Its new iiOD process started about 3.19 s
after that new boot. The pre-existing client context consequently terminated
as a broken pipe. `.20` remained on its six-hour-old boot and completed its
slot-32 recording.

The evidence does not distinguish a watchdog reset, power interruption, or
another whole-SoC reset cause. The new volatile kernel log contains only a
normal boot sequence; no pre-reset log survived. It contains no post-boot
AD9361/DMA/OOM/watchdog fault. Current `.21` carrier and TCP/30431 are healthy.

## Decisive timeline

| Event | UTC |
| --- | --- |
| Slot 31 requested start | `23:20:57.188859835` |
| Slot 31 observed starts | `.20 23:20:57.371622328`; `.21 23:20:57.386726705` |
| Slot 31 terminal recordings | `.20 23:21:02.880654569`; `.21 23:21:02.879586966` |
| Estimated `.21` whole-system boot | `23:21:25.377815040 ± 0.118230 s` |
| Slot 32 requested start | `23:21:27.958090604` |
| `.21` failed-record allocation | `23:21:27.973866035` |
| `.20` observed first sample | `23:21:28.140129055` |
| `.21` local terminal failure | `23:21:33.264977261` |
| `.20` successful terminal recording | `23:21:34.206425196` |
| Canary terminal `halted/capture_uncertain` | `23:21:34.387776386` |

`.21` never produced an observed first-sample timestamp. Its spool preserves:

```text
recording_id: rec_01M06E2M55Q2Y91J7MH5V1J62D
state: failed
publish_attempts: 0
error: RadioDisconnectedError: Pluto receive disconnected: [Errno 32] Broken pipe
```

`.20` completed and published
`rec_01M06E2M4W0WEF74Y5FX2K6Y4Z`. All eight `.20` segments record four
contiguous refills and `all_ci16_components_vary`.

## Radio comparison

| Evidence | `.20` control | `.21` failed side |
| --- | --- | --- |
| boot ID | `41974bfd-7aa8-4d28-b1c8-57d21c3e05bb` | `d6f89d3a-6856-441f-83db-96c71728e15b` |
| uptime at paired snapshot | `21,692.94 s` | `523.44 s` |
| iiOD | PID 178, start tick 326 | PID 180, start tick 319 |
| firmware | exact `v0.38-plutoplus-spf-libiio-metadata-v5` | same |
| eth0 | carrier up, 1 Gb/s full | carrier up, 1 Gb/s full |
| errors | RX/TX errors 0; missed 0; TX drops 0 | same |
| current IIO buffers | RX 0; TX 0 | RX 0; TX 0 |
| current passive state | TX0/TX1 `-89.75 dB`; 8 DDS scales 0 | same after authorized restoration |

`.21`'s much smaller packet/byte counters are also consistent with the new
boot. `.20` dmesg shows its boot-time link down/up sequence; `.21`'s new dmesg
shows one normal link-up. Neither current log contains an OOM, DMA failure,
AD9361 runtime failure, or watchdog firing record. BusyBox `logread` returned no
matching retained service/kernel messages on either radio.

## Why the canary says `capture_uncertain`

The durable capture and dashboard records are not ambiguous. Dashboard
projection sequence `5712`, written at `23:21:34.369443Z`, is terminal revision
2: `.20` succeeded with the recording above, `.21` failed with
`capture_engine_failed`, and paired analysis is ineligible.

The canary coordinator deliberately writes `CAPTURE_INVOKED` before entering
the capture composition and maps *any* exception escaping that composition to
`capture_uncertain`, because replay is forbidden. The capture composition had
already persisted and projected the terminal batch; its only remaining
operation before returning was the external-radio ownership gate. Therefore
that final ownership gate raised during process/socket teardown. The terminal
batch itself is `succeeded/failed`; `capture_uncertain` describes failure to
return cleanly across the outer coordinator boundary, not uncertainty about
the preserved attempt outcomes.

## Read-only commands and handling

The existing mode-0600 password was supplied to SSH through `pexpect`; it was
never placed in argv, stdout, or this report. Persistent known-host entries
were stale because these firmware images regenerate host keys. Current keys
were collected into a mode-0600 temporary known-host file, used with strict
checking, and deleted when both sessions closed. Before SSH, Release E had
already attested the exact serial/firmware identities at both IP endpoints.

Representative remote commands, run identically on both radios:

```sh
date -u +%Y-%m-%dT%H:%M:%SZ
cat /proc/sys/kernel/random/boot_id
cat /proc/uptime
uptime
cat /opt/VERSIONS
ps w
cat /proc/<iiod-pid>/stat
grep -E '^(State|VmRSS|Threads):' /proc/<iiod-pid>/status
cat /sys/class/net/*/{operstate,carrier}
cat /sys/class/net/*/statistics/{rx_bytes,rx_packets,rx_errors,rx_dropped,rx_missed_errors,tx_bytes,tx_packets,tx_errors,tx_dropped,collisions}
cat /sys/bus/iio/devices/iio:device*/out_voltage*_hardwaregain
cat /sys/bus/iio/devices/iio:device*/out_altvoltage*_scale
cat /sys/bus/iio/devices/iio:device*/buffer/enable
dmesg | grep -Ei 'ad936|iio|dma|cf_axi|eth|link|oom|watchdog|reset|fault|error|disconnect|broken|usb'
logread | grep -Ei 'iiod|spf|ad936|dma|eth|link|oom|watchdog|reset|fault|error|disconnect|broken'
```

The precise boot estimate used an immediate second `.21` read bracketed by
Gauss `time.time_ns()`:

```text
local_before_utc_ns: 1786923048349584993
local_after_utc_ns:  1786923048586044784
round_trip_ns:       236459791
remote_uptime_s:     563.09
estimated_boot_utc_ns: 1786922485377815040
uncertainty_ns:      118229895
```

All SSH processes exited status 0 and the temporary known-host file was
deleted. The canary remains terminal and unreplayed.
