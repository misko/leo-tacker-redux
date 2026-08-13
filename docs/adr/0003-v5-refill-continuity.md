# ADR 0003: V5 refill continuity is a first-class recording fact

Status: accepted for the Wave 4 implementation; target-hardware gates remain.

## Context

The target radio platform is custom firmware
`v0.38-plutoplus-spf-libiio-metadata-v5` at source commit
`d7c87a9a28094ee6f0b23cb47df9ff737b5a69d8`. It keeps the ordinary paired
CS16 layout but can associate each requested IIO frame with a stream identity,
capture-buffer sequence, FPGA sample sequence and time fit, gain/RSSI endpoints,
gain observations, and failure/overflow flags. At a rate above transport
capacity, received buffers can be individually valid while separated in radio
time. Concatenating them without preserving this evidence would create a false
continuous recording.

## Decision

The domain uses immutable normalized contracts in `contracts.continuity`. They
contain no pyadi, libiio, or SPF wire types. A radio adapter emits IQ bytes and
an optional `RefillMetadata` together. The capture engine constructs a
`SegmentContinuity`, whose validator rejects capture-sequence gaps, hardware
sample-sequence gaps, IQ-offset gaps/overlap, stream changes, time overlap, and
all overflow/read-failure flags.

`REQUIRE_VERIFIED` is the default Pluto policy. Missing metadata fails before a
recording is published. An explicitly configured `ALLOW_UNVERIFIED` ordinary
buffer fallback may store IQ, but its segment is labeled `unverified` and its
capability says `ordinary-buffer;continuity-unverified`; it never fabricates
refill facts.

The selected SigMF pair advances only the metadata namespace to 1.1. Raw IQ
remains a contiguous headerless paired CI16 object with layout
`[sample, receiver, component]`. A canonical `continuity` table lives in the
metadata object and is authenticated by that object's CAS digest. Metadata
bytes are never inserted into the IQ stream. Readers retain support for legacy
1.0 metadata, which returns no continuity record.

The patched-host boundary is an injected `MetadataReader` in the Pluto adapter.
It returns ordinary native IQ plus the normalized contract. This keeps the core
independent of private pyadi/libiio objects and permits deterministic fakes.
The deployment adapter that parses SPF protocol-v3 bytes belongs in the host
integration package/environment containing the pinned patched libiio, not in
the dependency-free core.

Verified segments currently require a sample count that is an exact multiple
of the IIO block size. V5 endpoint observations describe the entire hardware
refill; silently truncating a final refill would falsely associate its endpoint
metadata with the stored prefix.

## Consequences

- Analysis can distinguish verified radio-time continuity from mere byte
  adjacency and can locate every refill in both stored-sample and radio-sample
  coordinates.
- Firmware, source commit, host libiio line, metadata protocol, and advertised
  capability travel with each segment's evidence.
- Legacy objects remain readable but cannot be promoted to verified continuity.
- Capture aborts atomically on a late gap or overflow; partial IQ and metadata
  never enter CAS/catalog publication.
- A target-host bridge must translate the pinned SPF V3 parser/time fit into
  these contracts and pass hardware acceptance before deployment.

## Target-hardware acceptance gates

1. Verify firmware release and commit identity plus `iio,buffer-metadata=1`.
2. Verify patched host libiio 0.25 (`c26258b…`) or qualified 0.26
   (`d5695c3…`) and record the exact version.
3. Demonstrate byte-exact paired `[I0,Q0,I1,Q1]` CI16 association for every
   metadata refill at narrow and wide configured modes.
4. Inject/observe a sequence gap, device overflow, gain-observation overflow,
   and missing metadata; each must abort publication.
5. Run rate and slow-host tests and prove all production "contiguous" rates
   maintain complete capture- and sample-sequence continuity.
6. Validate UTC/monotonic mapping and uncertainty across retunes and a soak.
7. Exercise disconnect, SIGTERM, process kill, restart, and local-spool
   reconciliation; no partial pair may become publishable.
