# Conducted TX2 safety adapter

Status: adapter and supervised one-shot runner implemented. On 2026-08-14 the
radio at `192.168.1.15` was contacted only through read-only libiio inspection;
no transmission or attribute write was performed.

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

## Supervised one-shot runner

`python -m leo_flow.fixtures.conducted_tx2_runner` is an operator-facing
one-shot harness, not a service. Its strict JSON config contains exactly one
physical `ConductedFixtureAttestation`, TX LO, and sample rate. Unknown or
missing fields fail closed. The `.15` URI, host-runtime expectations, and radio
identity are immutable code constants, not operator-configurable fields, so a
config cannot redefine an arbitrary current runtime as qualified. The runner
constructs one fixed 4,096-sample,
16-RMS-count waveform at 80 dB TX attenuation. It contains two unmodulated
tones at ±117,187.5 Hz, corresponding to the inner pair of lower-edge pilot
bins. It is a spectral smoke-test stimulus, not a claim that the published
pilot coding, frame occupancy, or complete Starlink waveform is present.

Dry-run is the default. It validates every plan and waveform gate without
loading pyadi, opening a context, or contacting a radio, then creates one new
immutable receipt containing the exact plan and waveform digests:

```text
python -m leo_flow.fixtures.conducted_tx2_runner \
  --config /absolute/path/conducted-tx2.json \
  --receipt /absolute/path/conducted-tx2-dry-run.json
```

An optional live preflight opens only the exact configured URI, runs the full
pinned-host/V5 radio attestation, reads the exact serial, and closes the context.
It calls no mutable TX port:

```text
python -m leo_flow.fixtures.conducted_tx2_runner \
  --config /absolute/path/conducted-tx2.json \
  --receipt /absolute/path/conducted-tx2-preflight.json \
  --preflight
```

Transmission requires all of the following in one invocation: `--arm`, a
previous passing dry-run receipt for the byte-identical plan, a new result
receipt path, the exact serial repeated by the operator, and the exact current
antenna-free topology confirmation. The output receipt path must not already
exist. This preserves both the dry-run and final cleanup evidence.

```text
python -m leo_flow.fixtures.conducted_tx2_runner \
  --config /absolute/path/conducted-tx2.json \
  --receipt /absolute/path/conducted-tx2-result.json \
  --arm \
  --arm-from-dry-run /absolute/path/conducted-tx2-dry-run.json \
  --confirm-radio-serial 104000b29905000e17000800065934759d \
  --confirm-conducted-topology TX2_CONDUCTED_RX1_RX2_NO_ANTENNA
```

A successful armed receipt includes the exact control readbacks and a cleanup
result verifying that the buffer was destroyed, DDS was disabled, TX2 was
returned to `-80 dB`, and the context closed. A failed operation also creates a
bounded failure receipt. If mandatory cleanup itself fails, the receipt says
`cleanup.status=failed`; the operator must treat the radio as unsafe until a
separate read-only inspection proves mute.

## 2026-08-14 read-only observation

The local system `iio_info`/`iio_attr` tools contacted only
`ip:192.168.1.15`, with a 5-second timeout. They observed:

| Gate | Observation |
|---|---|
| Reachability | 2/2 ICMP replies; 0% loss; 0.285 ms average RTT |
| Radio serial | `104000b29905000e17000800065934759d` |
| Firmware | `v0.38-plutoplus-spf-libiio-metadata-v5` |
| Metadata capability | `iio,buffer-metadata=1` |
| RX scan layout | `voltage0..3`, indexes 0..3, signed 12-in-16 little-endian |
| TX2 gain | `-80.000000 dB` |
| TX2 DDS scales | `altvoltage4..7` all `0.000000` |
| Host CLI / radio iiOD | libiio 0.25 / 0.25 |
| Full V5 attestation | Pass in qualified runtime image `sha256:9f2424b29f89fd73fd33a64828056f911f68355eb67950647e4c6d788ca7d766` |

The native shell has no `/opt/leo-v5/runtime-manifest.json` and cannot import
the pinned `iio`, `adi`, or `spf` Python packages. The same read-only radio
observation was therefore repeated inside the previously qualified immutable
V5 runtime image, mounting the current source read-only. Process-local runtime
and radio attestation passed for the exact standard-libiio IP transport; the
context was then closed without tune, gain, DDS, buffer, or transmit calls.

The previously described bench topology said one branch may have only 20 dB
attenuation. The safety adapter requires independently identified and verified
attenuation of at least 30 dB on each path. Exclusive radio use does not satisfy
that missing physical gate, so the 2026-08-14 dry-run using the last stated
30/20 dB topology failed validation before opening a radio context and created
no passing arm receipt. Live mutation was therefore unavailable.

Run the hardware-free safety suite with:

```text
.venv/bin/python -m pytest tests/fixtures -q
```
