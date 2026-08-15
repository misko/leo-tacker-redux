"""Deterministic recording-time visibility candidates from archived TLE evidence.

This module consumes immutable recording/ephemeris links and the existing offline
orbit port.  Its output is deliberately weak evidence: sparse visibility samples
can narrow a candidate set, but cannot establish transmitter identity or truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from leo_flow.analysis.ephemeris.normalization import (
    NormalizedTLE,
    TLEFormatError,
    decode_normalized_catalog,
)
from leo_flow.contracts._validation import (
    require_finite,
    require_nonnegative,
    require_utc_ns,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    RecordingId,
    SchemaRef,
    UtcNs,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSelectionPolicy,
    EphemerisSnapshot,
    EphemerisSnapshotRef,
    EphemerisSource,
    RecordingInterval,
)
from leo_flow.contracts.ports import EphemerisReader
from leo_flow.contracts.storage import ObjectRef

from .association import (
    EphemerisLinkEvidence,
    OrbitPropagator,
    PropagationSpecification,
    StationGeometrySnapshot,
)

ALGORITHM_ID = "recording-visibility-candidates-v1"
POLICY_SCHEMA_ID = "org.leo-flow.recording-visibility-policy"
ASSOCIATION_SCHEMA_ID = "org.leo-flow.recording-visibility-association"
_PPM = 1_000_000
_NS_PER_SECOND = 1_000_000_000
_MAX_ELEMENT_AGE_S = 10 * 366 * 24 * 60 * 60


class RecordingVisibilityInputError(ValueError):
    """An archived snapshot or association identity failed closed validation."""


class VisibilityStatus(str, Enum):
    VISIBLE_AT_SAMPLE_WITH_MARGIN = "visible_at_sample_with_margin"
    ELEVATION_MARGIN_OVERLAP = "elevation_margin_overlap"
    BELOW_GATE_AT_SAMPLES = "below_gate_at_samples"
    ELEMENT_EPOCH_OUTSIDE_BOUND = "element_epoch_outside_bound"
    PROPAGATION_ERROR = "propagation_error"


@dataclass(frozen=True)
class RecordingVisibilityPolicy:
    """Frozen sparse-sampling and declared-uncertainty policy.

    ``elevation_uncertainty_deg`` is a pre-combined bound supplied by the
    operator/scientific configuration. Station and timestamp uncertainties are
    retained explicitly for audit but are not independently re-projected into
    elevation by this algorithm.
    """

    policy_ref: ArtifactRef
    uncertainty_basis_ref: ArtifactRef
    minimum_elevation_deg: float
    elevation_uncertainty_deg: float
    station_position_uncertainty_m: float
    recording_time_uncertainty_ns: int
    maximum_abs_element_age_s: int
    sample_fractions_ppm: tuple[int, ...]
    maximum_candidates: int

    def __post_init__(self) -> None:
        require_finite(self.minimum_elevation_deg, "minimum_elevation_deg")
        if not -90.0 <= self.minimum_elevation_deg <= 90.0:
            raise ValueError("minimum elevation must lie in [-90, 90] degrees")
        require_nonnegative(self.elevation_uncertainty_deg, "elevation_uncertainty_deg")
        if self.elevation_uncertainty_deg > 180.0:
            raise ValueError("elevation uncertainty cannot exceed 180 degrees")
        require_nonnegative(
            self.station_position_uncertainty_m,
            "station_position_uncertainty_m",
        )
        if (
            isinstance(self.recording_time_uncertainty_ns, bool)
            or not isinstance(self.recording_time_uncertainty_ns, int)
            or self.recording_time_uncertainty_ns < 0
        ):
            raise ValueError("recording_time_uncertainty_ns must be nonnegative")
        if (
            isinstance(self.maximum_abs_element_age_s, bool)
            or not isinstance(self.maximum_abs_element_age_s, int)
            or not 0 < self.maximum_abs_element_age_s <= _MAX_ELEMENT_AGE_S
        ):
            raise ValueError(
                "maximum_abs_element_age_s must be positive and at most ten leap years"
            )
        if (
            isinstance(self.maximum_candidates, bool)
            or not isinstance(self.maximum_candidates, int)
            or self.maximum_candidates <= 0
        ):
            raise ValueError("maximum_candidates must be positive")
        if (
            len(self.sample_fractions_ppm) < 2
            or self.sample_fractions_ppm
            != tuple(sorted(set(self.sample_fractions_ppm)))
            or self.sample_fractions_ppm[0] != 0
            or self.sample_fractions_ppm[-1] != _PPM
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _PPM
                for value in self.sample_fractions_ppm
            )
        ):
            raise ValueError(
                "sample fractions must be unique, sorted PPM values spanning 0 to 1000000"
            )
        expected = _visibility_policy_ref(
            self.uncertainty_basis_ref,
            minimum_elevation_deg=self.minimum_elevation_deg,
            elevation_uncertainty_deg=self.elevation_uncertainty_deg,
            station_position_uncertainty_m=self.station_position_uncertainty_m,
            recording_time_uncertainty_ns=self.recording_time_uncertainty_ns,
            maximum_abs_element_age_s=self.maximum_abs_element_age_s,
            sample_fractions_ppm=self.sample_fractions_ppm,
            maximum_candidates=self.maximum_candidates,
        )
        if self.policy_ref != expected:
            raise ValueError("visibility policy reference differs from policy content")


def build_recording_visibility_policy(
    uncertainty_basis_ref: ArtifactRef,
    *,
    minimum_elevation_deg: float,
    elevation_uncertainty_deg: float,
    station_position_uncertainty_m: float,
    recording_time_uncertainty_ns: int,
    maximum_abs_element_age_s: int,
    sample_fractions_ppm: tuple[int, ...] = (0, 500_000, 1_000_000),
    maximum_candidates: int = 10_000,
) -> RecordingVisibilityPolicy:
    """Build a policy whose artifact digest closes over every decision input."""

    ref = _visibility_policy_ref(
        uncertainty_basis_ref,
        minimum_elevation_deg=minimum_elevation_deg,
        elevation_uncertainty_deg=elevation_uncertainty_deg,
        station_position_uncertainty_m=station_position_uncertainty_m,
        recording_time_uncertainty_ns=recording_time_uncertainty_ns,
        maximum_abs_element_age_s=maximum_abs_element_age_s,
        sample_fractions_ppm=sample_fractions_ppm,
        maximum_candidates=maximum_candidates,
    )
    return RecordingVisibilityPolicy(
        ref,
        uncertainty_basis_ref,
        minimum_elevation_deg,
        elevation_uncertainty_deg,
        station_position_uncertainty_m,
        recording_time_uncertainty_ns,
        maximum_abs_element_age_s,
        sample_fractions_ppm,
        maximum_candidates,
    )


def recording_visibility_algorithm_ref() -> ArtifactRef:
    """Identify the exact weak-evidence association algorithm and semantics."""

    return ArtifactRef(
        ALGORITHM_ID,
        canonical_digest(
            {
                "algorithm_id": ALGORITHM_ID,
                "algorithm_version": "1",
                "catalog_input": "org.leo-flow.normalized-tle-catalog/1.0",
                "sampling": "integer-recording-interval-fractions-ppm",
                "gate": "sampled-maximum-elevation-with-declared-symmetric-margin",
                "ordering": "norad-id-ascending",
                "evidence_class": "weak-ephemeris-visibility",
                "ground_truth_eligible": False,
            }
        ),
        SchemaRef("org.leo-flow.recording-visibility-algorithm", V0_1),
    )


@dataclass(frozen=True)
class RecordingVisibilityRequest:
    algorithm_ref: ArtifactRef
    ephemeris_link: EphemerisLinkEvidence
    snapshot: EphemerisSnapshot
    provenance_object_ref: ObjectRef
    station: StationGeometrySnapshot
    propagation: PropagationSpecification
    candidate_norad_ids: tuple[int, ...]
    policy: RecordingVisibilityPolicy

    def __post_init__(self) -> None:
        if self.algorithm_ref != recording_visibility_algorithm_ref():
            raise ValueError("association algorithm reference is unsupported")
        expected_snapshot_ref = EphemerisSnapshotRef(
            self.snapshot.snapshot_id,
            self.snapshot.source,
            self.snapshot.raw_object_ref.digest,
            self.snapshot.normalized_object_ref.digest,
        )
        if self.ephemeris_link.snapshot_ref != expected_snapshot_ref:
            raise ValueError("recording link and archived snapshot identity differ")
        if (
            self.ephemeris_link.source is not self.snapshot.source
            or self.ephemeris_link.scope != self.snapshot.scope
        ):
            raise ValueError("recording link and archived snapshot provenance differ")
        if (
            self.snapshot.raw_object_ref.format_id != "tle-raw-v1"
            or self.snapshot.raw_object_ref.media_type != "text/plain"
            or self.snapshot.normalized_object_ref.format_id != "tle-normalized-v1"
            or self.snapshot.normalized_object_ref.media_type != "application/json"
        ):
            raise ValueError("snapshot objects have unsupported TLE archive formats")
        _validate_temporal_selection(self.ephemeris_link, self.snapshot)
        if (
            self.provenance_object_ref.format_id != "ephemeris-provenance-v1"
            or self.provenance_object_ref.media_type != "application/json"
        ):
            raise ValueError("snapshot provenance object has unsupported format")
        if (
            not self.candidate_norad_ids
            or self.candidate_norad_ids != tuple(sorted(set(self.candidate_norad_ids)))
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in self.candidate_norad_ids
            )
        ):
            raise ValueError("candidate NORAD IDs must be positive, sorted, and unique")
        if len(self.candidate_norad_ids) > self.policy.maximum_candidates:
            raise ValueError("candidate set exceeds frozen policy bound")


@dataclass(frozen=True)
class VisibilitySample:
    utc_ns: UtcNs
    nominal_elevation_deg: float | None
    lower_elevation_deg: float | None
    upper_elevation_deg: float | None
    propagation_error_code: str | None

    def __post_init__(self) -> None:
        require_utc_ns(self.utc_ns, "utc_ns")
        values = (
            self.nominal_elevation_deg,
            self.lower_elevation_deg,
            self.upper_elevation_deg,
        )
        if self.propagation_error_code is None:
            for value in values:
                if value is None:
                    raise ValueError(
                        "successful visibility sample requires elevation bounds"
                    )
                require_finite(value, "sample elevation")
        elif any(value is not None for value in values):
            raise ValueError("failed visibility sample cannot carry elevation values")


@dataclass(frozen=True)
class RecordingVisibilityCandidate:
    norad_id: int
    tle_name: str | None
    element_epoch_utc_ns: UtcNs
    maximum_abs_element_age_ns: int
    status: VisibilityStatus
    samples: tuple[VisibilitySample, ...]

    def __post_init__(self) -> None:
        if self.norad_id <= 0:
            raise ValueError("NORAD ID must be positive")
        require_utc_ns(self.element_epoch_utc_ns, "element_epoch_utc_ns")
        if self.maximum_abs_element_age_ns < 0:
            raise ValueError("element age must be nonnegative")


@dataclass(frozen=True)
class RecordingVisibilityAssociation:
    schema: SchemaRef
    association_id: str
    association_digest: Digest
    request_digest: Digest
    algorithm_ref: ArtifactRef
    recording_id: RecordingId
    recording_identity_digest: Digest
    recording_interval: RecordingInterval
    source: EphemerisSource
    snapshot: EphemerisSnapshot
    provenance_object_ref: ObjectRef
    station: StationGeometrySnapshot
    propagation: PropagationSpecification
    policy: RecordingVisibilityPolicy
    evidence_class: str
    ground_truth_eligible: bool
    associated_norad_ids: tuple[int, ...]
    possible_norad_ids: tuple[int, ...]
    candidates: tuple[RecordingVisibilityCandidate, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(ASSOCIATION_SCHEMA_ID, V0_1):
            raise ValueError("unsupported recording visibility association schema")
        if self.evidence_class != "weak-ephemeris-visibility":
            raise ValueError("recording visibility must remain weak evidence")
        if self.ground_truth_eligible is not False:
            raise ValueError("recording visibility cannot become ground truth")
        if self.algorithm_ref != recording_visibility_algorithm_ref():
            raise ValueError("association algorithm reference is unsupported")
        if self.candidates != tuple(
            sorted(self.candidates, key=lambda item: item.norad_id)
        ):
            raise ValueError("visibility candidates must be ordered by NORAD ID")
        if self.associated_norad_ids != tuple(
            item.norad_id
            for item in self.candidates
            if item.status is VisibilityStatus.VISIBLE_AT_SAMPLE_WITH_MARGIN
        ):
            raise ValueError("associated NORAD IDs differ from candidate statuses")
        if self.possible_norad_ids != tuple(
            item.norad_id
            for item in self.candidates
            if item.status is VisibilityStatus.ELEVATION_MARGIN_OVERLAP
        ):
            raise ValueError("possible NORAD IDs differ from candidate statuses")
        expected = canonical_digest(_association_identity(self))
        if self.association_digest != expected:
            raise ValueError("visibility association digest differs")
        if self.association_id != f"visassoc_{expected.value[:32]}":
            raise ValueError("visibility association ID differs from digest")


def associate_recording_visibility(
    request: RecordingVisibilityRequest,
    ephemerides: EphemerisReader,
    propagator: OrbitPropagator,
) -> RecordingVisibilityAssociation:
    """Return sparse visibility candidates without selecting satellite truth."""

    entries = _read_exact_catalog(request, ephemerides)
    by_norad = {entry.norad_id: entry for entry in entries}
    missing = sorted(set(request.candidate_norad_ids) - set(by_norad))
    if missing:
        raise RecordingVisibilityInputError(
            f"candidate NORAD IDs are absent from exact snapshot: {missing}"
        )
    instants = _sample_instants(
        request.ephemeris_link.recording_interval,
        request.policy.sample_fractions_ppm,
    )
    candidates = tuple(
        _candidate(request, by_norad[norad_id], instants, propagator)
        for norad_id in request.candidate_norad_ids
    )
    reasons = {"sparse-sampling-only", "weak-evidence-not-ground-truth"}
    status_reasons = {
        VisibilityStatus.ELEVATION_MARGIN_OVERLAP: "candidate-elevation-margin-overlap",
        VisibilityStatus.ELEMENT_EPOCH_OUTSIDE_BOUND: "candidate-element-age-bound",
        VisibilityStatus.PROPAGATION_ERROR: "candidate-propagation-error",
    }
    reasons.update(
        status_reasons[item.status]
        for item in candidates
        if item.status in status_reasons
    )
    request_digest = canonical_digest(request)
    associated = tuple(
        item.norad_id
        for item in candidates
        if item.status is VisibilityStatus.VISIBLE_AT_SAMPLE_WITH_MARGIN
    )
    possible = tuple(
        item.norad_id
        for item in candidates
        if item.status is VisibilityStatus.ELEVATION_MARGIN_OVERLAP
    )
    reason_codes = tuple(sorted(reasons))
    identity = _association_identity_values(
        schema=SchemaRef(ASSOCIATION_SCHEMA_ID, V0_1),
        request_digest=request_digest,
        algorithm_ref=request.algorithm_ref,
        recording_id=request.ephemeris_link.recording_id,
        recording_identity_digest=request.ephemeris_link.recording_identity_digest,
        recording_interval=request.ephemeris_link.recording_interval,
        source=request.snapshot.source,
        snapshot=request.snapshot,
        provenance_object_ref=request.provenance_object_ref,
        station=request.station,
        propagation=request.propagation,
        policy=request.policy,
        evidence_class="weak-ephemeris-visibility",
        ground_truth_eligible=False,
        associated_norad_ids=associated,
        possible_norad_ids=possible,
        candidates=candidates,
        reason_codes=reason_codes,
    )
    digest = canonical_digest(identity)
    return RecordingVisibilityAssociation(
        SchemaRef(ASSOCIATION_SCHEMA_ID, V0_1),
        f"visassoc_{digest.value[:32]}",
        digest,
        request_digest,
        request.algorithm_ref,
        request.ephemeris_link.recording_id,
        request.ephemeris_link.recording_identity_digest,
        request.ephemeris_link.recording_interval,
        request.snapshot.source,
        request.snapshot,
        request.provenance_object_ref,
        request.station,
        request.propagation,
        request.policy,
        "weak-ephemeris-visibility",
        False,
        associated,
        possible,
        candidates,
        reason_codes,
    )


def encode_recording_visibility_association(
    association: RecordingVisibilityAssociation,
) -> bytes:
    """Encode the immutable result as canonical JSON for content-addressed storage."""

    # Reconstructing validates identity before bytes become archive candidates.
    RecordingVisibilityAssociation(**association.__dict__)
    return canonical_json_bytes(association)


def _candidate(
    request: RecordingVisibilityRequest,
    entry: NormalizedTLE,
    instants: tuple[UtcNs, ...],
    propagator: OrbitPropagator,
) -> RecordingVisibilityCandidate:
    maximum_age_ns = max(
        abs(int(instant) - int(entry.epoch_utc_ns)) for instant in instants
    )
    if maximum_age_ns > request.policy.maximum_abs_element_age_s * _NS_PER_SECOND:
        return RecordingVisibilityCandidate(
            entry.norad_id,
            entry.name,
            entry.epoch_utc_ns,
            maximum_age_ns,
            VisibilityStatus.ELEMENT_EPOCH_OUTSIDE_BOUND,
            (),
        )
    samples: list[VisibilitySample] = []
    for instant in instants:
        try:
            state = propagator.propagate(
                request.ephemeris_link.snapshot_ref,
                request.station,
                request.propagation,
                entry.norad_id,
                instant,
            )
        except LookupError:
            samples.append(VisibilitySample(instant, None, None, None, "lookup-error"))
            continue
        if state.norad_id != entry.norad_id or state.utc_ns != instant:
            raise RecordingVisibilityInputError("propagator substituted state identity")
        if state.error_code is not None:
            samples.append(
                VisibilitySample(instant, None, None, None, state.error_code)
            )
            continue
        uncertainty = request.policy.elevation_uncertainty_deg
        samples.append(
            VisibilitySample(
                instant,
                state.elevation_deg,
                max(-90.0, state.elevation_deg - uncertainty),
                min(90.0, state.elevation_deg + uncertainty),
                None,
            )
        )
    successful = [sample for sample in samples if sample.propagation_error_code is None]
    if len(successful) != len(samples):
        status = VisibilityStatus.PROPAGATION_ERROR
    elif any(
        _required_elevation(sample.lower_elevation_deg)
        >= request.policy.minimum_elevation_deg
        for sample in successful
    ):
        status = VisibilityStatus.VISIBLE_AT_SAMPLE_WITH_MARGIN
    elif any(
        _required_elevation(sample.upper_elevation_deg)
        >= request.policy.minimum_elevation_deg
        for sample in successful
    ):
        status = VisibilityStatus.ELEVATION_MARGIN_OVERLAP
    else:
        status = VisibilityStatus.BELOW_GATE_AT_SAMPLES
    return RecordingVisibilityCandidate(
        entry.norad_id,
        entry.name,
        entry.epoch_utc_ns,
        maximum_age_ns,
        status,
        tuple(samples),
    )


def _required_elevation(value: float | None) -> float:
    if value is None:
        raise RecordingVisibilityInputError("successful sample lacks elevation")
    return value


def _read_exact_catalog(
    request: RecordingVisibilityRequest, ephemerides: EphemerisReader
) -> tuple[NormalizedTLE, ...]:
    ref = request.ephemeris_link.snapshot_ref
    with ephemerides.open(ref) as view:
        if view.ref != ref:
            raise RecordingVisibilityInputError(
                "ephemeris reader substituted snapshot identity"
            )
        payload = view.normalized_bytes()
    if not isinstance(payload, bytes):
        raise RecordingVisibilityInputError("normalized ephemeris must be bytes")
    normalized_ref = request.snapshot.normalized_object_ref
    if len(payload) != normalized_ref.byte_count or Digest.sha256(payload) != (
        normalized_ref.digest
    ):
        raise RecordingVisibilityInputError(
            "normalized ephemeris bytes or digest differ"
        )
    try:
        document = json.loads(payload)
        entries = decode_normalized_catalog(payload, request.snapshot.source)
    except (UnicodeDecodeError, json.JSONDecodeError, TLEFormatError) as error:
        raise RecordingVisibilityInputError(
            "normalized ephemeris catalog is invalid"
        ) from error
    if (
        not isinstance(document, dict)
        or document.get("scope") != request.snapshot.scope
    ):
        raise RecordingVisibilityInputError("normalized ephemeris scope differs")
    if not entries:
        raise RecordingVisibilityInputError("normalized ephemeris catalog is empty")
    ids = canonical_json_bytes([entry.norad_id for entry in entries])
    if (
        len(entries) != request.snapshot.satellite_count
        or Digest.sha256(ids) != request.snapshot.norad_id_set_digest
        or min(entry.epoch_utc_ns for entry in entries)
        != request.snapshot.element_epoch_min_utc_ns
        or max(entry.epoch_utc_ns for entry in entries)
        != request.snapshot.element_epoch_max_utc_ns
    ):
        raise RecordingVisibilityInputError(
            "parsed element identities or epochs differ from archived snapshot"
        )
    return entries


def _sample_instants(
    interval: RecordingInterval, fractions_ppm: tuple[int, ...]
) -> tuple[UtcNs, ...]:
    start = int(interval.started_utc_ns)
    duration = int(interval.finished_utc_ns) - start
    return tuple(
        UtcNs(value)
        for value in sorted(
            {start + duration * fraction // _PPM for fraction in fractions_ppm}
        )
    )


def _validate_temporal_selection(
    link: EphemerisLinkEvidence, snapshot: EphemerisSnapshot
) -> None:
    retrieved = snapshot.retrieved_at_utc_ns
    interval = link.recording_interval
    if retrieved > link.as_of_utc_ns:
        raise ValueError("snapshot retrieval lies after the link knowledge boundary")
    if (
        link.selection_policy is EphemerisSelectionPolicy.AVAILABLE_THEN
        and retrieved > interval.started_utc_ns
    ):
        raise ValueError("available-then snapshot was not available at recording start")
    if (
        link.selection_policy is EphemerisSelectionPolicy.FIRST_AFTER
        and retrieved <= interval.finished_utc_ns
    ):
        raise ValueError("first-after snapshot does not follow recording finish")


def _visibility_policy_ref(
    uncertainty_basis_ref: ArtifactRef,
    *,
    minimum_elevation_deg: float,
    elevation_uncertainty_deg: float,
    station_position_uncertainty_m: float,
    recording_time_uncertainty_ns: int,
    maximum_abs_element_age_s: int,
    sample_fractions_ppm: tuple[int, ...],
    maximum_candidates: int,
) -> ArtifactRef:
    identity = {
        "policy_version": "1",
        "minimum_elevation_deg": minimum_elevation_deg,
        "elevation_uncertainty_deg": elevation_uncertainty_deg,
        "station_position_uncertainty_m": station_position_uncertainty_m,
        "recording_time_uncertainty_ns": recording_time_uncertainty_ns,
        "maximum_abs_element_age_s": maximum_abs_element_age_s,
        "sample_fractions_ppm": sample_fractions_ppm,
        "maximum_candidates": maximum_candidates,
        "uncertainty_basis_ref": uncertainty_basis_ref,
        "ground_truth_eligible": False,
    }
    return ArtifactRef(
        "recording-visibility-policy-v1",
        canonical_digest(identity),
        SchemaRef(POLICY_SCHEMA_ID, V0_1),
    )


def _association_identity(
    value: RecordingVisibilityAssociation,
) -> dict[str, object]:
    return _association_identity_values(
        schema=value.schema,
        request_digest=value.request_digest,
        algorithm_ref=value.algorithm_ref,
        recording_id=value.recording_id,
        recording_identity_digest=value.recording_identity_digest,
        recording_interval=value.recording_interval,
        source=value.source,
        snapshot=value.snapshot,
        provenance_object_ref=value.provenance_object_ref,
        station=value.station,
        propagation=value.propagation,
        policy=value.policy,
        evidence_class=value.evidence_class,
        ground_truth_eligible=value.ground_truth_eligible,
        associated_norad_ids=value.associated_norad_ids,
        possible_norad_ids=value.possible_norad_ids,
        candidates=value.candidates,
        reason_codes=value.reason_codes,
    )


def _association_identity_values(
    *,
    schema: SchemaRef,
    request_digest: Digest,
    algorithm_ref: ArtifactRef,
    recording_id: RecordingId,
    recording_identity_digest: Digest,
    recording_interval: RecordingInterval,
    source: EphemerisSource,
    snapshot: EphemerisSnapshot,
    provenance_object_ref: ObjectRef,
    station: StationGeometrySnapshot,
    propagation: PropagationSpecification,
    policy: RecordingVisibilityPolicy,
    evidence_class: str,
    ground_truth_eligible: bool,
    associated_norad_ids: tuple[int, ...],
    possible_norad_ids: tuple[int, ...],
    candidates: tuple[RecordingVisibilityCandidate, ...],
    reason_codes: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema": schema,
        "request_digest": request_digest,
        "algorithm_ref": algorithm_ref,
        "recording_id": recording_id,
        "recording_identity_digest": recording_identity_digest,
        "recording_interval": recording_interval,
        "source": source.value,
        "snapshot": snapshot,
        "provenance_object_ref": provenance_object_ref,
        "station": station,
        "propagation": propagation,
        "policy": policy,
        "evidence_class": evidence_class,
        "ground_truth_eligible": ground_truth_eligible,
        "associated_norad_ids": associated_norad_ids,
        "possible_norad_ids": possible_norad_ids,
        "candidates": candidates,
        "reason_codes": reason_codes,
    }
