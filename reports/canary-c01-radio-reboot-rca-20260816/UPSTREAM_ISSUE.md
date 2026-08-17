# Whole-board reboot after repeated eight-retune dual-RX metadata captures on v0.38 v5

## Summary

One of two PlutoPlus radios running
`v0.38-plutoplus-spf-libiio-metadata-v5` rebooted during a repeated, bounded
dual-RX metadata-capture campaign. The other identically configured radio
completed the same slot.

The reboot is proven by a new Linux boot ID and short `/proc/uptime`; it was not
only an iiOD restart or a network disconnect. The client subsequently received
`EPIPE` (`Broken pipe`) through its pre-existing libiio context. The immediate
reset cause is unknown because no pre-reset volatile log survived.

This report is intended to add the missing diagnostics and a lifecycle stress
gate to the next firmware. It does **not** claim that metadata-buffer teardown
caused the reboot.

## Firmware and transport

- Board: PlutoPlus, two RX channels enabled
- Release: `v0.38-plutoplus-spf-libiio-metadata-v5`
- Firmware commit: `d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8`
- Release DFU SHA-256:
  `948b46506febacb087f3955be86015e074f8c0e3370a9dfc6a942e735d97f882`
- Radio libiio/iiOD: pinned 0.25 metadata extension
  (`c26258bfa33098c2b215e19cf85d448e89499b1a`)
- Host libiio: 0.25 base
  `c26258bfa33098c2b215e19cf85d448e89499b1a`, plus the RX-integrity close
  barrier patch SHA-256
  `195bddceada230ef32b662cfd7149186a623d1d4cfac234b0660770f32f901d4`
- Host SPF adapter: base `c40ee4116546889effd72056115adaaa1bc3fd40`,
  plus patch SHA-256
  `c9113a6d75466b4d1de38b45ccaee785c6a13a677b1e02c0e7c39919b66669b1`
- Transport: standard libiio IP/TCP to iiOD, with
  `iio,buffer-metadata=1`
- Metadata protocol: `spf-radio-metadata-v3`
- Kernel: `5.15.0-gd798b0d821b8`

## Exact workload before the failure

The campaign used a rational 30.769230769 s slot period. Each slot opened one
libiio/pyadi context per radio, performed eight sequential retunes, and closed
the context after the recording.

Each retune:

1. closed the prior `MetadataBuffer` and destroyed the prior pyadi RX buffer;
2. enabled RX1/RX2 (`I0,Q0,I1,Q1`, scan mask `0x0f`);
3. set slow-attack AGC, sample rate, RF bandwidth, and LO;
4. opened a fresh finite metadata buffer with two kernel buffers;
5. acquired an exact integer number of 40 ms refills; and
6. closed the metadata buffer before the next retune.

The nine-cell matrix was:

| Sample rate | Dwell per retune | Samples | 40 ms refills |
| ---: | ---: | ---: | ---: |
| 1.25 MS/s | 40 / 80 / 160 ms | 50k / 100k / 200k | 1 / 2 / 4 |
| 2.5 MS/s | 40 / 80 / 160 ms | 100k / 200k / 400k | 1 / 2 / 4 |
| 5 MS/s | 40 / 80 / 160 ms | 200k / 400k / 800k | 1 / 2 / 4 |

The eight IF centers were 959.6875, 1190.3125, 1209.6875, 1440.3125,
1459.6875, 1690.3125, 1709.6875, and 1940.3125 MHz. Their order alternated by
slot. The RF content is not believed to be relevant; the important property is
eight metadata-buffer create/refill/destroy lifecycles and retunes per context.

Slots 0 through 31 completed on both radios. Thus, before the failure, each
radio had completed 32 context lifecycles, 256 retuned metadata-buffer
lifecycles, and 584 finite metadata refills. No TX buffer was opened. DDS scales
were zero, and application preflight had set both TX gains to the maximum
attenuation before capture.

## Observed result

Timeline (UTC, 2026-08-16):

