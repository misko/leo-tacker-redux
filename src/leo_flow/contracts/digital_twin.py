"""Public, bounded contracts for deterministic RF digital-twin experiments."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ._validation import require_finite, require_positive, require_token
from .core import (
    V0_1,
    ArtifactRef,
    Digest,
    Provenance,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    canonical_json_bytes,
)
from .starlink import StarlinkEdge

MAX_TWIN_PATTERN_SAMPLES = 16_384
MAX_TWIN_SURROGATES = 32
MAX_TWIN_FRAMES = 512
MAX_TWIN_RECEIVERS = 4
MAX_TWIN_TOTAL_COMPLEX_SAMPLES = 1_048_576
MAX_TWIN_INTERFERERS = 16
MAX_TWIN_CANDIDATES = 32
MAX_TWIN_STATISTICS = 128
MAX_TWIN_STATISTIC_VALUES = 4_096
MAX_TWIN_COMPARISONS = 256
MAX_TWIN_JSON_BYTES = 64 * 1024 * 1024


class DigitalTwinPatternRole(str, Enum):
    QIN_EXACT = "qin-exact"
    SURROGATE = "deterministic-surrogate"


class DigitalTwinEmissionKind(str, Enum):
    NULL = "null"
    QIN_EXACT = "qin-exact"
    SURROGATE = "deterministic-surrogate"


class DigitalTwinStatisticKind(str, Enum):
    CANDIDATE_SCORE = "candidate-score"
    CONDITIONED_CONTROL_SCORE = "conditioned-control-score"
    CANDIDATE_CONTROL_MARGIN = "candidate-minus-control-margin"
    DRIFT_RATE_HZ_S = "drift-rate-hz-s"
    DRIFT_ACCELERATION_HZ_S2 = "drift-acceleration-hz-s2"
    SPECTRAL_PEAK_EXCESS = "spectral-peak-excess"
    TRACK_DURATION_S = "track-duration-s"


@dataclass(frozen=True)
class DigitalTwinFloatRangeV0_1:
    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        require_finite(self.minimum, "minimum")
        require_finite(self.maximum, "maximum")
        if self.maximum < self.minimum:
            raise ValueError("range maximum precedes minimum")


@dataclass(frozen=True)
class DigitalTwinPilotPatternV0_1:
    """Exact Qin samples or a deterministic same-energy surrogate."""

    schema: SchemaRef
    pattern_id: str
    role: DigitalTwinPatternRole
    source_template_ref: ArtifactRef
    edge: StarlinkEdge
    pilot_indices: tuple[int, ...]
    surrogate_seed: int | None
    surrogate_index: int | None
    sample_rate_hz: float
    i_samples: tuple[float, ...]
    q_samples: tuple[float, ...]
    sample_values_digest: Digest
    energy: float

    SCHEMA_ID = "org.leo-flow.digital-twin-pilot-pattern"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported digital-twin pilot pattern schema")
        require_token(self.pattern_id, "pattern_id")
        expected_indices = (
            tuple(range(528, 536))
            if self.edge is StarlinkEdge.LOWER
            else tuple(range(488, 496))
        )
        if self.pilot_indices != expected_indices:
            raise ValueError("pattern pilot indices do not match the Qin edge")
        expected_schema = SchemaRef("org.leo-flow.starlink-edge-pilot-template", V0_1)
        if self.source_template_ref.schema != expected_schema:
            raise ValueError("pattern source must be a Qin edge-pilot template")
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        if (
            not self.i_samples
            or len(self.i_samples) != len(self.q_samples)
            or len(self.i_samples) > MAX_TWIN_PATTERN_SAMPLES
        ):
            raise ValueError("pattern sample dimensions are outside their bound")
        for value in (*self.i_samples, *self.q_samples):
            require_finite(value, "pattern_sample")
        expected_digest = Digest.sha256(
            canonical_json_bytes(
                {"i_samples": self.i_samples, "q_samples": self.q_samples}
            )
        )
        if self.sample_values_digest != expected_digest:
            raise ValueError("pattern sample digest is inconsistent")
        require_positive(self.energy, "energy")
        expected_energy = sum(
            i * i + q * q for i, q in zip(self.i_samples, self.q_samples, strict=True)
        )
        if abs(self.energy - expected_energy) > max(1e-12, expected_energy * 1e-12):
            raise ValueError("pattern energy is inconsistent")
        if self.role is DigitalTwinPatternRole.QIN_EXACT:
            if self.surrogate_seed is not None or self.surrogate_index is not None:
                raise ValueError("exact Qin pattern cannot carry surrogate identity")
        else:
            _require_seed(self.surrogate_seed, "surrogate_seed")
            if (
                isinstance(self.surrogate_index, bool)
                or not isinstance(self.surrogate_index, int)
                or self.surrogate_index < 0
                or self.surrogate_index >= MAX_TWIN_SURROGATES
            ):
                raise ValueError("surrogate index is outside its bound")


@dataclass(frozen=True)
class DigitalTwinBurstScheduleV0_1:
    period_frames: int
    on_frames: int
    phase_frames: int

    def __post_init__(self) -> None:
        for name in ("period_frames", "on_frames", "phase_frames"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.period_frames < 1 or not 0 <= self.on_frames <= self.period_frames:
            raise ValueError("burst duty schedule is invalid")
        if self.phase_frames >= self.period_frames:
            raise ValueError("burst phase must lie within its period")


@dataclass(frozen=True)
class DigitalTwinReceiverConfigV0_1:
    radio_id: RadioId
    receiver_chain_id: ReceiverChainId
    lnb_offset_hz: float
    gain_linear: float
    phase_rad: float
    missing_frame_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        require_finite(self.lnb_offset_hz, "lnb_offset_hz")
        require_positive(self.gain_linear, "gain_linear")
        require_finite(self.phase_rad, "phase_rad")
        if tuple(
            sorted(set(self.missing_frame_indices))
        ) != self.missing_frame_indices or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in self.missing_frame_indices
        ):
            raise ValueError("missing frame indices must be unique and sorted")


@dataclass(frozen=True)
class DigitalTwinToneInterferenceV0_1:
    offset_hz: float
    amplitude: float
    drift_rate_hz_s: float
    phase_rad: float

    def __post_init__(self) -> None:
        for name in ("offset_hz", "drift_rate_hz_s", "phase_rad"):
            require_finite(getattr(self, name), name)
        if self.amplitude < 0:
            raise ValueError("tone amplitude must be non-negative")
        require_finite(self.amplitude, "amplitude")


@dataclass(frozen=True)
class DigitalTwinBroadbandInterferenceV0_1:
    standard_deviation: float
    burst: DigitalTwinBurstScheduleV0_1

    def __post_init__(self) -> None:
        require_finite(self.standard_deviation, "standard_deviation")
        if self.standard_deviation < 0:
            raise ValueError("broadband standard deviation must be non-negative")


@dataclass(frozen=True)
class DigitalTwinImpairmentConfigV0_1:
    awgn_standard_deviation: float
    gain_variation_fraction: float
    gain_variation_period_frames: int
    stationary_tones: tuple[DigitalTwinToneInterferenceV0_1, ...]
    narrowband_interferers: tuple[DigitalTwinToneInterferenceV0_1, ...]
    broadband: DigitalTwinBroadbandInterferenceV0_1 | None = None

    def __post_init__(self) -> None:
        for name in ("awgn_standard_deviation", "gain_variation_fraction"):
            value = getattr(self, name)
            require_finite(value, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.gain_variation_fraction >= 1:
            raise ValueError("gain variation fraction must lie below one")
        if (
            isinstance(self.gain_variation_period_frames, bool)
            or not isinstance(self.gain_variation_period_frames, int)
            or self.gain_variation_period_frames < 1
        ):
            raise ValueError("gain variation period must be positive")
        if (
            len(self.stationary_tones) + len(self.narrowband_interferers)
            > MAX_TWIN_INTERFERERS
        ):
            raise ValueError("interference source count exceeds its bound")
        if any(tone.drift_rate_hz_s != 0 for tone in self.stationary_tones):
            raise ValueError("stationary tones cannot carry drift")


@dataclass(frozen=True)
class DigitalTwinScenarioRequestV0_1:
    schema: SchemaRef
    request_id: str
    seed: int
    exact_qin_pattern: DigitalTwinPilotPatternV0_1
    surrogate_seed: int
    surrogate_count: int
    emission_kind: DigitalTwinEmissionKind
    emission_surrogate_index: int | None
    center_frequency_hz: float
    sample_rate_hz: float
    frame_count: int
    cfo_hz: DigitalTwinFloatRangeV0_1
    drift_rate_hz_s: DigitalTwinFloatRangeV0_1
    drift_acceleration_hz_s2: DigitalTwinFloatRangeV0_1
    amplitude: DigitalTwinFloatRangeV0_1
    burst: DigitalTwinBurstScheduleV0_1
    receivers: tuple[DigitalTwinReceiverConfigV0_1, ...]
    impairments: DigitalTwinImpairmentConfigV0_1
    generator_ref: ArtifactRef
    provenance: Provenance

    SCHEMA_ID = "org.leo-flow.digital-twin-scenario-request"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported digital-twin scenario request schema")
        require_token(self.request_id, "request_id")
        _require_seed(self.seed, "seed")
        _require_seed(self.surrogate_seed, "surrogate_seed")
        if self.exact_qin_pattern.role is not DigitalTwinPatternRole.QIN_EXACT:
            raise ValueError("scenario requires the exact Qin target pattern")
        if (
            self.exact_qin_pattern.sample_values_digest
            not in self.provenance.input_digests
        ):
            raise ValueError("provenance must include the exact Qin sample digest")
        if (
            self.exact_qin_pattern.source_template_ref.digest
            not in self.provenance.dependency_digests
        ):
            raise ValueError("provenance must include the Qin template dependency")
        if self.generator_ref.schema != SchemaRef(
            "org.leo-flow.digital-twin-generator", V0_1
        ):
            raise ValueError("scenario generator reference has the wrong schema")
        if self.generator_ref.digest not in self.provenance.dependency_digests:
            raise ValueError("provenance must include the generator dependency")
        if (
            isinstance(self.surrogate_count, bool)
            or not isinstance(self.surrogate_count, int)
            or not 0 <= self.surrogate_count <= MAX_TWIN_SURROGATES
        ):
            raise ValueError("surrogate count is outside its bound")
        if self.emission_kind is DigitalTwinEmissionKind.SURROGATE:
            if (
                self.emission_surrogate_index is None
                or not 0 <= self.emission_surrogate_index < self.surrogate_count
            ):
                raise ValueError("emitted surrogate is outside the generated bank")
        elif self.emission_surrogate_index is not None:
            raise ValueError("non-surrogate emission cannot select a surrogate")
        if (
            self.emission_kind is not DigitalTwinEmissionKind.NULL
            and self.amplitude.minimum <= 0
        ):
            raise ValueError("non-null amplitude range must be strictly positive")
        require_positive(self.center_frequency_hz, "center_frequency_hz")
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        if self.sample_rate_hz != self.exact_qin_pattern.sample_rate_hz:
            raise ValueError("scenario and Qin pattern sample rates differ")
        if len(self.exact_qin_pattern.i_samples) != round(self.sample_rate_hz / 750.0):
            raise ValueError("Qin pattern must represent one 750 Hz frame")
        if (
            isinstance(self.frame_count, bool)
            or not isinstance(self.frame_count, int)
            or not 2 <= self.frame_count <= MAX_TWIN_FRAMES
        ):
            raise ValueError("frame count is outside its bound")
        if not 2 <= len(self.receivers) <= MAX_TWIN_RECEIVERS:
            raise ValueError("digital twin requires two to four receiver chains")
        receiver_keys = tuple(
            (item.radio_id, item.receiver_chain_id) for item in self.receivers
        )
        if len(receiver_keys) != len(set(receiver_keys)):
            raise ValueError("receiver identities must be unique")
        radio_ids = {item.radio_id for item in self.receivers}
        if len(radio_ids) > 2:
            raise ValueError("digital twin supports at most two radio views")
        if any(
            frame >= self.frame_count
            for receiver in self.receivers
            for frame in receiver.missing_frame_indices
        ):
            raise ValueError("missing frame lies outside the scenario")
        total_samples = (
            self.frame_count
            * len(self.exact_qin_pattern.i_samples)
            * len(self.receivers)
        )
        if total_samples > MAX_TWIN_TOTAL_COMPLEX_SAMPLES:
            raise ValueError("digital-twin sample count exceeds its resource bound")


@dataclass(frozen=True)
class DigitalTwinScenarioV0_1:
    request_digest: Digest
    request_id: str
    seed: int
    emitted_pattern_id: str | None
    emission_kind: DigitalTwinEmissionKind
    center_frequency_hz: float
    sample_rate_hz: float
    frame_count: int
    cfo_hz: float
    drift_rate_hz_s: float
    drift_acceleration_hz_s2: float
    amplitude: float
    burst: DigitalTwinBurstScheduleV0_1
    receivers: tuple[DigitalTwinReceiverConfigV0_1, ...]
    impairments: DigitalTwinImpairmentConfigV0_1
    generator_ref: ArtifactRef
    prng_algorithm: str

    def __post_init__(self) -> None:
        require_token(self.request_id, "request_id")
        require_token(self.prng_algorithm, "prng_algorithm")
        if self.generator_ref.schema != SchemaRef(
            "org.leo-flow.digital-twin-generator", V0_1
        ):
            raise ValueError("scenario generator reference has the wrong schema")
        _require_seed(self.seed, "seed")
        if self.emission_kind is DigitalTwinEmissionKind.NULL:
            if self.emitted_pattern_id is not None or self.amplitude != 0:
                raise ValueError("null scenario cannot emit a pilot pattern")
        elif self.emitted_pattern_id is None or self.amplitude <= 0:
            raise ValueError("non-null scenario requires a positive pattern emission")
        if self.emitted_pattern_id is not None:
            require_token(self.emitted_pattern_id, "emitted_pattern_id")
        require_positive(self.center_frequency_hz, "center_frequency_hz")
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        if (
            isinstance(self.frame_count, bool)
            or not isinstance(self.frame_count, int)
            or not 2 <= self.frame_count <= MAX_TWIN_FRAMES
        ):
            raise ValueError("scenario frame count is outside its bound")
        if not 2 <= len(self.receivers) <= MAX_TWIN_RECEIVERS:
            raise ValueError("scenario receiver count is outside its bound")
        receiver_keys = tuple(
            (item.radio_id, item.receiver_chain_id) for item in self.receivers
        )
        if len(receiver_keys) != len(set(receiver_keys)):
            raise ValueError("scenario receiver identities must be unique")
        if len({item.radio_id for item in self.receivers}) > 2:
            raise ValueError("scenario supports at most two radios")
        for name in (
            "cfo_hz",
            "drift_rate_hz_s",
            "drift_acceleration_hz_s2",
            "amplitude",
        ):
            require_finite(getattr(self, name), name)
        if self.amplitude < 0:
            raise ValueError("scenario amplitude must be non-negative")
        _validate_scenario_nyquist(self)


@dataclass(frozen=True)
class DigitalTwinPathPointTruthV0_1:
    frame_index: int
    midpoint_seconds: float
    pilot_present: bool
    frequency_offset_hz: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.frame_index, bool)
            or not isinstance(self.frame_index, int)
            or self.frame_index < 0
        ):
            raise ValueError("frame_index must be non-negative")
        for name in ("midpoint_seconds", "frequency_offset_hz"):
            require_finite(getattr(self, name), name)
        if self.midpoint_seconds < 0:
            raise ValueError("path midpoint must be non-negative")


@dataclass(frozen=True)
class DigitalTwinReceiverTruthV0_1:
    radio_id: RadioId
    receiver_chain_id: ReceiverChainId
    lnb_offset_hz: float
    missing_frame_indices: tuple[int, ...]
    path: tuple[DigitalTwinPathPointTruthV0_1, ...]

    def __post_init__(self) -> None:
        require_finite(self.lnb_offset_hz, "lnb_offset_hz")
        if tuple(sorted(set(self.missing_frame_indices))) != self.missing_frame_indices:
            raise ValueError("truth missing frames must be unique and sorted")
        if not self.path or tuple(item.frame_index for item in self.path) != tuple(
            range(len(self.path))
        ):
            raise ValueError("receiver truth path must cover every frame in order")


@dataclass(frozen=True)
class DigitalTwinTruthV0_1:
    scenario_request_digest: Digest
    emission_kind: DigitalTwinEmissionKind
    emitted_pattern_id: str | None
    pilot_present: bool
    pilot_present_frame_count: int
    expected_drift_rate_hz_s: float
    expected_drift_acceleration_hz_s2: float
    receiver_truth: tuple[DigitalTwinReceiverTruthV0_1, ...]

    def __post_init__(self) -> None:
        if self.pilot_present != (self.pilot_present_frame_count > 0):
            raise ValueError("pilot-presence truth is inconsistent")
        if self.emission_kind is DigitalTwinEmissionKind.NULL and self.pilot_present:
            raise ValueError("null scenario cannot contain pilot-presence truth")
        if self.emission_kind is DigitalTwinEmissionKind.NULL:
            if self.emitted_pattern_id is not None:
                raise ValueError("null truth cannot identify an emitted pattern")
        elif self.emitted_pattern_id is None:
            raise ValueError("non-null truth must identify its emitted pattern")
        if self.pilot_present_frame_count < 0:
            raise ValueError("pilot-present frame count must be non-negative")
        for name in (
            "expected_drift_rate_hz_s",
            "expected_drift_acceleration_hz_s2",
        ):
            require_finite(getattr(self, name), name)


@dataclass(frozen=True)
class DigitalTwinObservationV0_1:
    radio_id: RadioId
    receiver_chain_id: ReceiverChainId
    sample_rate_hz: float
    frame_sample_count: int
    frame_count: int
    missing_frame_indices: tuple[int, ...]
    i_samples: tuple[float, ...]
    q_samples: tuple[float, ...]
    sample_values_digest: Digest

    def __post_init__(self) -> None:
        require_positive(self.sample_rate_hz, "sample_rate_hz")
        for name in ("frame_sample_count", "frame_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be positive")
        expected = self.frame_sample_count * self.frame_count
        if len(self.i_samples) != expected or len(self.q_samples) != expected:
            raise ValueError("observation samples do not cover the declared frames")
        if tuple(
            sorted(set(self.missing_frame_indices))
        ) != self.missing_frame_indices or any(
            frame < 0 or frame >= self.frame_count
            for frame in self.missing_frame_indices
        ):
            raise ValueError("observation missing frames are invalid")
        for value in (*self.i_samples, *self.q_samples):
            require_finite(value, "observation_sample")
        expected_digest = Digest.sha256(
            canonical_json_bytes(
                {"i_samples": self.i_samples, "q_samples": self.q_samples}
            )
        )
        if self.sample_values_digest != expected_digest:
            raise ValueError("observation sample digest is inconsistent")


@dataclass(frozen=True)
class DigitalTwinBundleV0_1:
    schema: SchemaRef
    scenario: DigitalTwinScenarioV0_1
    patterns: tuple[DigitalTwinPilotPatternV0_1, ...]
    truth: DigitalTwinTruthV0_1
    observations: tuple[DigitalTwinObservationV0_1, ...]
    provenance: Provenance
    warnings: tuple[str, ...] = ()

    SCHEMA_ID = "org.leo-flow.digital-twin-bundle"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported digital-twin bundle schema")
        if (
            not self.patterns
            or self.patterns[0].role is not DigitalTwinPatternRole.QIN_EXACT
        ):
            raise ValueError("digital-twin pattern bank must begin with exact Qin")
        if len(self.patterns) > MAX_TWIN_SURROGATES + 1:
            raise ValueError("digital-twin pattern bank exceeds its bound")
        if any(
            pattern.sample_rate_hz != self.scenario.sample_rate_hz
            or len(pattern.i_samples) != round(self.scenario.sample_rate_hz / 750.0)
            for pattern in self.patterns
        ):
            raise ValueError("pattern bank differs from the scenario sample grid")
        surrogates = self.patterns[1:]
        if any(
            pattern.role is not DigitalTwinPatternRole.SURROGATE
            or pattern.surrogate_index != index
            or pattern.source_template_ref != self.patterns[0].source_template_ref
            for index, pattern in enumerate(surrogates)
        ):
            raise ValueError("digital-twin surrogate bank is not canonical")
        pattern_ids = tuple(item.pattern_id for item in self.patterns)
        if len(pattern_ids) != len(set(pattern_ids)):
            raise ValueError("digital-twin pattern identities must be unique")
        if self.scenario.emitted_pattern_id not in (*pattern_ids, None):
            raise ValueError("scenario emitted pattern is absent from the bank")
        if self.truth.scenario_request_digest != self.scenario.request_digest:
            raise ValueError("scenario and truth request identities differ")
        if (
            self.truth.emission_kind is not self.scenario.emission_kind
            or self.truth.emitted_pattern_id != self.scenario.emitted_pattern_id
            or self.truth.expected_drift_rate_hz_s != self.scenario.drift_rate_hz_s
            or self.truth.expected_drift_acceleration_hz_s2
            != self.scenario.drift_acceleration_hz_s2
        ):
            raise ValueError("scenario and signal truth differ")
        if self.scenario.generator_ref.digest not in self.provenance.dependency_digests:
            raise ValueError("bundle provenance omits its generator dependency")
        observation_keys = tuple(
            (item.radio_id, item.receiver_chain_id) for item in self.observations
        )
        receiver_keys = tuple(
            (item.radio_id, item.receiver_chain_id) for item in self.scenario.receivers
        )
        if observation_keys != receiver_keys:
            raise ValueError("observations do not match scenario receiver order")
        truth_keys = tuple(
            (item.radio_id, item.receiver_chain_id)
            for item in self.truth.receiver_truth
        )
        if truth_keys != receiver_keys:
            raise ValueError("truth does not match scenario receiver order")
        for receiver, observation, truth in zip(
            self.scenario.receivers,
            self.observations,
            self.truth.receiver_truth,
            strict=True,
        ):
            if (
                observation.sample_rate_hz != self.scenario.sample_rate_hz
                or observation.frame_count != self.scenario.frame_count
                or observation.frame_sample_count != len(self.patterns[0].i_samples)
                or observation.missing_frame_indices != receiver.missing_frame_indices
                or truth.missing_frame_indices != receiver.missing_frame_indices
                or len(truth.path) != self.scenario.frame_count
            ):
                raise ValueError("receiver observation or truth differs from scenario")
            for point in truth.path:
                expected_frequency = (
                    self.scenario.cfo_hz
                    + receiver.lnb_offset_hz
                    + self.scenario.drift_rate_hz_s * point.midpoint_seconds
                    + 0.5
                    * self.scenario.drift_acceleration_hz_s2
                    * point.midpoint_seconds**2
                )
                if abs(point.frequency_offset_hz - expected_frequency) > max(
                    1e-9, abs(expected_frequency) * 1e-12
                ):
                    raise ValueError("receiver path contradicts Doppler truth")
        presence = tuple(
            point.pilot_present for point in self.truth.receiver_truth[0].path
        )
        if (
            any(
                tuple(point.pilot_present for point in receiver.path) != presence
                for receiver in self.truth.receiver_truth[1:]
            )
            or sum(presence) != self.truth.pilot_present_frame_count
        ):
            raise ValueError("receiver paths contradict pilot-presence truth")
        if tuple(sorted(set(self.warnings))) != self.warnings:
            raise ValueError("digital-twin warnings must be unique and sorted")
        for warning in self.warnings:
            require_token(warning, "warning")
        if len(canonical_json_bytes(self)) > MAX_TWIN_JSON_BYTES:
            raise ValueError("digital-twin bundle exceeds its JSON byte bound")

    @property
    def digest(self) -> Digest:
        return Digest.sha256(canonical_json_bytes(self))


@dataclass(frozen=True)
class DigitalTwinAnalyzerInputV0_1:
    scenario_request_digest: Digest
    observation: DigitalTwinObservationV0_1
    pattern_bank: tuple[DigitalTwinPilotPatternV0_1, ...]

    def __post_init__(self) -> None:
        if (
            not self.pattern_bank
            or self.pattern_bank[0].role is not DigitalTwinPatternRole.QIN_EXACT
            or len(self.pattern_bank) > MAX_TWIN_SURROGATES + 1
        ):
            raise ValueError("analyzer input pattern bank is invalid")
        if any(
            pattern.sample_rate_hz != self.observation.sample_rate_hz
            for pattern in self.pattern_bank
        ):
            raise ValueError("analyzer input pattern and observation rates differ")


@dataclass(frozen=True)
class DigitalTwinCandidatePathPointV0_1:
    frame_index: int
    frequency_offset_hz: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.frame_index, bool)
            or not isinstance(self.frame_index, int)
            or self.frame_index < 0
            or self.frame_index >= MAX_TWIN_FRAMES
        ):
            raise ValueError("candidate frame index is outside its bound")
        require_finite(self.frequency_offset_hz, "frequency_offset_hz")


@dataclass(frozen=True)
class DigitalTwinDopplerCandidateV0_1:
    rank: int
    drift_rate_hz_s: float
    drift_acceleration_hz_s2: float
    duration_s: float
    spectral_peak_excess: float
    path: tuple[DigitalTwinCandidatePathPointV0_1, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.rank, bool)
            or not isinstance(self.rank, int)
            or self.rank < 1
        ):
            raise ValueError("candidate rank must be positive")
        for name in (
            "drift_rate_hz_s",
            "drift_acceleration_hz_s2",
            "duration_s",
            "spectral_peak_excess",
        ):
            require_finite(getattr(self, name), name)
        if self.duration_s < 0:
            raise ValueError("candidate duration must be non-negative")
        if not self.path or len(self.path) > MAX_TWIN_FRAMES:
            raise ValueError("candidate path is outside its bound")
        if any(
            later.frame_index <= earlier.frame_index
            for earlier, later in zip(self.path, self.path[1:], strict=False)
        ):
            raise ValueError("candidate path frame indices must be increasing")


@dataclass(frozen=True)
class DigitalTwinAnalyzerStatisticV0_1:
    method_id: str
    statistic: DigitalTwinStatisticKind
    value: float

    def __post_init__(self) -> None:
        require_token(self.method_id, "method_id")
        require_finite(self.value, "value")


@dataclass(frozen=True)
class DigitalTwinDopplerAnalyzerOutputV0_1:
    scenario_request_digest: Digest
    radio_id: RadioId
    receiver_chain_id: ReceiverChainId
    candidate_only: bool
    candidates: tuple[DigitalTwinDopplerCandidateV0_1, ...]
    statistics: tuple[DigitalTwinAnalyzerStatisticV0_1, ...]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.candidate_only is not True:
            raise ValueError("digital-twin Doppler output must remain candidate-only")
        if len(self.candidates) > MAX_TWIN_CANDIDATES:
            raise ValueError("Doppler candidate count exceeds its bound")
        if tuple(item.rank for item in self.candidates) != tuple(
            range(1, len(self.candidates) + 1)
        ):
            raise ValueError("Doppler candidate ranks must be contiguous")
        _validate_statistics(self.statistics)
        _validate_labels(self.warnings, "analyzer warning")


@dataclass(frozen=True)
class DigitalTwinPilotAnalyzerOutputV0_1:
    scenario_request_digest: Digest
    radio_id: RadioId
    receiver_chain_id: ReceiverChainId
    candidate_only: bool
    calibrated_detection_count: None
    statistics: tuple[DigitalTwinAnalyzerStatisticV0_1, ...]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.candidate_only is not True
            or self.calibrated_detection_count is not None
        ):
            raise ValueError("digital-twin pilot output cannot claim detections")
        _validate_statistics(self.statistics)
        _validate_labels(self.warnings, "analyzer warning")


class DigitalTwinDopplerAnalyzerPortV0_1(Protocol):
    def analyze_doppler(
        self, analyzer_input: DigitalTwinAnalyzerInputV0_1
    ) -> DigitalTwinDopplerAnalyzerOutputV0_1: ...


class DigitalTwinPilotAnalyzerPortV0_1(Protocol):
    def analyze_pilot(
        self, analyzer_input: DigitalTwinAnalyzerInputV0_1
    ) -> DigitalTwinPilotAnalyzerOutputV0_1: ...


@dataclass(frozen=True)
class DigitalTwinTrialAnalysisV0_1:
    schema: SchemaRef
    scenario_request_digest: Digest
    twin_bundle_digest: Digest
    truth_digest: Digest
    candidate_only: bool
    calibrated_detection_count: None
    doppler: tuple[DigitalTwinDopplerAnalyzerOutputV0_1, ...]
    pilot: tuple[DigitalTwinPilotAnalyzerOutputV0_1, ...]
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.digital-twin-trial-analysis"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported digital-twin trial analysis schema")
        if (
            self.candidate_only is not True
            or self.calibrated_detection_count is not None
        ):
            raise ValueError("digital-twin trial analysis cannot claim detections")
        if not self.doppler or len(self.doppler) != len(self.pilot):
            raise ValueError("trial analysis requires paired analyzer outputs")
        doppler_keys = tuple(
            (item.radio_id, item.receiver_chain_id) for item in self.doppler
        )
        pilot_keys = tuple(
            (item.radio_id, item.receiver_chain_id) for item in self.pilot
        )
        if doppler_keys != pilot_keys or len(doppler_keys) != len(set(doppler_keys)):
            raise ValueError("trial analyzer receiver identities differ")
        if any(
            item.scenario_request_digest != self.scenario_request_digest
            for item in self.doppler
        ) or any(
            item.scenario_request_digest != self.scenario_request_digest
            for item in self.pilot
        ):
            raise ValueError("trial analyzer scenario identities differ")
        _validate_labels(self.warnings, "trial warning")


@dataclass(frozen=True)
class DigitalTwinStatisticDistributionV0_1:
    method_id: str
    statistic: DigitalTwinStatisticKind
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        require_token(self.method_id, "method_id")
        if not 1 <= len(self.values) <= MAX_TWIN_STATISTIC_VALUES:
            raise ValueError("statistic distribution size is outside its bound")
        for value in self.values:
            require_finite(value, "statistic_value")


@dataclass(frozen=True)
class DigitalTwinRealDataSummaryV0_1:
    schema: SchemaRef
    source_summary_digest: Digest
    window_label: str
    candidate_only: bool
    calibrated_detection_count: None
    distributions: tuple[DigitalTwinStatisticDistributionV0_1, ...]
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.digital-twin-real-data-summary"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported real-data summary schema")
        require_token(self.window_label, "window_label")
        if (
            self.candidate_only is not True
            or self.calibrated_detection_count is not None
        ):
            raise ValueError("real-data comparison summary must remain candidate-only")
        _validate_distributions(self.distributions)
        _validate_labels(self.warnings, "real-summary warning")


@dataclass(frozen=True)
class DigitalTwinDistributionFactsV0_1:
    count: int
    mean: float
    standard_deviation: float
    minimum: float
    q10: float
    median: float
    q90: float
    maximum: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count < 1
        ):
            raise ValueError("distribution fact count must be positive")
        for name in (
            "mean",
            "standard_deviation",
            "minimum",
            "q10",
            "median",
            "q90",
            "maximum",
        ):
            require_finite(getattr(self, name), name)
        if self.standard_deviation < 0:
            raise ValueError("distribution standard deviation must be non-negative")
        if not (self.minimum <= self.q10 <= self.median <= self.q90 <= self.maximum):
            raise ValueError("distribution quantiles are not ordered")


@dataclass(frozen=True)
class DigitalTwinDistributionComparisonV0_1:
    method_id: str
    statistic: DigitalTwinStatisticKind
    twin: DigitalTwinDistributionFactsV0_1
    real: DigitalTwinDistributionFactsV0_1
    mean_difference: float
    median_difference: float
    empirical_ks_distance: float

    def __post_init__(self) -> None:
        require_token(self.method_id, "method_id")
        for name in ("mean_difference", "median_difference", "empirical_ks_distance"):
            require_finite(getattr(self, name), name)
        if not 0 <= self.empirical_ks_distance <= 1:
            raise ValueError("empirical KS distance must lie in [0, 1]")


@dataclass(frozen=True)
class DigitalTwinComparisonViewV0_1:
    schema: SchemaRef
    twin_bundle_digests: tuple[Digest, ...]
    real_source_summary_digest: Digest
    window_label: str
    candidate_only: bool
    calibrated_detection_count: None
    comparisons: tuple[DigitalTwinDistributionComparisonV0_1, ...]
    twin_only_statistics: tuple[str, ...]
    real_only_statistics: tuple[str, ...]
    warnings: tuple[str, ...]

    SCHEMA_ID = "org.leo-flow.digital-twin-comparison-view"
    REQUIRED_WARNING = "candidate-only-comparison-not-calibration-or-detection"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported digital-twin comparison view schema")
        require_token(self.window_label, "window_label")
        if not self.twin_bundle_digests:
            raise ValueError("comparison requires digital-twin bundles")
        if (
            self.candidate_only is not True
            or self.calibrated_detection_count is not None
        ):
            raise ValueError("digital-twin comparison cannot claim detections")
        if len(self.comparisons) > MAX_TWIN_COMPARISONS:
            raise ValueError("comparison count exceeds its bound")
        identities = tuple(
            (item.method_id, item.statistic) for item in self.comparisons
        )
        if len(identities) != len(set(identities)):
            raise ValueError("comparison identities must be unique")
        if len(self.twin_bundle_digests) != len(set(self.twin_bundle_digests)):
            raise ValueError("digital-twin bundle identities must be unique")
        if self.REQUIRED_WARNING not in self.warnings:
            raise ValueError("candidate-only comparison warning is required")
        for values in (
            self.twin_only_statistics,
            self.real_only_statistics,
            self.warnings,
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError("comparison labels must be unique and sorted")
            for value in values:
                require_token(value, "comparison_label")


def _require_seed(value: int | None, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 0xFFFF_FFFF_FFFF_FFFF
    ):
        raise ValueError(f"{name} must be an unsigned 64-bit integer")


def _validate_statistics(
    statistics: tuple[DigitalTwinAnalyzerStatisticV0_1, ...],
) -> None:
    if len(statistics) > MAX_TWIN_STATISTICS:
        raise ValueError("analyzer statistic count exceeds its bound")
    identities = tuple((item.method_id, item.statistic) for item in statistics)
    if len(identities) != len(set(identities)):
        raise ValueError("analyzer statistics contain duplicate identities")


def _validate_distributions(
    distributions: tuple[DigitalTwinStatisticDistributionV0_1, ...],
) -> None:
    if len(distributions) > MAX_TWIN_STATISTICS:
        raise ValueError("distribution count exceeds its bound")
    identities = tuple((item.method_id, item.statistic) for item in distributions)
    if len(identities) != len(set(identities)):
        raise ValueError("distributions contain duplicate identities")


def _validate_labels(values: tuple[str, ...], name: str) -> None:
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{name}s must be unique and sorted")
    for value in values:
        require_token(value, name)


def _validate_scenario_nyquist(scenario: DigitalTwinScenarioV0_1) -> None:
    nyquist = scenario.sample_rate_hz / 2.0
    duration = scenario.frame_count / 750.0
    times = [0.0, duration]
    if scenario.drift_acceleration_hz_s2 != 0:
        vertex = -scenario.drift_rate_hz_s / scenario.drift_acceleration_hz_s2
        if 0 < vertex < duration:
            times.append(vertex)
    for receiver in scenario.receivers:
        if any(
            abs(
                scenario.cfo_hz
                + receiver.lnb_offset_hz
                + scenario.drift_rate_hz_s * time
                + 0.5 * scenario.drift_acceleration_hz_s2 * time**2
            )
            >= nyquist
            for time in times
        ):
            raise ValueError("pilot path reaches or exceeds Nyquist")
    for tone in (
        *scenario.impairments.stationary_tones,
        *scenario.impairments.narrowband_interferers,
    ):
        if any(
            abs(tone.offset_hz + tone.drift_rate_hz_s * time) >= nyquist
            for time in (0.0, duration)
        ):
            raise ValueError("interference path reaches or exceeds Nyquist")
