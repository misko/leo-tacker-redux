# Recording format spike: choose a SigMF pair for v1

Status: Wave 0 decision recommendation, 2026-08-13.

## Decision

Use one contiguous `.sigmf-data` file plus one canonical `.sigmf-meta` file for
each v1 recording. Keep the physical format behind `RecordingWriter` and
`RecordingObjectReader`; neither public contracts nor analysis algorithms may
depend on paths or SigMF implementation types.

HDF5 is technically workable, but it does not pass the plan's explicit gate
that direct slices be at least as fast as raw CI16 chunks. It also adds a
stateful native container, a hostile-file validation surface, and a repair-tool
requirement for inspecting a file whose writer exits abruptly. Those costs do
not buy enough at this experimental stage. A two-object recording is still a
large simplification over the current manifest, survey, and many-chunk tree.

This decision is deliberately reversible at the codec port. Do not implement
both production codecs in v1. Revisit HDF5 only when a measured need for
heterogeneous arrays in one object outweighs the simpler raw-data path.

## Required v1 physical layout

| Object | Contents | Authority |
|---|---|---|
| `<digest>.sigmf-data` | Contiguous little-endian complex-int16 words, sample-major then receiver-major | Exact acquired IQ |
| `<digest>.sigmf-meta` | Canonical UTF-8 JSON with recording manifest, segment byte ranges, shapes, units, and SigMF fields | Exact acquisition facts and byte interpretation |
| PostgreSQL recording row | Both `ObjectRef`s, canonical manifest digest, lifecycle state | Atomic visibility and discovery |

The data object contains no header. For each segment, the metadata must record
`byte_offset`, `byte_count`, `[sample, receiver, component]` shape, receiver
chain order, sample count, dtype `<i2`, sample rate, center frequency, bandwidth,
and timing. `core:datatype` is `ci16_le`. Receiver interleaving and variable
segment shapes use a versioned `leo:` namespace because a global SigMF datatype
does not by itself describe changing receiver dimensions.

The canonical embedded manifest never contains either object's own digest.
Publication computes SHA-256 and byte count externally and records them in the
two `ObjectRef`s. The metadata object's digest also authenticates the exact
manifest bytes. The scientific recording becomes visible only after both blobs
are durable and one database transaction installs both references.

## Measured comparison

The repeatable implementation is
`spikes/recording_format/format_spike.py`. It generated deterministic
dual-receiver CI16 arrays with layout `[sample, receiver, I/Q]`, wrote one GiB,
called `fsync`, read bounded slices, and verified exact values and receiver
order. Runs used local ext4 on Kalman, Python 3.11.14, NumPy 2.4.6, h5py 3.16.0,
and HDF5 2.0.0. This is a format comparison, not a Pi or radio acceptance test.

### One-GiB workload, 2 MiB writer chunks

| Measurement | SigMF pair | HDF5 | Interpretation |
|---|---:|---:|---|
| Sustained write, median of 3 | 154.3 MiB/s | 152.6 MiB/s | Equivalent median on this host |
| Sustained write, observed range | 29.4–159.3 MiB/s | 148.1–160.2 MiB/s | One raw-file `fsync` outlier shows host/storage contention matters |
| 2 MiB direct slice, median | 0.101 ms | 1.224 ms | HDF5 was about 12× slower, though both are small in absolute terms |
| Stored bytes for 1 GiB payload | 1,073,741,824 + 511 metadata | 1,073,763,111 | HDF5 overhead was 21,287 bytes with 2 MiB chunks |
| Exact CI16 and receiver order | Pass | Pass | Byte/value correctness is not a discriminator |

A separate like-for-like read used a 95.37 MiB slice, corresponding closely to
one current 100,000,000-byte CI16 chunk, while retaining 2 MiB HDF5 chunks.
SigMF took 36.16 ms and HDF5 took 49.75 ms. HDF5 was 1.38× slower. That misses
the written acceptance criterion even though the absolute rate is adequate for
offline analysis.

A 95.37 MiB HDF5 chunk was also tested and is not recommended: the partial
final chunk caused roughly 26 MiB of extra allocation, slice latency was
127.48 ms, and write throughput was 91.8 MiB/s. Small bounded chunks avoid that
allocation behavior.

Do not interpret these cached-read numbers as QNAP performance. The analysis
reader needs a scheduled read-only NFS test using the eventual blob adapter.

## Functional findings

