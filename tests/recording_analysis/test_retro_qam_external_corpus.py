from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from leo_flow.analysis.recording.starlink_detector_suite import (
    StarlinkDetectorSuiteConfigV0_2,
    StarlinkDetectorSuiteV0_2,
)
from leo_flow.analysis.recording.starlink_pilot_constellation import (
    StarlinkPilotConstellationAnalyzerV0_1,
    StarlinkPilotConstellationConfigV0_1,
)
from leo_flow.analysis.recording.starlink_templates import (
    qin_edge_pilot_template_pair_v0_1,
)
from leo_flow.contracts.core import Digest, ReceiverChainId, RecordingId, SegmentId
from leo_flow.contracts.starlink import StarlinkEdge

from .fakes import execution_context

MANIFEST_PATH = Path(__file__).parent / "fixtures/retro_qam_2026_08_17_v1.json"
MANIFEST_SHA256 = "47a5c98064128cfdcebcf1350acb3b3005f2646e769d45d8c92a5f2def22ba7e"


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_bytes())


def _archive_root(document: dict[str, Any]) -> Path:
    root = Path(document["archive"]["root"])
    # Avoid propagating automount errors: an unreachable external corpus is absent.
    if not os.path.isdir(root):
        pytest.skip("read-only RETRO QAM corpus is not mounted")
    return root


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def test_frozen_manifest_accepts_native_v03_search_without_detection_claim() -> None:
    assert hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest() == MANIFEST_SHA256
    document = _manifest()

    assert document["schema"] == "org.leo-flow.external-retro-qam-corpus/v1"
    assert document["archive"]["root"] == (
        "/mnt/qnap01/mouse9911/leo-store/2026_08_17_RETRO_QAM"
    )
    assert document["scope"] == {
        "calibrated_detection": False,
        "known_published_pilot": True,
        "payload_decoded": False,
    }
    assert document["future_revised_search_acceptance"]["status"] == (
        "accepted-native-v0.3-multibasin-search"
    )
    assert (
        document["future_revised_search_acceptance"]["asserted_by_current_regression"]
        is True
    )


@pytest.mark.integration
def test_archive_hashes_and_ci16_geometry_are_exact() -> None:
    document = _manifest()
    root = _archive_root(document)
    checksum_path = root / "SHA256SUMS"

    assert _sha256(checksum_path) == document["archive"]["sha256sums_sha256"]
    listed = {
        line.split(maxsplit=1)[1]: line.split(maxsplit=1)[0]
        for line in checksum_path.read_text(encoding="ascii").splitlines()
    }
    expected = {
        item["relative_path"]: item["sha256"] for item in document["archive_objects"]
    }
    assert listed == expected
    for relative_path, digest in expected.items():
        assert _sha256(root / relative_path) == digest

    iq = document["iq_object"]
    clip = root / iq["relative_path"]
    assert clip.stat().st_size == iq["byte_count"]
    assert iq["byte_count"] == iq["sample_count"] * iq["bytes_per_sample"]
    window = document["selected_window"]
    assert window["byte_offset"] == window["sample_offset"] * iq["bytes_per_sample"]
    assert window["byte_count"] == window["sample_count"] * iq["bytes_per_sample"]
    with clip.open("rb") as stream:
        stream.seek(window["byte_offset"])
        selected = stream.read(window["byte_count"])
    assert len(selected) == window["byte_count"]
    assert hashlib.sha256(selected).hexdigest() == window["sha256"]


@pytest.mark.integration
@pytest.mark.parametrize("expected", _manifest()["historical_conditioned_expectations"])
def test_native_redux_conditioned_qam_metrics_do_not_regress(
    expected: dict[str, Any],
) -> None:
    document = _manifest()
    root = _archive_root(document)
    iq = document["iq_object"]
    fmt = document["format"]
    window = document["selected_window"]
    clip = root / iq["relative_path"]
    raw = np.memmap(
        clip,
        dtype="<i2",
        mode="r",
        shape=(iq["sample_count"], fmt["receiver_count"], 2),
    )
    receiver = expected["receiver_index"]
    values = raw[
        window["sample_offset"] : window["sample_offset"] + window["sample_count"],
        receiver,
    ]
    samples = np.asarray(
        (values[:, 0].astype(np.float32) + 1j * values[:, 1]) / 32768.0,
        dtype=np.complex64,
    )

    # A one-cell suite supplies the public, immutable acquire contract at the
    # historical winner. It intentionally does not assert today's search finds it.
    suite = StarlinkDetectorSuiteV0_2(
        StarlinkDetectorSuiteConfigV0_2(
            (expected["winning_epoch_sample"],),
            (expected["winning_cfo_hz"],),
            (0.0,),
            maximum_probe_samples=window["sample_count"],
        ),
        execution_context(),
    ).analyze_receiver(
        samples,
        recording_id=RecordingId("rec_retro_qam_20260813"),
        recording_identity_digest=Digest.sha256(document["recording_id"].encode()),
        segment_id=SegmentId("seg_retro_qam_68p7s"),
        receiver_chain_id=ReceiverChainId(f"rx_retro_qam_{receiver}"),
        templates=qin_edge_pilot_template_pair_v0_1(
            fmt["sample_rate_hz"], StarlinkEdge.LOWER
        ),
    )
    evidence = StarlinkPilotConstellationAnalyzerV0_1(
        StarlinkPilotConstellationConfigV0_1(
            maximum_probe_samples=window["sample_count"]
        ),
        execution_context(),
    ).analyze(samples, suite)

    assert evidence.winning_epoch_sample == expected["winning_epoch_sample"]
    assert evidence.winning_coarse_cfo_hz == expected["winning_cfo_hz"]
    assert evidence.complete_frame_count == expected["complete_frame_count"]
    assert evidence.hard_symbol_accuracy == pytest.approx(
        expected["hard_symbol_accuracy"], abs=1e-12
    )
    assert evidence.rms_evm == pytest.approx(expected["rms_evm"], abs=1e-6)
    assert evidence.candidate_only is True
    assert evidence.known_synchronization_pilot is True
    assert evidence.payload_decoded is False
