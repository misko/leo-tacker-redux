from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from spikes.recording_format import format_spike as spike

pytestmark = pytest.mark.skipif(
    spike.np is None, reason="format spike needs ephemeral numpy/h5py environment"
)


def test_canonical_manifest_has_no_circular_object_digest() -> None:
    manifest = spike.manifest_for([spike.Segment("a", 4, 2)])
    encoded = spike.canonical_json_bytes(manifest)
    assert encoded == spike.canonical_json_bytes(json.loads(encoded))
    assert b"sha256" not in encoded


def test_sigmf_exact_round_trip_and_variable_segment_shapes(tmp_path: Path) -> None:
    segments = [spike.Segment("a", 37, 2), spike.Segment("b", 19, 1)]
    data_path, meta_path, _ = spike.write_sigmf(tmp_path / "recording", segments, 11)
    assert not list(tmp_path.glob("*.partial"))
    metadata = json.loads(meta_path.read_bytes())
    second_offset = segments[0].byte_count
    assert metadata["captures"][1]["leo:byte_offset"] == second_offset
    assert metadata["captures"][1]["core:sample_start"] == 37 * 2
    assert metadata["captures"][1]["leo:shape"] == [19, 1, 2]
    actual = spike.read_sigmf_slice(
        tmp_path / "recording", segments[1], second_offset, 3, 7
    )
    expected = spike.deterministic_ci16(3, 7, 1)
    assert spike.np.array_equal(actual, expected)
    assert data_path.stat().st_size == sum(segment.byte_count for segment in segments)


def test_sigmf_truncation_and_partial_are_unambiguous(tmp_path: Path) -> None:
    segment = spike.Segment("a", 20, 2)
    base = tmp_path / "interrupted"
    partial = Path(f"{base.with_suffix('.sigmf-data')}.partial")
    partial.write_bytes(b"\0" * 7)
    assert partial.name.endswith(".partial")
    partial.replace(base.with_suffix(".sigmf-data"))
    with pytest.raises(ValueError, match="truncated"):
        spike.read_sigmf_slice(base, segment, 0, 0, 2)


@pytest.mark.skipif(spike.h5py is None, reason="h5py unavailable")
def test_hdf5_exact_round_trip_chunking_and_variable_shapes(tmp_path: Path) -> None:
    segments = [spike.Segment("a", 37, 2), spike.Segment("b", 19, 1)]
    path, _ = spike.write_hdf5(tmp_path / "recording.h5", segments, 11)
    spike.validate_hdf5(path, segments)
    assert not list(tmp_path.glob("*.partial"))
    actual = spike.read_hdf5_slice(path, "a", 5, 13)
    expected = spike.deterministic_ci16(5, 13, 2)
    assert spike.np.array_equal(actual, expected)
    with spike.h5py.File(path, "r") as recording:
        assert recording["segments/a/iq"].chunks == (11, 2, 2)
        assert recording["segments/a/iq"].fletcher32


@pytest.mark.skipif(spike.h5py is None, reason="h5py unavailable")
def test_abrupt_hdf5_writer_exit_remains_identifiable_but_not_publishable(
    tmp_path: Path,
) -> None:
    partial = tmp_path / "interrupted.h5.partial"
    child = """
import os, sys
import h5py
import numpy as np
path = sys.argv[1]
recording = h5py.File(path, 'x', libver='latest')
recording.create_dataset('manifest', data=np.frombuffer(b'{\"state\":\"capturing\"}', dtype='u1'))
iq = recording.create_group('segments').create_group('a').create_dataset(
    'iq', shape=(0, 2, 2), maxshape=(None, 2, 2), chunks=(16, 2, 2),
    dtype='<i2', fletcher32=True)
iq.resize(16, axis=0)
iq[:] = np.arange(64, dtype='<i2').reshape(16, 2, 2)
recording.flush()
handle = recording.id.get_vfd_handle()
if isinstance(handle, int):
    os.fsync(handle)
os._exit(91)
"""
    completed = subprocess.run([sys.executable, "-c", child, str(partial)], check=False)
    assert completed.returncode == 91
    assert partial.name.endswith(".partial")
    # HDF5 leaves the "open for write" consistency flag set after os._exit,
    # even though data was flushed and fsynced.  Recovery requires h5clear or
    # equivalent.  Publication must therefore quarantine any .partial rather
    # than treating its readable prefix as a complete recording.
    with pytest.raises(OSError, match="already open for write"):
        spike.h5py.File(partial, "r")


@pytest.mark.skipif(spike.h5py is None, reason="h5py unavailable")
def test_hdf5_external_link_is_rejected_before_dereference(tmp_path: Path) -> None:
    path = tmp_path / "hostile.h5"
    with spike.h5py.File(path, "w") as recording:
        recording.create_dataset("manifest", data=spike.np.array([1], dtype="u1"))
        segments = recording.create_group("segments")
        segment = segments.create_group("a")
        segment["iq"] = spike.h5py.ExternalLink("missing.h5", "/iq")
    with pytest.raises(ValueError, match="links are forbidden"):
        spike.validate_hdf5(path, [spike.Segment("a", 1, 2)])


@pytest.mark.skipif(spike.h5py is None, reason="h5py unavailable")
def test_hdf5_sparse_excessive_shape_is_rejected_without_reading(
    tmp_path: Path,
) -> None:
    path = tmp_path / "oversized.h5"
    with spike.h5py.File(path, "w") as recording:
        recording.create_dataset("manifest", data=spike.np.array([1], dtype="u1"))
        segment = recording.create_group("segments").create_group("a")
        segment.create_dataset(
            "iq",
            shape=(spike.MAX_SAMPLES_PER_SEGMENT + 1, 2, 2),
            chunks=(1, 2, 2),
            dtype="<i2",
            fletcher32=True,
        )
    with pytest.raises(ValueError, match="sample count exceeds safety limit"):
        spike.validate_hdf5(
            path, [spike.Segment("a", spike.MAX_SAMPLES_PER_SEGMENT + 1, 2)]
        )
