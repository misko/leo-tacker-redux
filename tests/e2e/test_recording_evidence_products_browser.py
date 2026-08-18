from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from playwright.sync_api import expect, sync_playwright

from leo_flow.adapters.dashboard_http import StdlibDashboardServer
from leo_flow.contracts.core import RadioId, RecordingId
from leo_flow.dashboard import DashboardNotFound
from leo_flow.dashboard.api import (
    DashboardJsonApplicationV14,
    DashboardJsonApplicationV15,
    DashboardJsonApplicationV16,
    DashboardJsonApplicationV17,
    DashboardJsonApplicationV19,
    DashboardJsonApplicationV20,
    DashboardJsonApplicationV23,
    DashboardJsonApplicationV24,
    DashboardJsonApplicationV25,
    DashboardJsonApplicationV26,
    DashboardJsonApplicationV27,
    DashboardJsonApplicationV28,
    JsonRequest,
    JsonResponse,
)
from leo_flow.dashboard.ui import DashboardUiApplication

REQUESTED = "rec_e2e_radio_a"
COMPANION = "rec_e2e_radio_b"


class _NotFoundApplication:
    """Only the evidence APIs are relevant to this focused browser composition."""

    def handle(self, request: JsonRequest) -> JsonResponse:
        return JsonResponse(
            404,
            (("content-type", "application/json; charset=utf-8"),),
            b'{"error":{"code":"not_found","message":"fixture route absent"}}',
        )