| Event | Time |
| --- | --- |
| Slot 31 requested start | `23:20:57.188859835` |
| Slot 31 recording completed on both radios | approximately `23:21:02.88` |
| Failed radio's estimated new boot | `23:21:25.377815040 ± 0.118230 s` |
| Slot 32 requested start | `23:21:27.958090604` |
| Control radio first sample | `23:21:28.140129055` |
| Failed radio returned `EPIPE` | `23:21:33.264977261` |

The reboot therefore occurred approximately 22.5 seconds after both slot-31
recordings completed and approximately 2.58 seconds before slot 32's requested
start. The failed radio did not produce a slot-32 first-sample timestamp. The
control radio completed and published its recording.

The failed client result was:

```text
RadioDisconnectedError: Pluto receive disconnected: [Errno 32] Broken pipe
```

Post-failure paired observation:

| Evidence | Control radio | Failed radio |
| --- | ---: | ---: |
| `/proc/uptime` | 21,692.94 s | 523.44 s |
| iiOD | PID 178, start tick 326 | PID 180, start tick 319 |
| Ethernet errors / missed packets | 0 | 0 |
| Active RX/TX IIO buffers | 0 / 0 | 0 / 0 |

The failed radio had a new boot ID. A host-clock-bracketed `/proc/uptime` read
estimated its boot time as:

```text
local_before_utc_ns:   1786923048349584993
local_after_utc_ns:    1786923048586044784
remote_uptime_s:       563.09
estimated_boot_utc_ns: 1786922485377815040
uncertainty_ns:        118229895
```

The new iiOD began about 3.19 seconds after the new kernel boot. Current dmesg
showed a normal boot and no post-boot AD9361, DMA, OOM, or watchdog fault. It
cannot reveal what happened before reset.

After the reboot, both TX hardware-gain attributes had returned to the firmware
default of `-10 dB`; all eight DDS scales remained zero. This is consistent
with a full board reboot and with the release's documented boot behavior. The
application restored and read back `-89.75 dB` with DDS zero before permitting
further capture.

## Expected result

Repeated bounded RX metadata sessions and their teardown must not reset the
board. If iiOD or a kernel teardown path becomes stuck, recovery should be
bounded and observable without silently escalating to an unattributed
whole-board reboot. A client connected across a genuine reboot will naturally
fail, but the next boot should retain enough evidence to identify the reset
source.

## Smallest candidate reproducer

This smaller reproducer has **not yet reproduced the failure**, so it should be
treated as a stress reproducer, not proof of causation. It reuses the release's
`tests/test_buffer_metadata_e2e.c` primitives and removes all application,
storage, analysis, and dual-host coordination code.

Extend that test so one outer iteration creates one network context, enables
all four RX scan elements, sets the device kernel-buffer count to two, and then
runs eight inner buffer lifecycles:

```c
for (unsigned int slot = 0; slot < 936; ++slot) {
        struct iio_context *ctx = iio_create_network_context(argv[1]);
        assert(ctx);
        struct iio_device *dev =
                iio_context_find_device(ctx, "cf-ad9361-lpc");
        assert(dev);

        enable_all_four_rx_scan_elements(dev);
        assert(iio_device_set_kernel_buffers_count(dev, 2) == 0);

        const unsigned int refills = (unsigned int[]){1,2,4}[slot % 3];
        const size_t samples =
                (size_t[]){50000,50000,50000,100000,100000,100000,
                           200000,200000,200000}[slot % 9];

        for (unsigned int retune = 0; retune < 8; ++retune) {
                /* Optionally write the eight LO values above before opening. */
                struct iio_buffer *buf =
                        iio_device_create_buffer_with_metadata(dev, samples);
                assert(buf);
                for (unsigned int n = 0; n < refills; ++n) {
                        uint8_t metadata[64 * 1024];
                        size_t metadata_bytes = 0;
                        assert(iio_buffer_refill_with_metadata(
                                buf, metadata, sizeof(metadata),
                                &metadata_bytes) == (ssize_t)samples * 8);
                        assert(metadata_bytes > 0);
                }
                iio_buffer_destroy(buf);
        }
        iio_context_destroy(ctx);

        /* Maintain 30.769230769 s slot starts; do not run catch-up bursts. */
        sleep_until_next_absolute_slot();
}
```

