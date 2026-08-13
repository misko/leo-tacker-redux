"""Repeatable HDF5-versus-SigMF recording-format experiment.

The benchmark writes the same deterministic dual-receiver, little-endian CI16
samples through both formats.  HDF5 is optional so this file can still exercise
the SigMF path in the dependency-free project environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # Deliberately optional: the project does not depend on these packages.
    import h5py  # type: ignore[import-not-found]
    import numpy as np  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised by the dependency-free env
    h5py = None
    np = None


MAX_SEGMENTS = 4_096
MAX_RECEIVERS = 16
MAX_SAMPLES_PER_SEGMENT = 10_000_000_000
COMPONENTS = 2
CI16_BYTES = 2


@dataclass(frozen=True)
class Segment:
    segment_id: str
    sample_count: int
    receivers: int

    @property
    def byte_count(self) -> int:
        return self.sample_count * self.receivers * COMPONENTS * CI16_BYTES


def canonical_json_bytes(value: object) -> bytes:
    """Return the exact JSON representation embedded in either candidate."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest_file(path: Path, block_bytes: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(block_bytes):
            digest.update(block)
    return digest.hexdigest()


def require_numpy() -> None:
    if np is None:
        raise RuntimeError("sample operations require numpy")


def require_hdf5() -> None:
    require_numpy()
    if h5py is None:
        raise RuntimeError("HDF5 operations require h5py")


def deterministic_ci16(start: int, samples: int, receivers: int) -> Any:
    """Generate exact, receiver-distinguishable ``[sample, receiver, IQ]`` data."""

    if np is None:
        raise RuntimeError("sample generation requires numpy")
    sample = np.arange(start, start + samples, dtype=np.int64)[:, None, None]
    receiver = np.arange(receivers, dtype=np.int64)[None, :, None]
    component = np.arange(COMPONENTS, dtype=np.int64)[None, None, :]
    values = (sample * 31 + receiver * 997 + component * 7919) % 65_521 - 32_760
    return values.astype("<i2")


def iter_blocks(segment: Segment, block_samples: int) -> Iterator[Any]:
    for start in range(0, segment.sample_count, block_samples):
        count = min(block_samples, segment.sample_count - start)
        yield deterministic_ci16(start, count, segment.receivers)


def manifest_for(
    segments: Sequence[Segment], state: str = "complete"
) -> dict[str, Any]:
    """Construct spike metadata without a circular whole-object digest."""

    return {
        "schema_version": "spike.recording/v1",
        "recording_id": "01J00000000000000000000000",
        "state": state,
        "sample_layout": ["sample", "receiver", "component"],
        "dtype": "int16 little-endian",
        "segments": [
            {
                "segment_id": item.segment_id,
                "sample_count": item.sample_count,
                "shape": [item.sample_count, item.receivers, COMPONENTS],
            }
            for item in segments
        ],
    }


def write_sigmf(
    base: Path, segments: Sequence[Segment], block_samples: int, *, sync: bool = True
) -> tuple[Path, Path, float]:
    """Write one contiguous SigMF data object and one canonical metadata object.

    Different receiver counts require a ``leo:shape`` extension because SigMF's
    global datatype alone cannot express a changing interleave width.
    """

    require_numpy()
    data_path = base.with_suffix(".sigmf-data")
    meta_path = base.with_suffix(".sigmf-meta")
    partial_data = Path(f"{data_path}.partial")
    partial_meta = Path(f"{meta_path}.partial")
    manifest = manifest_for(segments)
    offsets: list[dict[str, Any]] = []
    byte_offset = 0
    started = time.perf_counter()
    with partial_data.open("xb", buffering=0) as output:
        for segment in segments:
            offsets.append(
                {
                    # SigMF indexes flattened complex samples. ``leo:shape``
                    # restores the simultaneous receiver dimension.
                    "core:sample_start": byte_offset // (COMPONENTS * CI16_BYTES),
                    "leo:byte_offset": byte_offset,
                    "leo:byte_count": segment.byte_count,
                    "leo:segment_id": segment.segment_id,
                    "leo:shape": [segment.sample_count, segment.receivers, COMPONENTS],
                }
            )
            for block in iter_blocks(segment, block_samples):
                encoded = block.tobytes(order="C")
                output.write(encoded)
                byte_offset += len(encoded)
        if sync:
            os.fsync(output.fileno())
    meta = {
        "global": {
            "core:datatype": "ci16_le",
            "core:version": "1.1.0",
            "leo:manifest": manifest,
        },
        "captures": offsets,
        "annotations": [],
    }
    with partial_meta.open("xb") as output:
        output.write(canonical_json_bytes(meta))
        output.flush()
        if sync:
            os.fsync(output.fileno())
    os.replace(partial_data, data_path)
    os.replace(partial_meta, meta_path)
    return data_path, meta_path, time.perf_counter() - started