class EvidencePorts:
    def __init__(
        self,
        *,
        qam_state: str = "complete",
        detector_state: str = "complete",
        basic_doppler_state: str = "complete",
        advanced_doppler_state: str = "complete",
        missing_qam_recordings: frozenset[str] = frozenset(),
        missing_detector_recordings: frozenset[str] = frozenset(),
    ) -> None:
        self.qam_state = qam_state
        self.detector_state = detector_state
        self.basic_doppler_state = basic_doppler_state
        self.advanced_doppler_state = advanced_doppler_state
        self.missing_qam_recordings = missing_qam_recordings
        self.missing_detector_recordings = missing_detector_recordings
        self.qam_queries: list[Any] = []
        self.detector_queries: list[Any] = []
        self.basic_doppler_queries: list[Any] = []
        self.advanced_doppler_queries: list[Any] = []
        self.timeline_queries: list[Any] = []
        self.pilot_doppler_queries: list[Any] = []
        self.pilot_prescreen_queries: list[Any] = []
        self.pilot_refinement_queries: list[Any] = []
        self.approach_queries: list[str] = []

    def recording_evidence_context(self, recording_id: RecordingId) -> dict[str, Any]:
        assert str(recording_id) == REQUESTED
        return {
            "requested_recording_id": REQUESTED,
            "capture_batch_id": "cbatch_evidence_e2e",
            "recordings": [
                {
                    "recording_id": REQUESTED,
                    "radio_id": "radio_a",
                    "radio_serial": "serial-a",
                    "analysis_state": "complete",
                    "requested": True,
                },
                {
                    "recording_id": COMPANION,
                    "radio_id": "radio_b",
                    "radio_serial": "serial-b",
                    "analysis_state": "complete",
                    "requested": False,
                },
            ],
            "receivers": [
                {
                    "recording_id": REQUESTED,
                    "radio_id": "radio_a",
                    "receiver_chain_id": "rx_a",
                    "lnb_id": "lnb_authoritative_a",
                    "radio_channel": 0,
                },
                {
                    "recording_id": COMPANION,
                    "radio_id": "radio_b",
                    "receiver_chain_id": "rx_b",
                    "lnb_id": "lnb_authoritative_b",
                    "radio_channel": 0,
                },
            ],
            "segments": [
                {
                    "recording_id": REQUESTED,
                    "segment_id": "seg_a",
                    "receiver_chain_ids": ["rx_a"],
                },
                {
                    "recording_id": COMPANION,
                    "segment_id": "seg_b",
                    "receiver_chain_ids": ["rx_b"],
                },
            ],
            "candidate_only": True,
            "calibrated_detection_count": None,
            "warnings": ["candidate-only-evidence-not-calibrated-detection"],
            "limitations": [],
        }

    def recording_starlink_acquired_constellation(self, query: Any) -> dict[str, Any]:
        self.qam_queries.append(query)
        if str(query.recording_id) in self.missing_qam_recordings:
            raise DashboardNotFound("acquired QAM recording was not found")
        self._raise_for_state(self.qam_state, "acquired QAM")
        identity = _identity(str(query.recording_id))
        if identity is None:
            raise DashboardNotFound("acquired QAM recording was not found")
        radio, receiver, lnb, segment = identity
        if not _matches(query.radio_ids, radio) or not _matches(
            query.receiver_chain_ids, receiver
        ):
            streams: list[dict[str, Any]] = []
        elif (query.lnb_ids and lnb not in query.lnb_ids) or (
            query.edges and "lower" not in {str(value.value) for value in query.edges}
        ):
            streams = []
        else:
            window_count = 1 if query.mode.value == "overall" else 2
            streams = [
                {
                    "recording_id": str(query.recording_id),
                    "radio_id": radio,
                    "lnb_id": lnb,
                    "segment_id": segment,
                    "receiver_chain_id": receiver,
                    "edge": "lower",
                    "sample_rate_hz": 2_500_000,
                    "segment_sample_count": 50_000_000,
                    "original_window_count": 2,
                    "overall": {
                        "window_count": 2,
                        "selected_display_window_index": 0,
                        "support_weighted_hard_symbol_accuracy": 0.84,
                        "support_weighted_rms_evm": 0.72,
                        "derivation": (
                            "support-weighted-window-summary;"
                            "display=max-held-out-margin-window"
                        ),
                    },
                    "windows": [
                        {
                            "window_index": index,
                            "start_sample": index * 25_000,
                            "stop_sample": (index + 1) * 25_000,
                            "interval_start_utc_ns": 1_000_000_000 + index * 10_000_000,
                            "interval_stop_utc_ns": 1_010_000_000 + index * 10_000_000,
                            "hard_symbol_accuracy": 0.84 - index * 0.04,
                            "rms_evm": 0.72 + index * 0.08,
                            "display_points": [
                                {"i": 0.70, "q": 0.72, "expected_state": 0},
                                {"i": -0.71, "q": 0.69, "expected_state": 1},
                            ],
                        }
                        for index in range(window_count)
                    ],
                }
            ]
        return {
            "recording_id": str(query.recording_id),
            "mode": query.mode.value,
            "candidate_only": True,
            "calibration_required": True,
            "streams": streams,
        }

    def recording_starlink_pilot_prescreen(self, query: Any) -> dict[str, Any]:
        self.pilot_prescreen_queries.append(query)
        identity = _identity(str(query.recording_id))
        if identity is None:
            raise DashboardNotFound("pilot prescreen recording was not found")
        radio, receiver, lnb, segment = identity
        if (
            not _matches(query.radio_ids, radio)
            or not _matches(query.receiver_chain_ids, receiver)
            or (query.lnb_ids and lnb not in query.lnb_ids)
        ):
            streams = []
        else:
            windows = [
                {
                    "window_index": index,
                    "start_sample": index * 20_000,
                    "stop_sample": (index + 1) * 20_000,
                    "start_utc_ns": 1_000_000_000 + index * 8_000_000,
                    "stop_utc_ns": 1_008_000_000 + index * 8_000_000,
                    "mean_power_counts_squared": 1000.0 + index,
                    "ofdm_periodicity_score": 0.1 + index * 0.2,
                    "best_symbol_phase_sample": 0,
                    "useful_symbol_lag_samples": 10,
                    "total_symbol_samples": 11,
                    "periodicity_rank": 0 if index == 3 else None,
                    "power_rank": 0 if index == 2 else None,
                }
                for index in range(4)
            ]
            streams = [
                {
                    "selection": {
                        "radio_id": radio,
                        "lnb_id": lnb,
                        "segment_id": segment,
                        "receiver_chain_id": receiver,
                        "channel_number": 4,
                        "edge": "lower",
                        "sample_rate_hz": 2_500_000,
                        "segment_sample_count": 80_000,
                    },
                    "windows": windows,
                    "original_window_count": 4,
                    "analyzed_sample_count": 80_000,
                    "coverage_fraction": 1.0,
                }
            ]
        return {
            "recording_id": str(query.recording_id),
            "plan": {
                "tile_sample_count": 20_000,
                "maximum_periodicity_seeds_per_stream": 32,
                "maximum_power_seeds_per_stream": 8,
            },
            "streams": streams,
            "original_window_count": 4,
            "truncated": False,
            "candidate_only": True,
            "calibrated_detection_count": None,
        }

    def recording_starlink_pilot_refinement(self, query: Any) -> dict[str, Any]:
        self.pilot_refinement_queries.append(query)
        if str(query.recording_id) in self.missing_detector_recordings:
            raise DashboardNotFound("pilot refinement recording was not found")
        self._raise_for_state(self.detector_state, "pilot refinement")
        identity = _identity(str(query.recording_id))
        if identity is None:
            raise DashboardNotFound("pilot refinement recording was not found")
        radio, receiver, lnb, segment = identity
        if (
            not _matches(query.radio_ids, radio)
            or not _matches(query.receiver_chain_ids, receiver)
            or (query.lnb_ids and lnb not in query.lnb_ids)
            or (
                query.edges
                and "lower" not in {str(value.value) for value in query.edges}
            )
        ):
            streams: list[dict[str, Any]] = []
        else:
            seeds = [
                {
                    "seed_index": 0,
                    "start_sample": 0,
                    "stop_sample": 20_000,
                    "ofdm_periodicity_score": 0.72,
                    "mean_power_counts_squared": 1000.0,
                    "periodicity_rank": 0,
                    "power_rank": None,
                },
                {
                    "seed_index": 1,
                    "start_sample": 20_000,
                    "stop_sample": 40_000,
                    "ofdm_periodicity_score": 0.64,
                    "mean_power_counts_squared": 1200.0,
                    "periodicity_rank": None,
                    "power_rank": 0,
                },
            ]
            points = []
            for method_offset, method in enumerate(query.methods):
                for window_index in range(2):
                    qin = 0.84 - method_offset * 0.08 - window_index * 0.02
                    points.append(
                        {
                            "method": method.value,
                            "window_index": window_index,
                            "interval_start_utc_ns": 1_000_000_000
                            + window_index * 10_000_000,
                            "interval_stop_utc_ns": 1_010_000_000
                            + window_index * 10_000_000,
                            "qin": {"score": qin},
                            "surrogates": [
                                {"winner": {"score": qin - 0.25}},
                                {"winner": {"score": qin - 0.30}},
                                {"winner": {"score": qin - 0.35}},
                                {"winner": {"score": qin - 0.40}},
                            ],
                            "finite_upper_tail_rank": 1,
                            "qin_minus_max_surrogate": 0.25,
                        }
                    )
            streams = [
                {
                    "selection": {
                        "radio_id": radio,
                        "lnb_id": lnb,
                        "segment_id": segment,
                        "receiver_chain_id": receiver,
                        "channel_number": 4,
                        "edge": "lower",
                        "sample_rate_hz": 2_500_000.0,
                        "segment_sample_count": 60_000,
                        "seeds": seeds,
                    },
                    "points": points,
                    "original_point_count": len(points),
                    "exact_covered_sample_count": 40_000,
                    "exact_coverage_fraction": 2 / 3,
                }
            ]
        return {
            "recording_id": str(query.recording_id),
            "candidate_only": True,
            "calibration_required": True,
            "streams": streams,
            "original_point_count": sum(len(stream["points"]) for stream in streams),
            "truncated": False,
        }

    def recording_starlink_adaptive_qam(self, query: Any) -> dict[str, Any]:
        legacy = self.recording_starlink_acquired_constellation(query)
        streams = []
        for stream in legacy["streams"]:
            streams.append(
                {
                    **stream,
                    "windows": [
                        {
                            "selection": {
                                "source_window_index": index,
                                "source_start_sample": window["start_sample"],
                                "source_stop_sample": window["stop_sample"],
                                "qam_start_sample": window["start_sample"],
                                "qam_stop_sample": window["stop_sample"],
                                "reasons": [
                                    "top-qin-score",
                                    "top-qin-minus-surrogate-margin",
                                ],
                                "source_qin_score": 0.82 - index * 0.02,
                                "source_max_surrogate_score": 0.31,
                                "source_qin_minus_max_surrogate": 0.51 - index * 0.02,
                            },
                            "qam": window,
                        }
                        for index, window in enumerate(stream["windows"])
                    ],
                }
            )
        return {
            **legacy,
            "source_adaptive_response_ref": {
                "artifact_id": "sladapt_fixture",
                "digest": {"algorithm": "sha256", "value": "a" * 64},
                "schema": None,
            },
            "streams": streams,
        }

    def recording_analysis_approach(self, recording_id: RecordingId) -> dict[str, Any]:
        self.approach_queries.append(str(recording_id))
        identity = _identity(str(recording_id))
        if identity is None:
            raise DashboardNotFound("analysis approach was not found")
        radio, receiver, lnb, segment = identity
        return {
            "recording_id": str(recording_id),
            "candidate_only": True,
            "calibration_required": True,
            "warnings": [
                "legacy-lnb-label-offsets-not-applied",
                "whole-search-null-calibration-required",
                "window-sampling-is-not-full-exact-coverage",
            ],
            "qam_streams": [
                {
                    "radio_id": radio,
                    "lnb_id": lnb,
                    "segment_id": segment,
                    "receiver_chain_id": receiver,
                    "edge": "lower",
                    "sample_rate_hz": 2_500_000.0,
                    "segment_sample_count": 150_000_000,
                    "window_count": 32,
                    "window_sample_count": 50_000,
                    "analyzed_union_sample_count": 1_600_000,
                    "analyzed_union_fraction": 1_600_000 / 150_000_000,
                    "sampling_plan": "bounded-evenly-spaced-endpoint-preserving-windows",
                    "overall_derivation": "support-weighted-window-summary;display=max-held-out-margin-window",
                    "receiver_cfo_profile_ids": [
                        "gauss-current-hardware-independent-wide-cfo-2500000-v0.3"
                    ],
                    "searched_cfo_min_hz": -1_040_000.0,
                    "searched_cfo_max_hz": 1_040_000.0,
                    "coarse_search_cell_count": 2_879_712,
                    "refinement_search_cell_count": 82_944,
                    "retained_candidate_count": 256,
                    "winning_cfo_min_hz": -194_400.0,
                    "winning_cfo_max_hz": 446_600.0,
                    "hardware_calibration_state": "label-independent-wide-physical-search",
                }
            ],
        }

    def recording_starlink_full_dwell(self, query: Any) -> dict[str, Any]:
        self.detector_queries.append(query)
        if str(query.recording_id) in self.missing_detector_recordings:
            raise DashboardNotFound("full-dwell recording was not found")
        self._raise_for_state(self.detector_state, "full-dwell detector")
        identity = _identity(str(query.recording_id))
        if identity is None:
            raise DashboardNotFound("full-dwell recording was not found")
        radio, receiver, _lnb, segment = identity
        if not _matches(query.radio_ids, radio) or not _matches(
            query.receiver_chain_ids, receiver
        ):
            streams: list[dict[str, Any]] = []
        elif query.edges and "lower" not in {str(value.value) for value in query.edges}:
            streams = []
        else:
            points = []
            for method_offset, method in enumerate(query.methods):
                for window_index in range(2):
                    qin = 0.84 - method_offset * 0.08 - window_index * 0.02
                    points.append(
                        {
                            "method": method.value,
                            "interval_start_utc_ns": 1_000_000_000
                            + window_index * 10_000_000,
                            "interval_stop_utc_ns": 1_010_000_000
                            + window_index * 10_000_000,
                            "qin": {"score": qin},
                            "surrogates": [
                                {"winner": {"score": qin - 0.25}},
                                {"winner": {"score": qin - 0.30}},
                            ],
                        }
                    )
            streams = [
                {
                    "radio_id": radio,
                    "segment_id": segment,
                    "receiver_chain_id": receiver,
                    "channel_number": 4,
                    "edge": "lower",
                    "sample_rate_hz": 2_500_000.0,
                    "segment_sample_count": 60_000,
                    "prescreen_window_count": 3,
                    "exact_window_count": 2,
                    "prescreen_coverage_fraction": 1.0,
                    "exact_coverage_fraction": 2 / 3,
                    "refinement_is_data_adaptive": True,
                    "points": points,
                }
            ]
        return {
            "recording_id": str(query.recording_id),
            "queue_state": "complete",
            "candidate_only": True,
            "calibrated_detection_count": None,
            "streams": streams,
        }

    def recording_starlink_adaptive_response(self, query: Any) -> dict[str, Any]:
        self.detector_queries.append(query)
        if str(query.recording_id) in self.missing_detector_recordings:
            raise DashboardNotFound("adaptive response recording was not found")
        self._raise_for_state(self.detector_state, "adaptive response")
        identity = _identity(str(query.recording_id))
        if identity is None:
            raise DashboardNotFound("adaptive response recording was not found")
        radio, receiver, lnb, segment = identity
        if (
            not _matches(query.radio_ids, radio)
            or not _matches(query.receiver_chain_ids, receiver)
            or (query.lnb_ids and lnb not in query.lnb_ids)
            or (
                query.edges
                and "lower" not in {str(value.value) for value in query.edges}
            )
        ):
            streams: list[dict[str, Any]] = []
        else:
            exact_windows = [
                {
                    "window_index": 0,
                    "start_sample": 0,
                    "stop_sample": 20_000,
                    "stage": "sentinel",
                    "selected_by_patterns": [],
                },
                {
                    "window_index": 1,
                    "start_sample": 20_000,
                    "stop_sample": 40_000,
                    "stage": "local",
                    "selected_by_patterns": ["qin", "surrogate-0"],
                },
            ]
            points = []
            for method_offset, method in enumerate(query.methods):
                for window_index in range(2):
                    qin = 0.84 - method_offset * 0.08 - window_index * 0.02
                    points.append(
                        {
                            "method": method.value,
                            "window_index": window_index,
                            "interval_start_utc_ns": 1_000_000_000
                            + window_index * 10_000_000,
                            "interval_stop_utc_ns": 1_010_000_000
                            + window_index * 10_000_000,
                            "qin": {"score": qin},
                            "surrogates": [
                                {"winner": {"score": qin - 0.25}},
                                {"winner": {"score": qin - 0.30}},
                            ],
                            "finite_upper_tail_rank": 1,
                            "qin_minus_max_surrogate": 0.25,
                        }
                    )
            streams = [
                {
                    "radio_id": radio,
                    "lnb_id": lnb,
                    "segment_id": segment,
                    "receiver_chain_id": receiver,
                    "channel_number": 4,
                    "edge": "lower",
                    "sample_rate_hz": 2_500_000.0,
                    "segment_sample_count": 60_000,
                    "selection": {"exact_windows": exact_windows},
                    "exact_coverage_fraction": 2 / 3,
                    "points": points,
                }
            ]
        return {
            "recording_id": str(query.recording_id),
            "candidate_only": True,
            "calibration_required": True,
            "plan": {"probe_sample_count": 20_000},
            "streams": streams,
            "warnings": ["time-look-elsewhere-calibration-required"],
        }

    def recording_full_dwell_timeline(self, query: Any) -> dict[str, Any]:
        self.timeline_queries.append(query)
        identity = _identity(str(query.recording_id))
        if identity is None:
            raise DashboardNotFound("full-dwell timeline was not found")
        radio, receiver, _lnb, segment = identity
        if not _matches(query.radio_ids, radio) or not _matches(
            query.receiver_chain_ids, receiver
        ):
            streams: list[dict[str, Any]] = []
        elif query.edges and "lower" not in {str(value.value) for value in query.edges}:
            streams = []
        else:
            streams = [
                {
                    "radio_id": radio,
                    "segment_id": segment,
                    "receiver_chain_id": receiver,
                    "channel_number": 4,
                    "edge": "lower",
                    "sample_rate_hz": 2_500_000.0,
                    "segment_sample_count": 60_000,
                    "original_window_count": 3,
                    "prescreen_coverage_fraction": 1.0,
                    "exact_coverage_fraction": 1 / 3,
                    "windows": [
                        {
                            "window_index": index,
                            "start_sample": index * 20_000,
                            "stop_sample": (index + 1) * 20_000,
                            "interval_start_utc_ns": 1_000_000_000 + index * 8_000_000,
                            "interval_stop_utc_ns": 1_008_000_000 + index * 8_000_000,
                            "mean_complex_power": 1.0 + index,
                            "selected_for_exact_refinement": index == 1,
                        }
                        for index in range(3)
                    ],
                }
            ]
        count = sum(len(stream["windows"]) for stream in streams)
        return {
            "recording_id": str(query.recording_id),
            "prescreen_window_samples": 20_000,
            "prescreen_stride_samples": 20_000,
            "original_window_count": count,
            "returned_window_count": count,
            "truncated": False,
            "candidate_only": True,
            "calibrated_detection_count": None,
            "streams": streams,
        }

    def recording_evidence_doppler(self, query: Any) -> dict[str, Any]:
        self.basic_doppler_queries.append(query)
        return self._doppler(query, self.basic_doppler_state, advanced=False)

    def recording_evidence_advanced_doppler(self, query: Any) -> dict[str, Any]:
        self.advanced_doppler_queries.append(query)
        return self._doppler(query, self.advanced_doppler_state, advanced=True)

    def recording_pilot_doppler_association(self, query: Any) -> dict[str, Any]:
        self.pilot_doppler_queries.append(query)
        if str(query.recording_id) in self.missing_qam_recordings:
            raise DashboardNotFound("pilot association is pending")
        self._raise_for_state(self.qam_state, "Pilot association")
        identity = _identity(str(query.recording_id))
        if identity is None:
            raise DashboardNotFound("association is absent")
        radio, receiver, lnb, segment = identity
        center = 10_754_600_000.0
        qam_windows = [
            {
                "window_index": index,
                "start_sample": index * 50_000,
                "stop_sample": (index + 1) * 50_000,
                "interval_start_utc_ns": 1_000_000_000 + index * 10_000_000,
                "interval_stop_utc_ns": 1_008_000_000 + index * 10_000_000,
                "winning_cfo_hz": 450_000.0 - index * 4_000.0,
                "absolute_frequency_hz": center + 450_000.0 - index * 4_000.0,
                "qam_goodness": 0.9,
                "hard_symbol_accuracy": 0.92,
                "rms_evm": 0.58,
            }
            for index in range(2)
        ]
        compatible = radio == "radio_a"
        distance = 5_000.0 if compatible else 300_000.0
        return {
            "recording_id": str(query.recording_id),
            "state": "complete",
            "frequency_gate_hz": 50_000.0,
            "candidate_only": True,
            "calibrated_detection_count": None,
            "warnings": ["candidate-only-no-calibrated-detection"],
            "series": [
                {
                    "recording_id": str(query.recording_id),
                    "radio_id": radio,
                    "lnb_id": lnb,
                    "receiver_chain_id": receiver,
                    "segment_id": segment,
                    "edge": "lower",
                    "center_frequency_hz": center,
                    "qam_windows": qam_windows,
                    "pilot_fit": {"drift_rate_hz_s": -4_000.0},
                    "comparisons": [
                        {
                            "path_digest": "a" * 64,
                            "association_state": "frequency-compatible-candidate"
                            if compatible
                            else "frequency-mismatch",
                            "blind_path_drift_rate_hz_s": -3_900.0,
                            "pilot_drift_rate_hz_s": -4_000.0,
                            "drift_rate_difference_hz_s": 100.0,
                            "minimum_frequency_distance_hz": distance,
                            "median_frequency_distance_hz": distance,
                            "frequency_gate_hz": 50_000.0,
                            "points": [
                                {
                                    "window_index": index,
                                    "midpoint_utc_ns": 1_004_000_000
                                    + index * 10_000_000,
                                    "pilot_frequency_hz": window[
                                        "absolute_frequency_hz"
                                    ],
                                    "blind_path_frequency_hz": window[
                                        "absolute_frequency_hz"
                                    ]
                                    - distance,
                                    "absolute_distance_hz": distance,
                                }
                                for index, window in enumerate(qam_windows)
                            ],
                        }
                    ],
                }
            ],
        }

    def _doppler(
        self, query: Any, product_state: str, *, advanced: bool
    ) -> dict[str, Any]:
        self._raise_for_state(product_state, "Doppler")
        if product_state == "missing":
            return _empty_doppler(product_state)
        series = []
        for recording in (REQUESTED, COMPANION):
            identity = _identity(recording)
            assert identity is not None
            radio, receiver, lnb, segment = identity
            if not _matches(query.radio_ids, radio):
                continue
            if not _matches(query.receiver_chain_ids, receiver):
                continue
            if query.lnb_ids and lnb not in query.lnb_ids:
                continue
            drift = (12_500.0 if radio == "radio_a" else -9_500.0) * (
                -1 if advanced else 1
            )
            windows = [
                {
                    "window_index": index,
                    "start_sample": index * 25_000,
                    "stop_sample": (index + 1) * 25_000,
                    "interval_start_utc_ns": 1_000_000_000 + index * 10_000_000,
                    "interval_stop_utc_ns": 1_010_000_000 + index * 10_000_000,
                    "point_start_utc_ns": 1_002_000_000 + index * 10_000_000,
                    "point_stop_utc_ns": 1_008_000_000 + index * 10_000_000,
                    "drift_rate_hz_s": drift + index * 200.0,
                    "midpoint_frequency_hz": 10_755_000_000.0,
                    "support_count": 2,
                }
                for index in range(2)
            ]
            item: dict[str, Any] = {
                "recording_id": recording,
                "radio_id": radio,
                "lnb_id": lnb,
                "receiver_chain_id": receiver,
                "segment_id": segment,
                "total": {"drift_rate_hz_s": drift},
                "windows": windows,
            }
            if advanced:
                item["association_state"] = "advanced-path-only"
                item["path_digest"] = "a" * 64
            else:
                item["candidate_rank"] = 1
            series.append(item)
        return {
            "requested_recording_id": REQUESTED,
            "state": "complete" if series else "missing",
            "candidate_only": True,
            "calibrated_detection_count": None,
            "series": series,
            "original_window_count": 2 * len(series),
            "truncated": False,
            "warnings": ["candidate-only-evidence-not-calibrated-detection"],
        }

    @staticmethod
    def _raise_for_state(product_state: str, label: str) -> None:
        if product_state == "pending":
            raise DashboardNotFound(f"{label} is pending")
        if product_state == "error":
            raise RuntimeError(f"{label} failed")