Run the same binary independently against two radios, while recording before
and after every slot:

```sh
cat /proc/sys/kernel/random/boot_id
cat /proc/uptime
pidof iiOD
cat /proc/$(pidof iiOD)/stat
```

For the closest reproduction, cycle rates/dwell/refill sizes over the nine-cell
matrix and write the eight LO centers. For isolation, useful reductions are:

1. one radio instead of two;
2. no retunes, retaining only eight buffer lifecycles;
3. one buffer lifecycle per context;
4. ordinary `iio_device_create_buffer()` instead of metadata buffers; and
5. a persistent context across all slots.

These variants distinguish metadata framing, retune, buffer teardown, context
teardown, and dual-radio/power effects.

## Established facts vs. hypotheses

Established:

- The affected unit performed a whole-system reboot; boot ID and uptime prove
  this.
- The other unit did not reboot and completed the same scheduled capture.
- The old TCP/libiio context later failed with `EPIPE`.
- iiOD started during the new boot, rather than merely restarting within the
  old boot.
- Volatile logs contain no pre-reset cause.
- TX gains reset to `-10 dB` and DDS stayed disabled, matching documented boot
  state.

Not established:

- Whether the reset came from supply instability, a watchdog, a kernel fault,
  explicit reset logic, or another SoC reset source.
- Whether metadata-buffer/context teardown contributed.
- Whether the issue is deterministic. This is one observed reboot in this
  workload; the control radio remained healthy.

The repeated base-v5 close/retune lifecycle is a plausible stressor worth
testing because it is the main lifecycle difference from a long-lived stream,
but it is not yet a root-cause finding. A power-supply cause must remain in the
hypothesis set.

## Diagnostics requested for the next firmware

1. Preserve and expose the Zynq reset-reason/watchdog status before boot code
   clears it, together with a monotonic boot counter.
2. Add persistent or pstore/ramoops-backed last-gasp kernel and iiOD lifecycle
   evidence, bounded so it cannot exhaust flash.
3. Expose iiOD process generation and RX ownership/lifecycle state through a
   passive query that opens no DMA buffers.
4. Log each metadata-buffer create/destroy and kernel DMA ownership transition
   with a bounded generation number and terminal result.
5. Add a wall-clock cleanup watchdog that first reports the stuck phase and
   recovers iiOD/service ownership; do not silently turn a teardown timeout into
   a whole-board reset.
6. Consider applying maximum TX attenuation during boot, before services become
   reachable, while leaving DDS disabled.

## Acceptance criteria

- The candidate stress test completes 936 slots on each of two radios across at
  least three cold-boot/power epochs with unchanged boot IDs, no unexpected
  iiOD generation change, no `EPIPE`, and no leaked RX ownership.
- The exact nine-cell/eight-retune workload completes with valid metadata,
  bounded close time, and zero active RX/TX buffers after each context closes.
- Forced iiOD termination recovers the service without rebooting the board and
  increments an observable iiOD generation.
- A deliberately forced watchdog/board reboot leaves an unambiguous reset
  reason, boot counter, prior iiOD generation, and last lifecycle phase readable
  after boot.
- Firmware boot establishes and exposes the safe TX state before network IIO is
  ready, or explicitly documents and tests the application gate required to do
  so.

## Related work / duplicate check

The repository currently has GitHub Issues disabled. The available pull
requests were checked for the same symptom. PRs
[#22](https://github.com/misko/plutosdr-fw/pull/22) and
[#23](https://github.com/misko/plutosdr-fw/pull/23) address direct-IP
control-rearm and RX-ownership races, but neither reports or proves this
whole-board reboot. PR
[#1](https://github.com/misko/plutosdr-fw/pull/1) addresses supervised
direct-USB process recovery, also without this reboot signature. No duplicate
issue could be found because this repository has no issue tracker.