def read_sigmf_slice(
    base: Path, segment: Segment, byte_offset: int, start: int, count: int
) -> Any:
    require_numpy()
    frame_bytes = segment.receivers * COMPONENTS * CI16_BYTES
    with base.with_suffix(".sigmf-data").open("rb", buffering=0) as stream:
        stream.seek(byte_offset + start * frame_bytes)
        encoded = stream.read(count * frame_bytes)
    if len(encoded) != count * frame_bytes:
        raise ValueError("truncated SigMF slice")
    return np.frombuffer(encoded, dtype="<i2").reshape(
        count, segment.receivers, COMPONENTS
    )


def write_hdf5(
    path: Path, segments: Sequence[Segment], block_samples: int, *, sync: bool = True
) -> tuple[Path, float]:
    """Append chunked IQ datasets and atomically remove the partial suffix."""

    require_hdf5()
    partial = Path(f"{path}.partial")
    manifest_bytes = canonical_json_bytes(manifest_for(segments))
    started = time.perf_counter()
    with h5py.File(partial, "x", libver="latest") as recording:
        recording.create_dataset(
            "manifest", data=np.frombuffer(manifest_bytes, dtype="u1")
        )
        segment_root = recording.create_group("segments")
        for segment in segments:
            group = segment_root.create_group(segment.segment_id)
            chunk_rows = min(block_samples, max(segment.sample_count, 1))
            iq = group.create_dataset(
                "iq",
                shape=(0, segment.receivers, COMPONENTS),
                maxshape=(None, segment.receivers, COMPONENTS),
                chunks=(chunk_rows, segment.receivers, COMPONENTS),
                dtype="<i2",
                fletcher32=True,
            )
            written = 0
            for block in iter_blocks(segment, block_samples):
                next_written = written + block.shape[0]
                iq.resize(next_written, axis=0)
                iq[written:next_written, :, :] = block
                written = next_written
            group.attrs["sample_count"] = segment.sample_count
        recording.flush()
        if sync:
            handle = recording.id.get_vfd_handle()
            if isinstance(handle, int):
                os.fsync(handle)
    os.replace(partial, path)
    return path, time.perf_counter() - started


def validate_hdf5(path: Path, expected: Sequence[Segment]) -> None:
    """Validate structure and bounds without materializing IQ arrays.

    Link inspection happens before dereferencing objects.  Expected manifest
    bounds prevent a tiny sparse HDF5 file from requesting an enormous read.
    """

    require_hdf5()
    if len(expected) > MAX_SEGMENTS:
        raise ValueError("too many expected segments")
    expected_by_id = {item.segment_id: item for item in expected}
    with h5py.File(path, "r") as recording:
        visited_groups: set[int] = {hash(recording.id)}
        visited_objects = 1

        def reject_links(group: Any, prefix: str = "") -> None:
            nonlocal visited_objects
            # h5py's ``visit`` follows objects and silently omits a dangling
            # external link. Inspect each link entry before any dereference.
            for child_name in group:
                visited_objects += 1
                if visited_objects > MAX_SEGMENTS * 4 + 2:
                    raise ValueError("too many HDF5 objects")
                qualified = f"{prefix}/{child_name}" if prefix else child_name
                link = group.get(child_name, getlink=True)
                if isinstance(link, (h5py.ExternalLink, h5py.SoftLink)):
                    raise ValueError(f"links are forbidden: {qualified}")  # noqa: TRY004
                child = group[child_name]
                if isinstance(child, h5py.Group):
                    identity = hash(child.id)
                    if identity in visited_groups:
                        raise ValueError(
                            f"group cycles or aliases are forbidden: {qualified}"
                        )
                    visited_groups.add(identity)
                    reject_links(child, qualified)

        reject_links(recording)
        if set(recording.keys()) != {"manifest", "segments"}:
            raise ValueError("unexpected top-level HDF5 objects")
        if set(recording["segments"].keys()) != set(expected_by_id):
            raise ValueError("segment set differs from manifest")
        for segment_id, expected_segment in expected_by_id.items():
            if expected_segment.receivers > MAX_RECEIVERS:
                raise ValueError("receiver count exceeds safety limit")
            if expected_segment.sample_count > MAX_SAMPLES_PER_SEGMENT:
                raise ValueError("sample count exceeds safety limit")
            group = recording[f"segments/{segment_id}"]
            if set(group.keys()) != {"iq"}:
                raise ValueError("unexpected segment objects")
            iq = group["iq"]
            wanted = (
                expected_segment.sample_count,
                expected_segment.receivers,
                COMPONENTS,
            )
            if iq.shape != wanted or iq.dtype != np.dtype("<i2"):
                raise ValueError("IQ shape or dtype differs from manifest")
            if iq.chunks is None:
                raise ValueError("IQ dataset must be chunked")
            if not iq.fletcher32:
                raise ValueError("IQ dataset must use Fletcher32")


