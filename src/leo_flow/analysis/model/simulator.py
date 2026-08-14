"""Independent synthetic observations for the additive radio/LNB model."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from leo_flow.contracts.core import (
    FeatureId,
    ReceiverChainId,
    RecordingId,
    SegmentId,
    UtcNs,
)
from leo_flow.contracts.features import Covariance, FeatureObservation

METHOD_ID = "carrier-residual"
METHOD_VERSION = "0.1.0"
FEATURE_KIND = "carrier-residual"


@dataclass(frozen=True)
class NuisanceTerm:
    subject_id: str
    frequency_offset_hz: float
    drift_hz_s: float

    def __post_init__(self) -> None:
        if not self.subject_id:
            raise ValueError("subject_id must be non-empty")
        if not math.isfinite(self.frequency_offset_hz) or not math.isfinite(
            self.drift_hz_s
        ):
            raise ValueError("nuisance truth must be finite")


@dataclass(frozen=True)
class ReceiverAssignment:
    receiver_chain_id: str
    radio_id: str
    lnb_id: str

    def __post_init__(self) -> None:
        if not self.receiver_chain_id or not self.radio_id or not self.lnb_id:
            raise ValueError("receiver assignment identifiers must be non-empty")


@dataclass(frozen=True)
class NuisanceSimulationSpec:
    radios: tuple[NuisanceTerm, ...]
    lnbs: tuple[NuisanceTerm, ...]
    assignments: tuple[ReceiverAssignment, ...]
    samples_per_assignment: int = 12
    frequency_sigma_hz: float = 2.0
    drift_sigma_hz_s: float = 0.02
    seed: int = 0
    start_utc_ns: UtcNs = field(default_factory=lambda: UtcNs(1_000_000_000))
    spacing_ns: int = 1_000_000_000
    outlier_indices: tuple[int, ...] = ()
    missing_frequency_indices: tuple[int, ...] = ()
    missing_drift_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.samples_per_assignment < 1 or self.spacing_ns < 1:
            raise ValueError("simulation counts and spacing must be positive")
        if self.frequency_sigma_hz <= 0 or self.drift_sigma_hz_s <= 0:
            raise ValueError("simulation standard deviations must be positive")
        radio_ids = [term.subject_id for term in self.radios]
        lnb_ids = [term.subject_id for term in self.lnbs]
        receiver_ids = [item.receiver_chain_id for item in self.assignments]
        if (
            len(radio_ids) != len(set(radio_ids))
            or len(lnb_ids) != len(set(lnb_ids))
            or len(receiver_ids) != len(set(receiver_ids))
        ):
            raise ValueError("simulation identities must be unique")
        if any(item.radio_id not in radio_ids for item in self.assignments):
            raise ValueError("assignment references unknown radio")
        if any(item.lnb_id not in lnb_ids for item in self.assignments):
            raise ValueError("assignment references unknown LNB")


def simulate_nuisance_observations(
    spec: NuisanceSimulationSpec,
) -> tuple[FeatureObservation, ...]:
    """Generate deterministic contract-valid observations from independent truth.

    The simulator does not call the fitter and deliberately uses Python's seeded
    standard-library Gaussian generator.  Its samples are development fixtures,
    not a cross-runtime bitwise scientific corpus.
    """

    radios = {term.subject_id: term for term in spec.radios}
    lnbs = {term.subject_id: term for term in spec.lnbs}
    rng = random.Random(spec.seed)
    outliers = frozenset(spec.outlier_indices)
    missing_frequency = frozenset(spec.missing_frequency_indices)
    missing_drift = frozenset(spec.missing_drift_indices)
    observations: list[FeatureObservation] = []
    index = 0
    for assignment in spec.assignments:
        radio = radios[assignment.radio_id]
        lnb = lnbs[assignment.lnb_id]
        for _ in range(spec.samples_per_assignment):
            frequency = (
                radio.frequency_offset_hz
                + lnb.frequency_offset_hz
                + rng.gauss(0.0, spec.frequency_sigma_hz)
            )
            drift = (
                radio.drift_hz_s
                + lnb.drift_hz_s
                + rng.gauss(0.0, spec.drift_sigma_hz_s)
            )
            if index in outliers:
                frequency += 40.0 * spec.frequency_sigma_hz
                drift -= 40.0 * spec.drift_sigma_hz_s
            observations.append(
                FeatureObservation(
                    feature_id=FeatureId(f"feature_sim_{index}"),
                    recording_id=RecordingId(f"rec_sim_{index}"),
                    segment_id=SegmentId(f"seg_sim_{index}"),
                    method_id=METHOD_ID,
                    method_version=METHOD_VERSION,
                    window_start_sample=0,
                    window_stop_sample=64,
                    segment_sample_count=64,
                    midpoint_utc_ns=UtcNs(
                        int(spec.start_utc_ns) + index * spec.spacing_ns
                    ),
                    feature_kind=FEATURE_KIND,
                    score=1.0,
                    score_semantics="model-input-quality",
                    receiver_chain_id=ReceiverChainId(assignment.receiver_chain_id),
                    frequency_offset_hz=(
                        None if index in missing_frequency else frequency
                    ),
                    drift_hz_s=None if index in missing_drift else drift,
                    covariance=Covariance(
                        basis=("frequency_offset_hz", "drift_hz_s"),
                        units=("Hz", "Hz/s"),
                        values=(
                            (spec.frequency_sigma_hz**2, 0.0),
                            (0.0, spec.drift_sigma_hz_s**2),
                        ),
                    ),
                )
            )
            index += 1
    return tuple(observations)