def _empty_doppler(state: str) -> dict[str, Any]:
    return {
        "requested_recording_id": REQUESTED,
        "state": state,
        "candidate_only": True,
        "calibrated_detection_count": None,
        "series": [],
        "original_window_count": 0,
        "truncated": False,
        "warnings": ["candidate-only-evidence-not-calibrated-detection"],
    }


def _identity(recording_id: str) -> tuple[str, str, str, str] | None:
    return {
        REQUESTED: ("radio_a", "rx_a", "lnb_authoritative_a", "seg_a"),
        COMPANION: ("radio_b", "rx_b", "lnb_authoritative_b", "seg_b"),
    }.get(recording_id)


def _matches(values: tuple[Any, ...], expected: str) -> bool:
    return not values or expected in {str(value) for value in values}


class DualReceiverEvidencePorts(EvidencePorts):
    """One recording with two authoritative receivers and exact-time QAM windows."""

    def recording_evidence_context(self, recording_id: RecordingId) -> dict[str, Any]:
        payload = super().recording_evidence_context(recording_id)
        payload["receivers"].insert(
            1,
            {
                "recording_id": REQUESTED,
                "radio_id": "radio_a",
                "receiver_chain_id": "rx_a2",
                "lnb_id": "lnb_authoritative_a2",
                "radio_channel": 1,
            },
        )
        payload["segments"][0]["receiver_chain_ids"] = ["rx_a", "rx_a2"]
        return payload

    def recording_starlink_adaptive_qam(self, query: Any) -> dict[str, Any]:
        self.qam_queries.append(query)
        if str(query.recording_id) != REQUESTED:
            raise DashboardNotFound("adaptive QAM recording was not found")
        streams = []
        receiver_rows = (
            ("rx_a", "lnb_authoritative_a", 0.70, 0.18),
            ("rx_a2", "lnb_authoritative_a2", 0.82, 0.28),
        )
        for receiver, lnb, amplitude, perturbation in receiver_rows:
            if not _matches(query.receiver_chain_ids, receiver) or (
                query.lnb_ids and lnb not in query.lnb_ids
            ):
                continue
            window_count = 1 if query.mode.value == "overall" else 2
            windows = []
            for index in range(window_count):
                starts = 1_000_000_000 + index * 10_000_000
                signs = ((1, 1), (-1, 1), (-1, -1), (1, -1))
                points = [
                    {
                        "symbol_index": state + 2,
                        "subcarrier_index": 488,
                        "expected_state": state,
                        "i": sign_i * amplitude + perturbation * 0.02,
                        "q": sign_q * amplitude - perturbation * 0.02,
                    }
                    for state, (sign_i, sign_q) in enumerate(signs)
                ]
                windows.append(
                    {
                        "selection": {
                            "source_window_index": index,
                            "source_start_sample": index * 25_000,
                            "source_stop_sample": (index + 1) * 25_000,
                            "qam_start_sample": index * 25_000,
                            "qam_stop_sample": (index + 1) * 25_000,
                            "reasons": ["top-qin-score"],
                            "source_qin_score": 0.8,
                            "source_max_surrogate_score": 0.2,
                            "source_qin_minus_max_surrogate": 0.6,
                        },
                        "qam": {
                            "window_index": index,
                            "start_sample": index * 25_000,
                            "stop_sample": (index + 1) * 25_000,
                            "interval_start_utc_ns": starts,
                            "interval_stop_utc_ns": starts + 10_000_000,
                            "hard_symbol_accuracy": 1.0,
                            "rms_evm": perturbation,
                            "display_points": points,
                        },
                    }
                )
            streams.append(
                {
                    "recording_id": REQUESTED,
                    "radio_id": "radio_a",
                    "lnb_id": lnb,
                    "segment_id": "seg_a",
                    "receiver_chain_id": receiver,
                    "edge": "lower",
                    "sample_rate_hz": 2_500_000,
                    "segment_sample_count": 50_000_000,
                    "original_window_count": 2,
                    "overall": {
                        "window_count": 2,
                        "selected_display_window_index": 0,
                        "support_weighted_hard_symbol_accuracy": 1.0,
                        "support_weighted_rms_evm": perturbation,
                        "derivation": (
                            "support-weighted-window-summary;"
                            "display=max-held-out-margin-window"
                        ),
                    },
                    "windows": windows,
                }
            )
        return {
            "recording_id": REQUESTED,
            "mode": query.mode.value,
            "candidate_only": True,
            "calibration_required": True,
            "source_adaptive_response_ref": {"artifact_id": "sladapt_dual"},
            "streams": streams,
        }