def read_hdf5_slice(path: Path, segment_id: str, start: int, count: int) -> Any:
    require_hdf5()
    with h5py.File(path, "r") as recording:
        return recording[f"segments/{segment_id}/iq"][start : start + count, :, :]


def _slice_timings(operation: Any, repeats: int) -> list[float]:
    timings: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        result = operation()
        if np is not None:
            _ = int(result[0, 0, 0])
        timings.append(time.perf_counter() - started)
    return timings


def benchmark(
    output_dir: Path,
    total_mib: int,
    block_samples: int,
    repeats: int,
    slice_samples: int | None = None,
) -> dict[str, Any]:
    require_hdf5()
    output_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = total_mib * 1024 * 1024
    sample_count = max(1, total_bytes // (2 * COMPONENTS * CI16_BYTES))
    segment = Segment("segment-000", sample_count, 2)
    effective_mib = segment.byte_count / 1024**2

    sigmf_base = output_dir / "benchmark"
    sigmf_data, sigmf_meta, sigmf_seconds = write_sigmf(
        sigmf_base, [segment], block_samples
    )
    hdf5_path, hdf5_seconds = write_hdf5(
        output_dir / "benchmark.h5", [segment], block_samples
    )
    validate_hdf5(hdf5_path, [segment])

    slice_count = min(slice_samples or block_samples, sample_count)
    slice_start = max(0, sample_count // 2 - slice_count // 2)
    sigmf_slices = _slice_timings(
        lambda: read_sigmf_slice(sigmf_base, segment, 0, slice_start, slice_count),
        repeats,
    )
    hdf5_slices = _slice_timings(
        lambda: read_hdf5_slice(
            hdf5_path, segment.segment_id, slice_start, slice_count
        ),
        repeats,
    )
    expected = deterministic_ci16(slice_start, slice_count, 2)
    sigmf_exact = bool(
        np.array_equal(
            read_sigmf_slice(sigmf_base, segment, 0, slice_start, slice_count), expected
        )
    )
    hdf5_exact = bool(
        np.array_equal(
            read_hdf5_slice(hdf5_path, segment.segment_id, slice_start, slice_count),
            expected,
        )
    )
    return {
        "environment": {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "h5py": h5py.__version__,
            "hdf5": h5py.version.hdf5_version,
            "filesystem": "caller-selected; record findmnt separately",
        },
        "workload": {
            "mib": effective_mib,
            "sample_count": sample_count,
            "receivers": 2,
            "components": COMPONENTS,
            "block_samples": block_samples,
            "block_mib": block_samples * 2 * COMPONENTS * CI16_BYTES / 1024**2,
            "slice_mib": slice_count * 2 * COMPONENTS * CI16_BYTES / 1024**2,
            "slice_repeats": repeats,
        },
        "sigmf": {
            "write_seconds": sigmf_seconds,
            "write_mib_s": effective_mib / sigmf_seconds,
            "slice_median_ms": statistics.median(sigmf_slices) * 1000,
            "exact": sigmf_exact,
            "data_bytes": sigmf_data.stat().st_size,
            "meta_bytes": sigmf_meta.stat().st_size,
            "sha256_data": digest_file(sigmf_data),
            "sha256_meta": digest_file(sigmf_meta),
        },
        "hdf5": {
            "write_seconds": hdf5_seconds,
            "write_mib_s": effective_mib / hdf5_seconds,
            "slice_median_ms": statistics.median(hdf5_slices) * 1000,
            "exact": hdf5_exact,
            "bytes": hdf5_path.stat().st_size,
            "sha256": digest_file(hdf5_path),
        },
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--total-mib", type=int, default=256)
    parser.add_argument("--block-samples", type=int, default=262_144)
    parser.add_argument("--slice-samples", type=int)
    parser.add_argument("--slice-repeats", type=int, default=9)
    args = parser.parse_args(argv)
    if (
        args.total_mib <= 0
        or args.block_samples <= 0
        or args.slice_repeats <= 0
        or (args.slice_samples is not None and args.slice_samples <= 0)
    ):
        parser.error("sizes and repeat count must be positive")
    if h5py is None or np is None:
        parser.error(
            "install the ephemeral spike environment: uv run --with numpy --with h5py ..."
        )
    if args.output_dir is None:
        with tempfile.TemporaryDirectory(prefix="leo-format-spike-") as temporary:
            result = benchmark(
                Path(temporary),
                args.total_mib,
                args.block_samples,
                args.slice_repeats,
                args.slice_samples,
            )
    else:
        result = benchmark(
            args.output_dir,
            args.total_mib,
            args.block_samples,
            args.slice_repeats,
            args.slice_samples,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
