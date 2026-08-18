"""Periodic read-only regression canary for one frozen historical QAM corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, cast

import numpy as np

from leo_flow.analysis.recording.api import AnalysisExecutionContext
from leo_flow.analysis.recording.starlink_retro_qam_canary import (
    RetroQamCanaryInputV0_1,
    RetroQamCombinedExpectationV0_1,
    RetroQamReceiverExpectationV0_1,
    analyze_starlink_retro_qam_canary_v0_1,
)
from leo_flow.contracts.core import (
    Digest,
    DigestAlgorithm,
    UtcNs,
    canonical_json_bytes,
)

MAXIMUM_MANIFEST_BYTES = 64 * 1024
EXPECTED_SCHEMA = "org.leo-flow.external-retro-qam-corpus/v1"


def _sha256(path: Path) -> Digest:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return Digest(DigestAlgorithm.SHA256, digest.hexdigest())


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _load_request(path: Path) -> RetroQamCanaryInputV0_1:
    manifest_bytes = path.read_bytes()
    if not manifest_bytes or len(manifest_bytes) > MAXIMUM_MANIFEST_BYTES:
        raise ValueError("retro-QAM corpus manifest size is invalid")
    document = _mapping(json.loads(manifest_bytes), "corpus manifest")
    if document.get("schema") != EXPECTED_SCHEMA:
        raise ValueError("unsupported retro-QAM corpus manifest")
    archive = _mapping(document.get("archive"), "archive")
    root = Path(archive["root"])
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("retro-QAM archive root must be absolute and non-symlink")
    iq = _mapping(document.get("iq_object"), "IQ object")
    window = _mapping(document.get("selected_window"), "selected window")
    clip = root / str(iq["relative_path"])
    expected_iq_digest = Digest(DigestAlgorithm.SHA256, str(iq["sha256"]))
    if clip.stat().st_size != int(iq["byte_count"]):
        raise ValueError("retro-QAM IQ object size differs")
    actual_iq_digest = _sha256(clip)
    if actual_iq_digest != expected_iq_digest:
        raise ValueError("retro-QAM IQ object digest differs")
    byte_offset = int(window["byte_offset"])
    byte_count = int(window["byte_count"])
    with clip.open("rb") as stream:
        stream.seek(byte_offset)
        selected = stream.read(byte_count)
    if len(selected) != byte_count:
        raise ValueError("retro-QAM selected window is truncated")
    selected_digest = Digest.sha256(selected)
    if selected_digest != Digest(DigestAlgorithm.SHA256, str(window["sha256"])):
        raise ValueError("retro-QAM selected window digest differs")
    fmt = _mapping(document.get("format"), "format")
    if (
        fmt.get("component_dtype") != "int16"
        or fmt.get("byte_order") != "little"
        or fmt.get("receiver_count") != 2
        or fmt.get("layout") != ["sample", "receiver", "component"]
        or fmt.get("component_order") != ["i", "q"]
    ):
        raise ValueError("unsupported retro-QAM CI16 geometry")
    sample_count = int(window["sample_count"])
    raw = np.frombuffer(selected, dtype="<i2").reshape(sample_count, 2, 2)
    samples = tuple(
        np.asarray(
            (raw[:, receiver, 0].astype(np.float32) + 1j * raw[:, receiver, 1])
            / 32768.0,
            dtype=np.complex64,
        )
        for receiver in range(2)
    )
    expected = tuple(
        RetroQamReceiverExpectationV0_1(
            int(item["receiver_index"]),
            int(item["winning_epoch_sample"]),
            float(item["winning_cfo_hz"]),
            int(item["complete_frame_count"]),
            float(item["hard_symbol_accuracy"]),
            float(item["rms_evm"]),
        )
        for item in document["historical_conditioned_expectations"]
    )
    if len(expected) != 2:
        raise ValueError("retro-QAM corpus requires two receiver expectations")
    combined = _mapping(
        document.get("historical_combined_expectation"), "combined expectation"
    )
    return RetroQamCanaryInputV0_1(
        str(document["fixture_id"]),
        Digest.sha256(manifest_bytes),
        actual_iq_digest,
        selected_digest,
        int(window["sample_offset"]),
        float(fmt["sample_rate_hz"]),
        cast(tuple[np.ndarray, np.ndarray], samples),
        expected,
        RetroQamCombinedExpectationV0_1(
            float(combined["hard_symbol_accuracy"]),
            float(combined["rms_evm"]),
            float(combined["soft_mean_confidence"]),
        ),
    )


def _write_receipt(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay the frozen historical Starlink QAM corpus in Redux"
    )
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--producer-version", default="0.1.0")
    parser.add_argument("--git-commit", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    started = time.time_ns()
    try:
        request = _load_request(args.corpus_manifest)
        receipt = analyze_starlink_retro_qam_canary_v0_1(
            request,
            AnalysisExecutionContext(
                "leo-starlink-retro-qam-canary",
                args.producer_version,
                args.git_commit,
                Digest.sha256(
                    f"python={sys.version_info.major}.{sys.version_info.minor};numpy={np.__version__}".encode()
                ),
                UtcNs(started),
                UtcNs(time.time_ns()),
                "gauss-analysis-host",
            ),
        )
        payload = canonical_json_bytes(receipt) + b"\n"
        _write_receipt(args.receipt, payload)
        sys.stdout.buffer.write(payload)
        return 0 if receipt.metrics_match_oracle else 1
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        sys.stderr.write(
            json.dumps(
                {
                    "event": "starlink_retro_qam_canary_failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