def _application(ports: EvidencePorts) -> DashboardUiApplication:
    base = cast(DashboardJsonApplicationV14, _NotFoundApplication())
    fixture_port = cast(Any, ports)
    v15 = DashboardJsonApplicationV15(base, fixture_port)
    v16 = DashboardJsonApplicationV16(v15, fixture_port, fixture_port)
    v17 = DashboardJsonApplicationV17(v16, fixture_port)
    v19 = DashboardJsonApplicationV19(v17, fixture_port)
    v20 = DashboardJsonApplicationV20(v19, fixture_port)
    v23 = DashboardJsonApplicationV23(v20, fixture_port)
    v24 = DashboardJsonApplicationV24(v23, fixture_port)
    v25 = DashboardJsonApplicationV25(v24, fixture_port)
    v26 = DashboardJsonApplicationV26(v25, fixture_port)
    v27 = DashboardJsonApplicationV27(v26, fixture_port)
    return DashboardUiApplication(DashboardJsonApplicationV28(v27, fixture_port))


@contextmanager
def running_dashboard(ports: EvidencePorts) -> Iterator[str]:
    server = StdlibDashboardServer(request_timeout_s=0.01)
    server.preflight("127.0.0.1", 0)
    application = _application(ports)
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            server.serve_once(application)

    worker = threading.Thread(target=serve, name="recording-evidence-e2e-server")
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.bound_port}"
    finally:
        stopped.set()
        worker.join(timeout=1)
        server.close(1)
        assert not worker.is_alive()


