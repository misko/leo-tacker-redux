from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.contracts.core import (
    CaptureBatchId,
    Digest,
    JobId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.focused_analysis_completion import (
    FocusedAnalysisCompletionV0_1,
    decode_focused_analysis_completion,
    encode_focused_analysis_completion,
)
from leo_flow.deployments.gauss_focused_analysis_operator import _write_completion


def _receipt() -> FocusedAnalysisCompletionV0_1:
    return FocusedAnalysisCompletionV0_1(
        SchemaRef(FocusedAnalysisCompletionV0_1.SCHEMA_ID),
        CaptureBatchId("cbatch_focused_test"),
        Digest.sha256(b"definition"),
        (RecordingId("rec_a"), RecordingId("rec_b")),
        (Digest.sha256(b"recording-a"), Digest.sha256(b"recording-b")),
        (JobId("job_a"), JobId("job_b")),
        (Digest.sha256(b"result-a"), Digest.sha256(b"result-b")),
        UtcNs(123),
    )


def test_completion_codec_is_canonical_and_exact() -> None:
    receipt = _receipt()
    payload = encode_focused_analysis_completion(receipt)
    assert decode_focused_analysis_completion(payload) == receipt
    assert payload.endswith(b"\n")


def test_completion_codec_rejects_mutation_and_noncanonical_bytes() -> None:
    value = json.loads(encode_focused_analysis_completion(_receipt()))
    value["recording_ids"].reverse()
    mutated = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    with pytest.raises(ValueError):
        decode_focused_analysis_completion(mutated)
    with pytest.raises(ValueError, match="not canonical"):
        decode_focused_analysis_completion(json.dumps(value, indent=2).encode() + b"\n")


def test_completion_receipt_publish_is_immutable_exact_replay(tmp_path: Path) -> None:
    path = tmp_path / "completion.json"
    receipt = _receipt()
    _write_completion(path, receipt)
    _write_completion(path, receipt)
    assert decode_focused_analysis_completion(path.read_bytes()) == receipt
    with pytest.raises(RuntimeError, match="identity conflict"):
        _write_completion(path, replace(receipt, completed_utc_ns=UtcNs(124)))
