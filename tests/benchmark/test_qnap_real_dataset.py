from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmark.qnap_real_dataset import (
    DEFAULT_MANIFEST,
    DEFAULT_ROOT,
    RealDatasetError,
    load_dataset_spec,
    run_detector_audit,
    verify_references,
)


def test_frozen_manifest_is_one_group_unlabeled_and_bounded() -> None:
    spec = load_dataset_spec()

    assert str(spec.digest) == (
        "sha256:e6f3b5e35c14333d1491cf1b7ec28b8597f560f11c7b604a54a9d93d026a5259"
    )
    assert len(spec.members) == 6
    assert {member["partition"] for member in spec.members} == {"development"}
    assert len({member["split_group_id"] for member in spec.members}) == 1
    assert len({member["sweep_pair_id"] for member in spec.members}) == 3
    assert all(member["truth"]["target_present"] is None for member in spec.members)
    assert all(member["truth"]["accuracy_eligible"] is False for member in spec.members)
    assert spec.raw["selection"]["detector_window_count"] == 192
    assert spec.raw["selection"]["analyzed_iq_bytes"] == 6_291_456
    assert spec.raw["selection"]["referenced_iq_bytes"] == 76_800_000


def test_membership_mutation_is_rejected(tmp_path: Path) -> None:
    document = json.loads(DEFAULT_MANIFEST.read_bytes())
    document["members"][0]["partition"] = "train"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RealDatasetError, match="membership digest"):
        load_dataset_spec(changed)


def test_threshold_sources_are_content_addressed() -> None:
    spec = load_dataset_spec()
    repository_root = DEFAULT_MANIFEST.parents[2]

    for ref in spec.raw["analysis_profile"]["reference_thresholds"]["source_refs"]:
        payload = (repository_root / ref["relative_path"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == ref["sha256"]


@pytest.mark.integration
def test_mounted_subset_hashes_and_detector_summary() -> None:
    if not DEFAULT_ROOT.is_dir():
        pytest.skip("read-only QNAP corpus is not mounted")
    spec = load_dataset_spec()

    verification = verify_references(spec, DEFAULT_ROOT, verify_full_iq=True)
    report = run_detector_audit(spec, DEFAULT_ROOT)

    assert verification["full_iq_bytes_hashed"] == 76_800_000
    assert report["scope"]["selected_window_attempt_count"] == 192
    assert report["scope"]["accepted_window_count_by_method"] == {
        "coarse-energy@0.1.0": 123,
        "paired-common-mode@0.1.0": 123,
        "periodic-coherence@0.1.0": 123,
    }
    assert report["firings"]["coarse-energy@0.1.0"]["count"] == 5
    assert report["firings"]["paired-common-mode@0.1.0"]["count"] == 0
    assert report["firings"]["periodic-coherence@0.1.0"]["count"] == 33
    direct = report["cross_radio_direct_replication"]["by_method"]
    assert direct["coarse-energy@0.1.0"]["compared_count"] == 17
    assert direct["periodic-coherence@0.1.0"]["both_fire"] == 1
