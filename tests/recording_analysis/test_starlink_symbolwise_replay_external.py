from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from leo_flow.analysis.recording.starlink_symbolwise_replay import (
    StarlinkSymbolwiseReplayAnalyzerV0_1,
    StarlinkSymbolwiseReplayConfigV0_1,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.contracts.starlink_symbolwise_replay import (
    V0_1,
    StarlinkReceiverFrequencyCenterV0_1,
)

from .fakes import execution_context

MANIFEST_PATH = (
    Path(__file__).parent / "fixtures/starlink_symbolwise_parity_2026_08_18_v1.json"
)
MANIFEST_SHA256 = "2fcafad31dd3a9be93ec27dea3884da46acbf20d134d6d0a4554e468175b575c"


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_bytes())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _require_case_paths(case: dict[str, Any]) -> tuple[Path, Path]:
    iq_path = Path(case["object_path"])
    source_path = Path(case["calibration"]["source_path"])
    if not os.path.isfile(iq_path) or not os.path.isfile(source_path):
        pytest.skip(f"read-only external corpus is absent: {case['case_id']}")
    return iq_path, source_path


class _SelectedWindowReader:
    def __init__(self, values: np.ndarray) -> None:
        self._values = values

    def read_window(self, start_sample: int, sample_count: int) -> np.ndarray:
        return self._values[start_sample : start_sample + sample_count]


def _frequency_center(
    case: dict[str, Any], receiver_index: int
) -> StarlinkReceiverFrequencyCenterV0_1:
    calibration = case["calibration"]
    return StarlinkReceiverFrequencyCenterV0_1(
        SchemaRef(StarlinkReceiverFrequencyCenterV0_1.SCHEMA_ID, V0_1),
        calibration["calibration_id"],
        Digest.sha256(calibration["hardware_epoch_identity"].encode()),
        Digest.sha256(
            calibration["receiver_signal_path_identities"][receiver_index].encode()
        ),
        ArtifactRef(
            f"frequency-center-source-{case['case_id']}",
            Digest.sha256(Path(calibration["source_path"]).read_bytes()),
            SchemaRef("org.leo-flow.external-frequency-center-source"),
        ),
        calibration["receiver_centers_hz"][receiver_index],
        "absolute-cfo-relative-to-recording-if-center",
        True,
    )


def _selected_samples(
    case: dict[str, Any], receiver_index: int
) -> tuple[np.ndarray, str]:
    fmt = _manifest()["format"]
    iq_path, _ = _require_case_paths(case)
    with iq_path.open("rb") as stream:
        stream.seek(case["selected_byte_offset"])
        selected = stream.read(case["selected_byte_count"])
    raw = np.frombuffer(selected, dtype=fmt["component_dtype"]).reshape(
        -1, fmt["receiver_count"], 2
    )
    iq = raw[:, receiver_index]
    values = np.asarray(iq[:, 0] + 1j * iq[:, 1], np.complex64)
    return values, hashlib.sha256(selected).hexdigest()


def test_external_parity_manifest_is_frozen_and_explicit_about_oracle() -> None:
    assert hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest() == MANIFEST_SHA256
    document = _manifest()
    assert document["legacy_oracle"] == {
        "commit": "0bb80d14759fd8496b74e7d3219a690be18565a6",
        "method": "pilot_symbolwise_v3",
    }
    for case in document["cases"]:
        calibration = case["calibration"]
        assert len(calibration["receiver_centers_hz"]) == 2
        assert len(set(calibration["receiver_signal_path_identities"])) == 2
        assert all(
            "lnb" not in item for item in calibration["receiver_signal_path_identities"]
        )


@pytest.mark.integration
@pytest.mark.parametrize("case", _manifest()["cases"], ids=lambda item: item["case_id"])
def test_external_iq_and_frequency_center_sources_match_frozen_hashes(
    case: dict[str, Any],
) -> None:
    iq_path, source_path = _require_case_paths(case)
    assert iq_path.stat().st_size == case["object_byte_count"]
    assert _sha256(iq_path) == case["object_sha256"]
    assert _sha256(source_path) == case["calibration"]["source_sha256"]
    _, selected_sha256 = _selected_samples(case, 0)
    assert selected_sha256 == case["selected_sha256"]


@pytest.mark.integration
@pytest.mark.parametrize("case", _manifest()["cases"], ids=lambda item: item["case_id"])
def test_native_symbolwise_qin_matches_frozen_legacy_oracle_with_symmetric_controls(
    case: dict[str, Any],
) -> None:
    fmt = _manifest()["format"]
    elapsed = 0.0
    for expected in case["expected"]:
        receiver = expected["receiver_index"]
        samples, selected_sha256 = _selected_samples(case, receiver)
        assert selected_sha256 == case["selected_sha256"]
        started = time.perf_counter()
        bundle = StarlinkSymbolwiseReplayAnalyzerV0_1(
            StarlinkSymbolwiseReplayConfigV0_1(), execution_context()
        ).analyze_receiver(
            _SelectedWindowReader(samples),
            recording_id=RecordingId(f"rec_{case['case_id'].replace('-', '_')}"),
            recording_identity_digest=Digest.sha256(case["object_sha256"].encode()),
            segment_id=SegmentId("seg_external_selected_window"),
            receiver_chain_id=ReceiverChainId(f"rx_physical_path_{receiver}"),
            edge=StarlinkEdge.LOWER,
            sample_rate_hz=fmt["sample_rate_hz"],
            segment_sample_count=len(samples),
            frequency_center=_frequency_center(case, receiver),
        )
        elapsed += time.perf_counter() - started
        qin = bundle.windows[0].qin
        for name in (
            "selected_candidate_rank",
            "winning_epoch_sample",
            "frame_support",
            "symbol_match_count",
        ):
            assert getattr(qin, name) == expected[name]
        for name in (
            "timing_coarse_cfo_hz",
            "symbolwise_coarse_cfo_hz",
            "symbolwise_residual_cfo_hz",
            "winning_cfo_hz",
        ):
            assert getattr(qin, name) == pytest.approx(expected[name], abs=1e-5)
        for name in (
            "timing_score",
            "timing_peak_to_median",
            "symbolwise_score",
            "symbolwise_control_score",
            "symbolwise_margin",
        ):
            assert getattr(qin, name) == pytest.approx(expected[name], abs=2e-12)
        for name in (
            "conditioned_score",
            "conditioned_control_score",
            "conditioned_margin",
        ):
            assert getattr(qin, name) == pytest.approx(expected[name], abs=5e-7)
        assert len(bundle.windows[0].patterns) == 5
        assert {
            item.timing_search_cell_count for item in bundle.windows[0].patterns
        } == {29_997}
        assert {
            item.refinement_search_cell_count for item in bundle.windows[0].patterns
        } == {188}
        assert bundle.candidates_only is True
    assert elapsed < 10.0