def browser_environment() -> dict[str, str | float | bool]:
    environment: dict[str, str | float | bool] = dict(os.environ)
    root = Path.home() / ".cache" / "ms-playwright" / "ubuntu-libs"
    if root.is_dir():
        environment["LD_LIBRARY_PATH"] = str(root / "usr/lib/x86_64-linux-gnu")
        environment["FONTCONFIG_FILE"] = str(root / "etc/fonts/fonts.conf")
        environment["FONTCONFIG_SYSROOT"] = str(root)
    return environment


def test_detail_page_renders_and_filters_all_populated_candidate_evidence() -> None:
    ports = EvidencePorts()
    with running_dashboard(ports) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1300})
            response = page.goto(f"{base_url}/recordings/{REQUESTED}")
            assert response is not None and response.ok

            expect(page.locator("#evidence-warning")).to_contain_text(
                "Candidate-only evidence"
            )
            expect(page.locator("#evidence-warning")).to_contain_text("never pooled")
            expect(page.locator("#evidence-context-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#evidence-context-state")).to_contain_text(
                "2 recording scope"
            )
            for product in ("qam", "detector", "doppler"):
                expect(page.locator(f"#evidence-{product}-state")).to_have_attribute(
                    "data-state", "ready"
                )
                expect(page.locator(f"#evidence-{product}-badge")).to_have_text(
                    "Candidate evidence"
                )
                expect(page.locator(f"#evidence-{product}-canvas")).to_be_visible()
            expect(page.locator("#evidence-timeline-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#evidence-timeline-badge")).to_have_text(
                "Full coverage"
            )
            expect(page.locator("#evidence-timeline-canvas")).to_be_visible()
            expect(page.locator("#evidence-timeline-canvas")).to_have_attribute(
                "data-point-count", "8"
            )
            expect(page.locator("#evidence-timeline-state")).to_contain_text(
                "source union coverage is 100%"
            )
            expect(page.locator("#evidence-prescreen-state")).to_have_attribute(
                "data-state", "ready"
            )
            expect(page.locator("#evidence-prescreen-state")).to_contain_text(
                "every source sample was prescreened"
            )
            expect(page.locator("#evidence-prescreen-canvas")).to_be_visible()
            expect(page.locator("#evidence-prescreen-canvas")).to_have_attribute(
                "data-point-count", "12"
            )
            expect(page.locator("#evidence-approaches-state")).to_have_attribute(
                "data-state", "ready"
            )
            approach_rows = page.locator("#evidence-approaches-body tr")
            expect(approach_rows).to_have_count(16)
            expect(
                approach_rows.filter(has_text="Complete-IQ OFDM pilot prescreen")
            ).to_have_count(2)
            expect(
                page.locator('#evidence-approaches-body tr[data-approach="qam"]')
            ).to_have_count(2)
            approaches = page.locator("#evidence-approaches-table")
            expect(approaches).to_contain_text("Acquired pilot QAM v0.3")
            expect(approaches).to_contain_text("Adaptive target/control QAM v0.4")
            expect(approaches).to_contain_text(
                "2 × 10.000 ms QAM around 10.000 ms exact detector windows"
            )
            expect(approaches).to_contain_text("20.000 ms / 20.000 s (0.1000%)")
            expect(approaches).to_contain_text(
                "top-qin-minus-surrogate-margin, top-qin-score"
            )
            expect(approaches).to_contain_text(
                "target/control selection bias disclosed"
            )
            expect(approaches).to_contain_text("Complete IQ power timeline")
            expect(approaches).to_contain_text(
                "Time-diverse symmetric adaptive Qin + surrogate search"
            )
            expect(approaches).to_contain_text("sentinel, local")
            expect(approaches).to_contain_text(
                "same union of sentinel, power-seed, Qin-selected, surrogate-selected, and local windows for every pattern"
            )
            expect(approaches).to_contain_text(
                "time look-elsewhere calibration required"
            )
            expect(approaches).to_contain_text(
                "conditioned algorithm score vs exact-window UTC"
            )
            expect(approaches).to_contain_text("Basic blind Doppler track")
            expect(approaches).to_contain_text("Advanced-path-only Doppler")
            expect(approaches).to_contain_text("Acquired-pilot frequency association")
            expect(approaches).to_contain_text("diagnostic gate 50000 Hz")
            expect(approaches).to_contain_text("-1040.0…1040.0 kHz physical CFO")
            expect(approaches).to_contain_text("label-independent-wide-physical-search")
            expect(approaches).to_contain_text("1.0667%")
            expect(approaches).to_contain_text("100.0000%")
            expect(approaches).not_to_contain_text("602869")
            expect(approaches).not_to_contain_text("legacy LNB correction")
            assert sorted(ports.approach_queries) == sorted([REQUESTED, COMPANION])

            expect(page.locator("#evidence-qam-heading")).to_have_text("Pilot QAM")
            expect(page.locator("#evidence-qam-state")).to_contain_text(
                "adaptive target/control window selection"
            )
            expect(page.locator("#evidence-qam-goodness")).to_contain_text(
                "top-qin-score"
            )
            expect(page.locator("#evidence-qam-goodness")).to_contain_text(
                "source Qin 0.8200, control 0.3100, margin 0.5100"
            )
            expect(page.locator("#evidence-detector-heading")).to_have_text(
                "Starlink algorithm scores"
            )
            expect(page.locator("#evidence-doppler-heading")).to_have_text(
                "Doppler estimates"
            )
            expect(page.locator("#evidence-qam-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-qam-time-canvas")).to_be_visible()
            expect(page.locator("#evidence-qam-time-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-series-count", "12"
            )
            expect(page.locator("#evidence-doppler-canvas")).to_have_attribute(
                "data-series-count", "4"
            )
            expect(page.locator("#evidence-qam-legend")).to_contain_text(
                f"{REQUESTED} · radio_a · lnb_authoritative_a · rx_a · lower · overall"
            )
            expect(page.locator("#evidence-detector-legend")).to_contain_text(
                f"{COMPANION} · radio_b · lnb_authoritative_b · rx_b · CH4 · lower · time-diverse adaptive · glrt-32 · qin"
            )
            expect(page.locator("#evidence-doppler-legend")).to_contain_text(
                "basic candidate 1"
            )
            expect(page.locator("#evidence-doppler-legend")).to_contain_text(
                "advanced path only"
            )
            expect(page.locator("#evidence-doppler-state")).to_contain_text(
                "4 unpooled series (2 basic candidate, 2 advanced-path-only)"
            )
            expect(page.locator("#evidence-pilot-doppler-state")).to_contain_text(
                "frequency-compatible-candidate"
            )
            expect(page.locator("#evidence-pilot-doppler-state")).to_contain_text(
                "frequency-mismatch"
            )
            expect(page.locator("#evidence-pilot-doppler-canvas")).to_be_visible()
            expect(page.locator("#evidence-pilot-doppler-canvas")).to_have_attribute(
                "data-series-count", "4"
            )
            expect(page.locator("#evidence-pilot-doppler-legend")).to_contain_text(
                "acquired pilot CFO"
            )
            expect(page.locator("#evidence-pilot-doppler-legend")).to_contain_text(
                "blind path"
            )
            assert len(ports.pilot_doppler_queries) == 2
            assert all(
                query.maximum_windows_per_stream == 32
                for query in ports.pilot_doppler_queries
            )
            assert len(ports.pilot_prescreen_queries) == 2
            assert all(
                query.maximum_points == 8192 for query in ports.pilot_prescreen_queries
            )
            expect(
                page.locator(
                    '#evidence-detector-approaches input[value="adaptive-time-diverse"]'
                )
            ).to_be_checked()
            expect(
                page.locator(
                    '#evidence-detector-approaches input[value="prescreen-global"]'
                )
            ).not_to_be_checked()
            assert len(ports.pilot_refinement_queries) == 0
            page.locator(
                '#evidence-detector-approaches input[value="prescreen-global"]'
            ).check()
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-series-count", "24"
            )
            expect(page.locator("#evidence-detector-legend")).to_contain_text(
                "global OFDM / power refinement"
            )
            expect(approaches).to_contain_text(
                "Complete-IQ prescreen-selected exact Qin + surrogate search"
            )
            assert len(ports.pilot_refinement_queries) == 2
            assert all(
                query.maximum_points == 4096 for query in ports.pilot_refinement_queries
            )
            page.locator(
                '#evidence-detector-approaches input[value="prescreen-global"]'
            ).uncheck()
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-series-count", "12"
            )

            page.locator("#evidence-mode").select_option("windows")
            expect(page.locator("#evidence-qam-canvas")).to_have_attribute(
                "data-series-count", "4"
            )
            expect(page.locator("#evidence-qam-time-canvas")).to_have_attribute(
                "data-point-count", "4"
            )
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-point-count", "24"
            )
            expect(page.locator("#evidence-doppler-canvas")).to_have_attribute(
                "data-point-count", "8"
            )
            expect(page.locator("#evidence-detector-state")).to_contain_text(
                "each point is one exact analyzed window"
            )
            expect(page.locator("#evidence-doppler-state")).to_contain_text(
                "explicit UTC/sample scope"
            )
            assert {query.mode.value for query in ports.qam_queries} == {
                "overall",
                "windows",
            }
            assert all(query.maximum_streams == 4 for query in ports.qam_queries)
            assert all(
                query.maximum_windows_per_stream == 32 for query in ports.qam_queries
            )
            assert {
                query.maximum_points_per_constellation for query in ports.qam_queries
            } == {1, 128}

            page.locator('#evidence-methods input[value="anchor-8"]').uncheck()
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-series-count", "8"
            )
            assert all(
                method.value != "anchor-8"
                for method in ports.detector_queries[-1].methods
            )
            page.locator('#evidence-methods input[value="anchor-8"]').check()

            page.locator('#evidence-patterns input[value="surrogate-0"]').uncheck()
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-series-count", "6"
            )
            expect(page.locator("#evidence-detector-legend")).not_to_contain_text(
                "surrogate-0"
            )
            page.locator('#evidence-patterns input[value="surrogate-0"]').check()

            page.locator(f'#evidence-radios input[value="{COMPANION}"]').uncheck()
            expect(page.locator("#evidence-qam-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-detector-canvas")).to_have_attribute(
                "data-series-count", "6"
            )
            expect(page.locator("#evidence-doppler-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-qam-legend")).not_to_contain_text(COMPANION)
            assert ports.basic_doppler_queries[-1].radio_ids == (RadioId("radio_a"),)
            page.locator(f'#evidence-radios input[value="{COMPANION}"]').check()

            page.locator('#evidence-lnbs input[value="lnb_authoritative_b"]').uncheck()
            expect(page.locator("#evidence-qam-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-doppler-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-qam-legend")).not_to_contain_text(
                "lnb_authoritative_b"
            )
            page.locator('#evidence-lnbs input[value="lnb_authoritative_b"]').check()

            page.locator('#evidence-receivers input[value="rx_b"]').uncheck()
            expect(page.locator("#evidence-qam-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            expect(page.locator("#evidence-doppler-canvas")).to_have_attribute(
                "data-series-count", "2"
            )
            page.locator('#evidence-receivers input[value="rx_b"]').check()

            page.locator('#evidence-channels input[value="4"]').uncheck()
            expect(page.locator("#evidence-detector-state")).to_have_attribute(
                "data-state", "missing"
            )
            expect(page.locator("#evidence-detector-canvas")).to_be_hidden()
            page.locator('#evidence-channels input[value="4"]').check()

            page.locator('#evidence-edges input[value="lower"]').uncheck()
            expect(page.locator("#evidence-qam-state")).to_have_attribute(
                "data-state", "missing"
            )
            expect(page.locator("#evidence-detector-state")).to_have_attribute(
                "data-state", "missing"
            )
            expect(page.locator("#evidence-doppler-state")).to_have_attribute(
                "data-state", "ready"
            )
            page.locator('#evidence-edges input[value="lower"]').check()
        finally:
            browser.close()


def test_detail_page_combines_only_exact_time_known_pilot_receiver_pairs() -> None:
    ports = DualReceiverEvidencePorts()
    with running_dashboard(ports) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1300})
            response = page.goto(f"{base_url}/recordings/{REQUESTED}")
            assert response is not None and response.ok

            combined = page.locator("#evidence-qam-combined-state")
            expect(combined).to_have_attribute("data-state", "ready")
            expect(combined).to_contain_text("1 exact-time paired candidate series")
            expect(combined).to_contain_text("no label-derived frequency correction")
            canvas = page.locator("#evidence-qam-combined-canvas")
            expect(canvas).to_be_visible()
            expect(canvas).to_have_attribute("data-series-count", "1")
            expect(canvas).to_have_attribute("data-point-count", "4")
            legend = page.locator("#evidence-qam-combined-legend")
            expect(legend).to_contain_text(
                "lnb_authoritative_a/rx_a + lnb_authoritative_a2/rx_a2"
            )
            expect(legend).to_contain_text("weights")

            page.locator('#evidence-receivers input[value="rx_a2"]').uncheck()
            expect(combined).to_have_attribute("data-state", "missing")
            expect(canvas).to_be_hidden()
        finally:
            browser.close()


