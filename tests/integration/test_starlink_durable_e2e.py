from __future__ import annotations

import ast
import json
from pathlib import Path

import benchmark.starlink_durable_e2e as durable_e2e
from benchmark.starlink_durable_e2e import run_harness
from leo_flow.contracts.core import Digest, canonical_json_bytes
from leo_flow.storage import FileSystemBlobStore


def test_runtime_harness_has_no_test_or_live_adapter_imports() -> None:
    path = Path(durable_e2e.__file__ or "")
    imports: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.append(node.module)

    assert not any(name == "tests" or name.startswith("tests.") for name in imports)
    assert not any("drivers.pluto" in name for name in imports)
    assert not any("qualification" in name for name in imports)


def test_durable_offline_harness_is_reproducible_bounded_and_fail_closed(
    tmp_path: Path,
) -> None:
    first = run_harness(tmp_path / "first")
    second = run_harness(tmp_path / "second")

    assert first.report_bytes == second.report_bytes
    assert first.report_object == second.report_object
    assert first.report_object.digest == Digest.sha256(first.report_bytes)
    assert first.report["result_digest"] == Digest.sha256(
        canonical_json_bytes(first.report["result"])
    )
    document = json.loads(first.report_bytes)
    result = document["result"]
    assert result["offline_only"] is True
    assert result["observed"]["case_count"] == 6
    assert result["observed"]["segment_count"] == 48
    assert result["observed"]["detector_window_count"] == 48
    assert result["observed"]["generated_iq_bytes"] == 1_572_864
    assert (
        result["observed"]["durable_artifact_bytes"]
        <= result["bounds"]["max_durable_artifact_bytes"]
    )
    groups = result["split_policy"]["groups"]
    assert groups == {
        "train": ["group_train_101"],
        "validation": ["group_validation_202"],
        "locked_test": ["group_locked_303"],
    }
    assert len({group for values in groups.values() for group in values}) == 3
    assert result["split_policy"]["threshold_calibration_split"] == "train"
    assert [item["kind"] for item in result["failure_injections"]] == [
        "truncation",
        "missing_frame",
    ]
    assert all(
        item["outcome"] == "rejected"
        and item["spool_state"] == "failed"
        and item["durable_recording_published"] is False
        and item["partial_artifact_retained"] is False
        for item in result["failure_injections"]
    )
    assert [item["error_type"] for item in result["failure_injections"]] == [
        "SampleCountError",
        "ContinuityError",
    ]
    assert len(result["artifact_identities"]["recordings"]) == 6
    assert len(result["artifact_identities"]["feature_sets"]) == 6
    assert len(result["handoff_receipts"]) == 6
    assert all(
        receipt["spool_state"] == "cleaned"
        and receipt["cas_verified_after_reader_reconstruction"] is True
        and receipt["manifest_digest"]["algorithm"] == "sha256"
        and receipt["data_object"]["digest"]["algorithm"] == "sha256"
        and receipt["metadata_object"]["digest"]["algorithm"] == "sha256"
        for receipt in result["handoff_receipts"]
    )
    assert result["detector_evaluation"]["threshold_calibration_split"] == "train"
    assert (
        result["detector_evaluation"]["overall_association"]["union_window_count"] == 48
    )

    with FileSystemBlobStore(tmp_path / "first" / "cas").open(
        first.report_object
    ) as stream:
        assert stream.read() == first.report_bytes
    assert not tuple((tmp_path / "first").rglob("*.partial"))
    assert not tuple((tmp_path / "first" / "capture").rglob("recording.data"))
