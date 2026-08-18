"""Additive candidate-only association of acquired pilot QAM and blind Doppler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ._validation import require_finite, require_token, require_utc_ns
from .core import (
    V0_1,
    Digest,
    RadioId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    SegmentId,
    UtcNs,
)
from .starlink import StarlinkEdge

MAXIMUM_PILOT_DOPPLER_SERIES = 16
MAXIMUM_PILOT_DOPPLER_WINDOWS = 32
MAXIMUM_PILOT_DOPPLER_COMPARISONS = 32
PILOT_DOPPLER_FREQUENCY_GATE_HZ = 50_000.0


@dataclass(frozen=True)
class PilotDopplerAssociationQueryV0_1:
    recording_id: RecordingId
    radio_ids: tuple[RadioId, ...] = ()
    lnb_ids: tuple[str, ...] = ()
    receiver_chain_ids: tuple[ReceiverChainId, ...] = ()
    edges: tuple[StarlinkEdge, ...] = ()
    maximum_windows_per_stream: int = MAXIMUM_PILOT_DOPPLER_WINDOWS

    def __post_init__(self) -> None:
        for values, label in (
            (self.radio_ids, "radios"),
            (self.lnb_ids, "LNBs"),
            (self.receiver_chain_ids, "receivers"),
            (self.edges, "edges"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"pilot Doppler query {label} must be unique")
        for value in self.lnb_ids:
            require_token(value, "lnb_id")
        if not 1 <= self.maximum_windows_per_stream <= MAXIMUM_PILOT_DOPPLER_WINDOWS:
            raise ValueError("pilot Doppler query window bound is invalid")


@dataclass(frozen=True)
class PilotQamFrequencyWindowV0_1:
    window_index: int
    start_sample: int
    stop_sample: int
    interval_start_utc_ns: UtcNs
    interval_stop_utc_ns: UtcNs
    winning_cfo_hz: float
    absolute_frequency_hz: float
    qam_goodness: float
    hard_symbol_accuracy: float
    rms_evm: float

    def __post_init__(self) -> None:
        if (
            self.window_index < 0
            or self.start_sample < 0
            or self.stop_sample <= self.start_sample
        ):
            raise ValueError("pilot QAM frequency window geometry is invalid")
        require_utc_ns(self.interval_start_utc_ns, "interval_start_utc_ns")
        require_utc_ns(self.interval_stop_utc_ns, "interval_stop_utc_ns")
        if self.interval_stop_utc_ns <= self.interval_start_utc_ns:
            raise ValueError("pilot QAM frequency window UTC interval is invalid")
        for name in (
            "winning_cfo_hz",
            "absolute_frequency_hz",
            "qam_goodness",
            "hard_symbol_accuracy",
            "rms_evm",
        ):
            require_finite(getattr(self, name), name)
        if (
            not 0 <= self.qam_goodness <= 1
            or not 0 <= self.hard_symbol_accuracy <= 1
            or self.rms_evm < 0
        ):
            raise ValueError("pilot QAM frequency metrics are invalid")


@dataclass(frozen=True)
class PilotQamFrequencyFitV0_1:
    reference_utc_ns: UtcNs
    reference_frequency_hz: float
    drift_rate_hz_s: float
    residual_rms_hz: float
    support_count: int
    selection: str

    def __post_init__(self) -> None:
        require_utc_ns(self.reference_utc_ns, "reference_utc_ns")
        for name in ("reference_frequency_hz", "drift_rate_hz_s", "residual_rms_hz"):
            require_finite(getattr(self, name), name)
        if self.residual_rms_hz < 0 or not 2 <= self.support_count <= 8:
            raise ValueError("pilot QAM frequency fit support is invalid")
        if self.selection != "top-qam-goodness-up-to-eight-diagnostic":
            raise ValueError("pilot QAM frequency fit selection is unknown")


@dataclass(frozen=True)
class PilotDopplerDistancePointV0_1:
    window_index: int
    midpoint_utc_ns: UtcNs
    pilot_frequency_hz: float
    blind_path_frequency_hz: float
    absolute_distance_hz: float

    def __post_init__(self) -> None:
        if self.window_index < 0:
            raise ValueError("pilot Doppler point index is invalid")
        require_utc_ns(self.midpoint_utc_ns, "midpoint_utc_ns")
        for name in (
            "pilot_frequency_hz",
            "blind_path_frequency_hz",
            "absolute_distance_hz",
        ):
            require_finite(getattr(self, name), name)
        if self.absolute_distance_hz < 0:
            raise ValueError("pilot Doppler distance cannot be negative")


@dataclass(frozen=True)
class PilotDopplerPathComparisonV0_1:
    path_digest: Digest
    association_state: str
    blind_path_drift_rate_hz_s: float
    pilot_drift_rate_hz_s: float
    drift_rate_difference_hz_s: float
    minimum_frequency_distance_hz: float
    median_frequency_distance_hz: float
    frequency_gate_hz: float
    points: tuple[PilotDopplerDistancePointV0_1, ...]

    def __post_init__(self) -> None:
        if self.association_state not in {
            "frequency-compatible-candidate",
            "frequency-mismatch",
            "insufficient-time-overlap",
        }:
            raise ValueError("pilot Doppler association state is invalid")
        for name in (
            "blind_path_drift_rate_hz_s",
            "pilot_drift_rate_hz_s",
            "drift_rate_difference_hz_s",
            "minimum_frequency_distance_hz",
            "median_frequency_distance_hz",
            "frequency_gate_hz",
        ):
            require_finite(getattr(self, name), name)
        if (
            self.minimum_frequency_distance_hz < 0
            or self.median_frequency_distance_hz < self.minimum_frequency_distance_hz
            or self.frequency_gate_hz <= 0
            or len(self.points) > MAXIMUM_PILOT_DOPPLER_WINDOWS
        ):
            raise ValueError("pilot Doppler comparison bounds are invalid")
        if (self.association_state == "insufficient-time-overlap") != (
            len(self.points) < 2
        ):
            raise ValueError("pilot Doppler overlap state is inconsistent")


@dataclass(frozen=True)
class PilotDopplerAssociationSeriesV0_1:
    recording_id: RecordingId
    radio_id: RadioId
    lnb_id: str
    receiver_chain_id: ReceiverChainId
    segment_id: SegmentId
    edge: StarlinkEdge
    center_frequency_hz: float
    qam_windows: tuple[PilotQamFrequencyWindowV0_1, ...]
    pilot_fit: PilotQamFrequencyFitV0_1
    comparisons: tuple[PilotDopplerPathComparisonV0_1, ...]

    def __post_init__(self) -> None:
        require_token(self.lnb_id, "lnb_id")
        require_finite(self.center_frequency_hz, "center_frequency_hz")
        if (
            self.center_frequency_hz <= 0
            or not 2 <= len(self.qam_windows) <= MAXIMUM_PILOT_DOPPLER_WINDOWS
        ):
            raise ValueError("pilot Doppler series window membership is invalid")
        if len(self.comparisons) > MAXIMUM_PILOT_DOPPLER_COMPARISONS:
            raise ValueError("pilot Doppler series comparisons are unbounded")


@dataclass(frozen=True)
class RecordingPilotDopplerAssociationViewV0_1:
    schema: SchemaRef
    recording_id: RecordingId
    state: str
    frequency_gate_hz: float
    series: tuple[PilotDopplerAssociationSeriesV0_1, ...]
    candidate_only: bool
    calibrated_detection_count: None
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.dashboard.recording-pilot-doppler-association"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported pilot Doppler association schema")
        if self.state not in {"complete", "pending", "missing", "error"}:
            raise ValueError("pilot Doppler association state is invalid")
        require_finite(self.frequency_gate_hz, "frequency_gate_hz")
        if (
            self.frequency_gate_hz != PILOT_DOPPLER_FREQUENCY_GATE_HZ
            or len(self.series) > MAXIMUM_PILOT_DOPPLER_SERIES
            or self.candidate_only is not True
            or self.calibrated_detection_count is not None
        ):
            raise ValueError("pilot Doppler association safety bounds are invalid")
        if (self.state == "complete") != bool(self.series):
            raise ValueError("pilot Doppler association state and series disagree")
        if tuple(sorted(set(self.warnings))) != self.warnings:
            raise ValueError("pilot Doppler warnings must be canonical")


class RecordingPilotDopplerAssociationQueryPortV0_1(Protocol):
    def recording_pilot_doppler_association(
        self, query: PilotDopplerAssociationQueryV0_1
    ) -> RecordingPilotDopplerAssociationViewV0_1: ...