def test_detail_page_distinguishes_pending_error_and_missing_real_api_states() -> None:
    ports = EvidencePorts(
        qam_state="pending",
        detector_state="error",
        basic_doppler_state="missing",
        advanced_doppler_state="missing",
    )
    with running_dashboard(ports) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            response = page.goto(f"{base_url}/recordings/{REQUESTED}")
            assert response is not None and response.ok
            expect(page.locator("#evidence-qam-state")).to_have_attribute(
                "data-state", "pending"
            )
            expect(page.locator("#evidence-qam-state")).to_contain_text(
                "pending for every selected recording"
            )
            expect(page.locator("#evidence-detector-state")).to_have_attribute(
                "data-state", "error"
            )
            expect(page.locator("#evidence-detector-state")).to_contain_text(
                "dashboard query failed"
            )
            expect(page.locator("#evidence-doppler-state")).to_have_attribute(
                "data-state", "missing"
            )
            expect(page.locator("#evidence-doppler-state")).to_contain_text(
                "Doppler evidence is missing"
            )
            expect(page.locator("#evidence-pilot-doppler-state")).to_have_attribute(
                "data-state", "pending"
            )
            for product in ("qam", "detector", "doppler"):
                expect(page.locator(f"#evidence-{product}-canvas")).to_be_hidden()
            expect(page.locator("#evidence-qam-time-canvas")).to_be_hidden()
            expect(page.locator("#evidence-pilot-doppler-canvas")).to_be_hidden()
        finally:
            browser.close()


