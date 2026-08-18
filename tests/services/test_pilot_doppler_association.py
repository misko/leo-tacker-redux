from __future__ import annotations

from types import SimpleNamespace

import pytest

from leo_flow.contracts.core import (
    Digest,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.dashboard_pilot_doppler import (
    PilotDopplerAssociationQueryV0_1,
)
from leo_flow.contracts.starlink import StarlinkEdge
from leo_flow.services.pilot_doppler_association import (
    RecordingPilotDopplerAssociationServiceV0_1,
)

_BASE = 1_787_000_000_000_000_000
_CENTER = 1_709_687_500.0
_RECORDING = RecordingId("rec_pilot_doppler")
_SEGMENT = SegmentId("seg_pilot_doppler")
_RECEIVER = ReceiverChainId("rx_pilot_doppler")


def _qam_window(index: int, cfo_hz: float) -> SimpleNamespace:
    start = _BASE + (index + 1) * 1_000_000_000
    return SimpleNamespace(
        qam=SimpleNamespace(
            window_index=index,
            start_sample=index * 50_000,
            stop_sample=(index + 1) * 50_000,
            interval_start_utc_ns=UtcNs(start),
            interval_stop_utc_ns=UtcNs(start + 20_000_000),
            winning_cfo_hz=cfo_hz,
            hard_symbol_accuracy=0.9,
            rms_evm=0.6,
        )
    )


def _path(offset_hz: float, label: bytes) -> SimpleNamespace:
    windows = []
    for index in range(5):
        midpoint = _BASE + (index + 1) * 1_000_000_000 + 10_000_000
        windows.append(
            SimpleNamespace(
                point_start_utc_ns=UtcNs(midpoint - 1_000_000),
                point_stop_utc_ns=UtcNs(midpoint + 1_000_000),
                midpoint_frequency_hz=_CENTER + 450_000.0 - 4_000.0 * index + offset_hz,
            )
        )
    return SimpleNamespace(
        recording_id=_RECORDING,
        radio_id=RadioId("radio_pilot_doppler"),
        lnb_id="lnb-current",
        receiver_chain_id=_RECEIVER,
        segment_id=_SEGMENT,
        path_digest=Digest.sha256(label),
        total=SimpleNamespace(drift_rate_hz_s=-4_000.0),
        windows=tuple(windows),
    )


class _Recordings:
    def recording_capture_detail(self, recording_id: RecordingId) -> SimpleNamespace:
        assert recording_id == _RECORDING
        return SimpleNamespace(
            segments=(
                SimpleNamespace(segment_id=_SEGMENT, center_frequency_hz=_CENTER),
            )
        )


class _Qam:
    def recording_starlink_adaptive_qam(self, query: object) -> SimpleNamespace:
        return SimpleNamespace(
            streams=(
                SimpleNamespace(
                    radio_id=RadioId("radio_pilot_doppler"),
                    lnb_id="lnb-current",
                    receiver_chain_id=_RECEIVER,
                    segment_id=_SEGMENT,
                    edge=StarlinkEdge.LOWER,
                    windows=tuple(
                        _qam_window(index, 450_000.0 - 4_000.0 * index)
                        for index in range(5)
                    ),
                ),
            )
        )


class _Doppler:
    def recording_evidence_advanced_doppler(self, query: object) -> SimpleNamespace:
        return SimpleNamespace(
            state="complete",
            series=(_path(5_000.0, b"aligned"), _path(-300_000.0, b"wrong")),
        )


def test_pilot_frequency_rejects_parallel_blind_ridge_with_similar_slope() -> None:
    service = RecordingPilotDopplerAssociationServiceV0_1(
        _Recordings(),  # type: ignore[arg-type]
        _Qam(),  # type: ignore[arg-type]
        _Doppler(),  # type: ignore[arg-type]
    )

    view = service.recording_pilot_doppler_association(
        PilotDopplerAssociationQueryV0_1(_RECORDING)
    )

    assert view.state == "complete"
    assert view.candidate_only is True
    assert view.calibrated_detection_count is None
    assert len(view.series) == 1
    series = view.series[0]
    assert series.pilot_fit.drift_rate_hz_s == pytest.approx(-4_000.0)
    assert series.pilot_fit.support_count == 5
    assert [item.association_state for item in series.comparisons] == [
        "frequency-compatible-candidate",
        "frequency-mismatch",
    ]
    assert series.comparisons[0].median_frequency_distance_hz == pytest.approx(5_000.0)
    assert series.comparisons[1].median_frequency_distance_hz == pytest.approx(
        300_000.0
    )
    assert all(
        item.absolute_frequency_hz == pytest.approx(_CENTER + item.winning_cfo_hz)
        for item in series.qam_windows
    )