| Concern | SigMF pair | HDF5 |
|---|---|---|
| Multiple segments | Byte ranges in metadata | Native group per segment |
| Variable receiver shape | Explicit versioned `leo:shape`; requires our codec | Native dataset shape |
| Bounded reads | One seek/read or mmap slice | Native dataset slice; chunk-aware |
| Integrity | SHA-256 for both objects | SHA-256 for file plus Fletcher32 chunks |
| Publication | Two blobs, one database transaction | One blob, one database transaction |
| Interrupted write | `.partial` bytes are transparent and length-checkable | `.partial` is identifiable; abrupt exit sets an HDF5 consistency flag |
| Native dependencies | None beyond the existing numeric reader | h5py and bundled HDF5 native libraries |
| Hostile input surface | Bounds and exact-length checks | Must reject links, aliases/cycles, wrong types/shapes, huge sparse datasets, and unsafe filters |
| Offline inspection | Standard file tools can inspect the payload | Requires a compatible HDF5 stack |

The abrupt-exit test wrote and flushed an HDF5 prefix, called `fsync`, then used
`os._exit`. Reopening failed with "file is already open for write" and suggested
`h5clear`. This satisfies only the plan's *safely quarantinable* branch, not
recoverability. Capture must never promote any `.partial` file, regardless of
whether a tool can salvage it.

The spike validator rejects external and soft links before dereferencing them,
caps object and segment counts, checks expected shapes before reads, and rejects
an enormous sparse dataset without materializing it. These defenses are
feasible but are complexity that the raw format avoids.

## Dependency finding

On x86-64, the ephemeral wheels occupied about 54.7 MiB for NumPy and another
15.0 MiB for h5py; h5py bundled HDF5, HDF5-HL, szip, and libaec native libraries.
The legacy capture environment already needs NumPy. An `uv` dry run found h5py
and NumPy wheels for Python 3.11 on `aarch64-manylinux_2_28`, but that does not
prove compatibility with the actual Pi architecture, OS image, or deployment
method. The Pi was not accessed during this spike.

## Acceptance gates for the chosen SigMF path

Run these on the actual Pi, local NVMe, and radio before freezing codec v1:

| Gate | Pass condition |
|---|---|
| Narrow capture | Two receivers at 2.5 MS/s for 120 s; exact expected bytes and zero driver/sample drops |
| Wide capture | Two receivers at 10 MS/s for at least 10 s; zero driver/sample drops and writer backlog remains bounded |
| Sustained soak | Existing scan/dwell schedule for one hour; no missed deadlines attributable to write or `fsync` |
| Block sizing | Compare 2, 8, and 32 MiB application writes; select the smallest size that sustains peak input with 2× measured headroom |
| Interruption | Kill before data close, after data close, and between the two blob uploads; no incomplete recording becomes visible |
| Restart | Spool reconciliation either resumes publication of two verified complete blobs or quarantines capture; it never appends to a published object |
| Exactness | Independent reader verifies byte count, segment ranges, receiver order, first/last samples, and full SHA-256 |
| NFS publication | Both content-addressed blobs verify remotely before the database transaction; readers observe neither or both |
| Disk pressure | Capture stops or evicts only acknowledged data at declared watermarks; no unacknowledged recording is deleted |

The peak source payload is 80,000,000 bytes/s for two receivers at 10 MS/s
(`10e6 × 2 receivers × I/Q × int16`). The desired local writer result is at
least 160 MB/s sustained to retain 2× headroom. If the Pi cannot meet that with
plain contiguous writes, HDF5 will not solve the storage bottleneck; fix block
sizing, NVMe behavior, or capture cadence instead.

## Component tests required after the spike

The production codec must have independent writer and reader fixtures. Do not
construct every reader fixture with the production writer.

1. Golden byte fixtures for one and two receivers, multiple segments, partial
   final blocks, negative/full-scale CI16 values, and receiver dropout.
2. Property tests for segment offsets, non-overlap, exact total length, shape
   products, integer overflow, and malformed canonical JSON.
3. Reader rejection of path traversal, duplicate segment IDs, overlapping or
   out-of-bounds ranges, unknown required namespace versions, wrong dtype,
   impossible receiver counts, truncation, trailing bytes, and digest mismatch.
4. Publication fault tests at every data/meta/blob/database boundary, including
   retry and garbage collection races.
5. Cross-platform fixtures proving little-endian interpretation explicitly.
6. A scheduled NFS test proving bounded slices and read-only credentials without
   directory discovery.

## Commands

```bash
uv run --no-project --with 'pytest>=8' --with numpy --with h5py \
  python -m pytest -q tests/format_spike

uv run --no-project --with numpy --with h5py \
  python -m spikes.recording_format.format_spike \
  --total-mib 1024 --block-samples 262144 --slice-repeats 15

uv run --no-project --with numpy --with h5py \
  python -m spikes.recording_format.format_spike \
  --total-mib 1024 --block-samples 262144 \
  --slice-samples 12500000 --slice-repeats 5
```

Measured values are evidence for this decision, not permanent CI thresholds.
The small conformance and hostile-input tests are suitable for CI; performance
and radio-drop gates belong on controlled hardware runners.