def test_detail_page_renders_available_recording_when_companion_is_pending() -> None:
    ports = EvidencePorts(
        missing_qam_recordings=frozenset({COMPANION}),
        missing_detector_recordings=frozenset({COMPANION}),
    )
    with running_dashboard(ports) as base_url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, env=browser_environment())
        try:
            page = browser.new_page()
            response = page.goto(f"{base_url}/recordings/{REQUESTED}")
            assert response is not None and response.ok
            for product in ("qam", "detector"):
                expect(page.locator(f"#evidence-{product}-state")).to_have_attribute(
                    "data-state", "ready"
                )
                expect(page.locator(f"#evidence-{product}-canvas")).to_be_visible()
            expect(page.locator("#evidence-qam-state")).to_contain_text(
                "1 selected recording(s) remain pending"
            )
            expect(page.locator("#evidence-detector-state")).to_contain_text(
                "1 selected recording/product combination(s) remain pending"
            )
            expect(page.locator("#evidence-qam-legend")).to_contain_text(REQUESTED)
            expect(page.locator("#evidence-qam-legend")).not_to_contain_text(COMPANION)
            expect(page.locator("#evidence-detector-legend")).to_contain_text(REQUESTED)
            expect(page.locator("#evidence-detector-legend")).not_to_contain_text(
                COMPANION
            )
        finally:
            browser.close()
