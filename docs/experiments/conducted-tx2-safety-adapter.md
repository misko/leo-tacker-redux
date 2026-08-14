# Conducted TX2 safety adapter

Status: hardware-free implementation and failure tests complete; no radio was
contacted and no transmission was performed during implementation.

The adapter is `leo_flow.fixtures.conducted_tx2`. It is a bench-only boundary
for sending a generated CI16 fixture through V5 TX2. It is not a capture
adapter, service, deployment plugin, or discovery tool. Capture remains
receive-only and has no import of this module.

## Mandatory gates

`run_conducted_tx2_ladder(plan, device_factory)` validates the entire plan and
all waveform bytes before it calls the factory. A plan is admitted only when:

- the URI is one exact, nonempty standard-libiio `ip:` or `usb:` URI;
- `armed_radio_serial` exactly equals the nonempty expected V5 serial;
- the expected host runtime and radio use the existing qualified V5 attestation
  contracts, including the exact firmware, metadata capability, paired scan
  layout, TX2 DDS layout, and pre-existing TX2 mute state;
- the physical attestation names the same serial, the exact
  `TX2->ATTENUATORS->PASSIVE_SPLITTER->RX1+RX2` topology, an identified passive
  splitter/tee, and identified attenuators on both paths;
- each attested TX2-to-RX path has at least 30 dB attenuation and the exact
  antenna-free confirmation is present;
- LO, sample rate, number of samples, waveform digest, RMS, and component peak
  are inside the fixed bounds below; and
- the first step is 16 RMS counts at 80 dB TX attenuation, with each later
  step advancing exactly one adjacent level or attenuation rung.

After the exact context is opened, it remains read-only while the adapter uses
the existing V5 runtime and radio observers and `attest_v5` gate. That gate
checks the qualified host libraries, firmware release and capability, paired
2RX/2TX layout, serial, TX2 gain, and TX2 DDS channel layout/state. A mismatch
closes the context without changing that radio. After attestation and a second
exact serial read succeed, the adapter destroys any TX buffer, disables and
reads back all four TX2 DDS scales, sets and reads back TX2 gain at `-80 dB`,
and only then configures the finite run. LO, sample rate, gain, and DDS state
are checked again before every transmission.

## Hard bounds

| Item | Allowed value |
|---|---|
| TX attenuation ladder | 80, 70, 60, 50, 40 dB |
| Fixture RMS ladder | 16, 32, 64, 128 CI16 counts |
| First step | 80 dB attenuation and 16 RMS counts |
| Step transition | One adjacent rung in one dimension only |
| Maximum steps | 8 |
| Maximum waveform | 262,144 complex samples per step |
| Maximum CI16 component peak | 512 counts |
| Sample rate | 1–5 MS/s |
| TX LO | 325 MHz–3.8 GHz |
| TX mode | non-cyclic, one finite buffer per step |
| libiio I/O timeout | 5 seconds, installed before serial observation |
| Muted state | TX2 gain `-80 dB`; DDS scales 4–7 all zero |
| Minimum attested path attenuation | 30 dB independently to RX1 and RX2 |

These are code constants, not caller-configurable policy. A larger waveform,
higher gain, longer run, different topology, skipped rung, or cyclic buffer
requires a separately reviewed adapter change.

## Interface

The immutable inputs are:

- `ConductedFixtureAttestation`: serial, exact topology and no-antenna
  confirmation, splitter identity, attenuator identities, and measured or
  verified path attenuation;
- `FiniteTx2Waveform`: CI16 bytes, declared RMS level, and SHA-256 digest;
- `Tx2LadderStep`: one permitted positive attenuation and waveform pair; and
- `ConductedTx2Plan`: exact URI/serial arm, topology attestation, the existing
  `ExpectedV5Runtime` and `ExpectedV5Radio` contracts, LO, rate, and a finite
  tuple of steps. The expected radio must retain the qualified four-component
  paired layout, TX2 DDS mapping, and `-80 dB` pre-mutation ceiling.

`open_exact_pyadi_tx2` is the production device factory. It lazily imports
pyadi/NumPy and constructs `adi.ad9361(uri=the_exact_uri)` without discovery.
Before the runner permits mutation, the device applies the existing V5
runtime/radio attestation described above. For each finite send it selects TX
channel 2 (`tx_enabled_channels = [1]`), forces and reads back
`tx_cyclic_buffer = False`, converts the armed CI16 bytes in memory, and lets
pinned pyadi 0.0.21 infer the non-cyclic buffer length from that array. The
adapter does not invent a `tx_buffer_size` property that pyadi does not expose.

The successful result is `ConductedTx2Evidence`, containing exact radio/URI
identity and per-step gain, LO, rate, sample count, level, and waveform digest
readbacks. It is returned to an operator-owned harness; this adapter does not
write files, publish scientific truth, invoke capture, or run analysis.

## Cleanup contract

Each individual step has a `finally` mute. The whole operation has an outer
cleanup that independently attempts all of the following even if a prior
cleanup action fails:

1. destroy the TX buffer;
2. zero all TX2 DDS scales;
3. restore TX2 gain to `-80 dB`;
4. verify gain and all DDS readbacks; and
5. close the exact context.

A cleanup failure is surfaced as `Tx2CleanupError`; success is never reported
when the final mute cannot be verified. Before serial identity is established,
only context close is allowed, so a wrong device is not mutated. Pinned pyadi
0.0.21 and pylibiio 0.25 release native contexts by dropping references; the
adapter therefore clears both its pyadi-device and direct-context references
even if pyadi close raises, instead of retaining the context behind a nominal
close.

## Qualification sequence

Before the first real use, a reviewer and operator should independently verify
the serial label, splitter/tee identity, attenuator identity and rating on each
branch, DC compatibility, absence of any antenna or LNB, and safe receiver
input limits. Begin with a one-step plan only. Inspect RX1 and RX2 RMS,
clipping, continuity, and pilot contrast before authoring the next adjacent
rung. Stop at the first unexpected readback or receive measurement.

There is deliberately no CLI or installed service in this slice. The first
hardware operation should be a separately reviewed, supervised harness that
constructs the immutable plan explicitly and retains the returned evidence
beside the capture result.

Run the hardware-free safety suite with:

```text
.venv/bin/python -m pytest tests/fixtures/test_conducted_tx2.py -q
```
