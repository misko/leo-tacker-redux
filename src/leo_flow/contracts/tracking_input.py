"""Immutable, locator-independent evidence for offline RF tracking inputs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ._validation import require_finite, require_positive, require_token, require_utc_ns
from .core import (
    V0_1,
    AnalysisRunId,
    ArtifactRef,
    DatasetSnapshotId,
    Digest,
    FeatureId,
    FeatureSetId,
    Provenance,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    StationId,
    UtcNs,
    canonical_digest,
)
from .ephemeris import RecordingEphemerisLink, RecordingInterval
from .features import Covariance
from .hardware import HardwareMetadataSnapshotRef, RecordingHardwareLink
from .storage import ObjectRef

RF_MEASUREMENT_BASIS = ("frequency_hz", "drift_hz_s")
RF_CALIBRATION_BASIS = ("frequency_bias_hz", "frequency_drift_hz_s")
RF_UNITS = ("Hz", "Hz/s")
TRACKING_INPUT_MEDIA_TYPE = "application/json"
TRACKING_INPUT_FORMAT_ID = "tracking-input-snapshot-v0.1"
MAX_TRACKING_INPUT_ENTRIES = 100_000
MAX_CALIBRATION_SOURCE_REFS = 64


class RfReferenceFrame(str, Enum):
    ABSOLUTE_RF = "absolute_rf"


@dataclass(frozen=True)
class DurableDatasetIdentity:
    """Exact mirror of the durable dataset ref without reversing dependencies."""

    snapshot_id: DatasetSnapshotId
    feature_membership_digest: Digest
    snapshot_digest: Digest


@dataclass(frozen=True)
class FeatureSetIdentity:
    """Exact feature identity with replaceable storage location excluded."""

    feature_set_id: FeatureSetId
    analysis_run_id: AnalysisRunId
    bundle_digest: Digest
    bundle_byte_count: int
    bundle_media_type: str
    bundle_format_id: str

    def __post_init__(self) -> None:
        require_positive(self.bundle_byte_count, "bundle_byte_count")
        if "/" not in self.bundle_media_type:
            raise ValueError("bundle_media_type must be a MIME type")
        require_token(self.bundle_format_id, "bundle_format_id")


@dataclass(frozen=True)
class AbsoluteRfMeasurementEvidence:
    feature_id: FeatureId
    recording_id: RecordingId
    receiver_chain_id: ReceiverChainId
    midpoint_utc_ns: UtcNs
    reference_frame: RfReferenceFrame
    value: tuple[float, float]
    basis: tuple[str, str]
    units: tuple[str, str]
    covariance: Covariance

    def __post_init__(self) -> None:
        require_utc_ns(self.midpoint_utc_ns, "midpoint_utc_ns")
        if self.reference_frame is not RfReferenceFrame.ABSOLUTE_RF:
            raise ValueError("tracking measurement must use the ABSOLUTE_RF frame")
        if self.basis != RF_MEASUREMENT_BASIS or self.units != RF_UNITS:
            raise ValueError("tracking measurement basis or units differ")
        for value in self.value:
            require_finite(value, "measurement value")
        _require_covariance(
            self.covariance,
            RF_MEASUREMENT_BASIS,
            RF_UNITS,
            "measurement covariance",
            positive_diagonal=True,
        )


@dataclass(frozen=True)
class ReceiverCalibrationEvidence:
    calibration_ref: ArtifactRef
    receiver_chain_id: ReceiverChainId
    hardware_snapshot_ref: HardwareMetadataSnapshotRef
    station_id: StationId
    validity: RecordingInterval
    value: tuple[float, float]
    basis: tuple[str, str]
    units: tuple[str, str]
    covariance: Covariance
    source_refs: tuple[ArtifactRef, ...]

    SCHEMA_ID = "org.leo-flow.receiver-rf-calibration-evidence"

    def __post_init__(self) -> None:
        if self.basis != RF_CALIBRATION_BASIS or self.units != RF_UNITS:
            raise ValueError("RF calibration basis or units differ")
        for value in self.value:
            require_finite(value, "calibration value")
        _require_covariance(
            self.covariance,
            RF_CALIBRATION_BASIS,
            RF_UNITS,
            "calibration covariance",
            positive_diagonal=True,
        )
        if not self.source_refs:
            raise ValueError("RF calibration requires immutable source references")
        if len(self.source_refs) > MAX_CALIBRATION_SOURCE_REFS:
            raise ValueError("RF calibration has too many source references")
        if self.source_refs != tuple(sorted(self.source_refs, key=_artifact_key)):
            raise ValueError("RF calibration source references are not canonical")
        if len({item.artifact_id for item in self.source_refs}) != len(
            self.source_refs
        ):
            raise ValueError("RF calibration source reference IDs are duplicated")
        if any(item.schema is None for item in self.source_refs):
            raise ValueError("RF calibration source references require schemas")
        expected = receiver_calibration_digest(
            self.receiver_chain_id,
            self.hardware_snapshot_ref,
            self.station_id,
            self.validity,
            self.value,
            self.basis,
            self.units,
            self.covariance,
            self.source_refs,
        )
        if (
            self.calibration_ref.schema != SchemaRef(self.SCHEMA_ID, V0_1)
            or self.calibration_ref.digest != expected
            or self.calibration_ref.artifact_id != f"rfcal_{expected.value[:32]}"
        ):
            raise ValueError("RF calibration reference differs from evidence")


def receiver_calibration_digest(
    receiver_chain_id: ReceiverChainId,
    hardware_snapshot_ref: HardwareMetadataSnapshotRef,
    station_id: StationId,
    validity: RecordingInterval,
    value: tuple[float, float],
    basis: tuple[str, str],
    units: tuple[str, str],
    covariance: Covariance,
    source_refs: tuple[ArtifactRef, ...],
) -> Digest:
    return canonical_digest(
        {
            "receiver_chain_id": receiver_chain_id,
            "hardware_snapshot_ref": hardware_snapshot_ref,
            "station_id": station_id,
            "validity": validity,
            "value": value,
            "basis": basis,
            "units": units,
            "covariance": covariance,
            "source_refs": source_refs,
        }
    )


@dataclass(frozen=True)
class PredictionCovarianceEvidence:
    policy_ref: ArtifactRef
    basis: tuple[str, str]
    units: tuple[str, str]
    covariance: Covariance

    def __post_init__(self) -> None:
        if self.policy_ref.schema is None:
            raise ValueError("prediction covariance policy requires a schema")
        if self.basis != RF_MEASUREMENT_BASIS or self.units != RF_UNITS:
            raise ValueError("prediction covariance basis or units differ")
        _require_covariance(
            self.covariance,
            RF_MEASUREMENT_BASIS,
            RF_UNITS,
            "prediction covariance",
            positive_diagonal=False,
        )


@dataclass(frozen=True)
class TrackingInputEntry:
    feature_set: FeatureSetIdentity
    recording_identity_digest: Digest
    recording_interval: RecordingInterval
    hardware_link: RecordingHardwareLink
    ephemeris_link: RecordingEphemerisLink
    measurement: AbsoluteRfMeasurementEvidence
    calibration: ReceiverCalibrationEvidence
    prediction: PredictionCovarianceEvidence

    def __post_init__(self) -> None:
        recording_id = self.measurement.recording_id
        if (
            self.hardware_link.recording_id != recording_id
            or self.ephemeris_link.recording_id != recording_id
        ):
            raise ValueError("tracking entry recording IDs differ")
        if (
            self.hardware_link.recording_identity_digest
            != self.recording_identity_digest
            or self.ephemeris_link.recording_identity_digest
            != self.recording_identity_digest
        ):
            raise ValueError("tracking entry recording identity digests differ")
        if self.ephemeris_link.recording_interval != self.recording_interval:
            raise ValueError("tracking entry recording intervals differ")
        if self.ephemeris_link.selection.policy_ref.schema is None:
            raise ValueError("ephemeris selection policy requires a schema")
        midpoint = self.measurement.midpoint_utc_ns
        if not (
            self.recording_interval.started_utc_ns
            <= midpoint
            < self.recording_interval.finished_utc_ns
        ):
            raise ValueError("measurement midpoint lies outside half-open recording")
        if not (
            self.calibration.validity.started_utc_ns
            <= midpoint
            < self.calibration.validity.finished_utc_ns
        ):
            raise ValueError("measurement midpoint lies outside calibration validity")
        if self.measurement.receiver_chain_id != self.calibration.receiver_chain_id:
            raise ValueError("measurement and calibration receiver chains differ")
        if (
            self.hardware_link.hardware_snapshot_ref
            != self.calibration.hardware_snapshot_ref
        ):
            raise ValueError("hardware link and calibration snapshot differ")


@dataclass(frozen=True)
class TrackingInputSnapshot:
    schema: SchemaRef
    snapshot_id: str
    durable_dataset: DurableDatasetIdentity
    builder_ref: ArtifactRef
    selector_ref: ArtifactRef
    provenance: Provenance
    entries: tuple[TrackingInputEntry, ...]
    membership_digest: Digest
    snapshot_digest: Digest

    SCHEMA_ID = "org.leo-flow.tracking-input-snapshot"

    def __post_init__(self) -> None:
        if self.schema != SchemaRef(self.SCHEMA_ID, V0_1):
            raise ValueError("unsupported tracking input snapshot schema")
        if self.builder_ref.schema is None or self.selector_ref.schema is None:
            raise ValueError("tracking builder and selector require schemas")
        if not self.entries:
            raise ValueError("tracking input snapshot cannot be empty")
        if len(self.entries) > MAX_TRACKING_INPUT_ENTRIES:
            raise ValueError("tracking input snapshot has too many entries")
        if self.entries != tuple(sorted(self.entries, key=tracking_entry_key)):
            raise ValueError("tracking input entries are not in canonical order")
        keys = tuple(
            (item.feature_set.feature_set_id, item.measurement.feature_id)
            for item in self.entries
        )
        if len(set(keys)) != len(keys):
            raise ValueError("tracking input feature identities are duplicated")
        expected_membership = tracking_input_membership_digest(self.entries)
        if self.membership_digest != expected_membership:
            raise ValueError("tracking input membership digest differs")
        if self.provenance.normalized_config_digest != self.selector_ref.digest:
            raise ValueError("tracking selector provenance differs")
        if self.provenance.input_digests != (
            self.durable_dataset.snapshot_digest,
            self.membership_digest,
        ):
            raise ValueError("tracking input provenance does not close inputs")
        if (
            not self.provenance.dependency_digests
            or self.provenance.dependency_digests[0] != self.builder_ref.digest
        ):
            raise ValueError("tracking input provenance does not identify builder")
        expected_snapshot = tracking_input_snapshot_digest(
            self.schema,
            self.durable_dataset,
            self.builder_ref,
            self.selector_ref,
            self.provenance,
            self.entries,
            self.membership_digest,
        )
        if self.snapshot_digest != expected_snapshot:
            raise ValueError("tracking input snapshot digest differs")
        if (
            re.fullmatch(r"trackinput_[0-9a-f]{32}", self.snapshot_id) is None
            or self.snapshot_id != f"trackinput_{expected_snapshot.value[:32]}"
        ):
            raise ValueError("tracking input snapshot ID differs from content")


@dataclass(frozen=True)
class TrackingInputSnapshotRef:
    snapshot_id: str
    snapshot_digest: Digest
    membership_digest: Digest
    bundle_ref: ObjectRef

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"trackinput_[0-9a-f]{32}", self.snapshot_id) is None
            or self.snapshot_id != f"trackinput_{self.snapshot_digest.value[:32]}"
        ):
            raise ValueError("tracking input snapshot ref ID differs")
        if self.bundle_ref.byte_count == 0:
            raise ValueError("tracking input bundle cannot be empty")
        if (
            self.bundle_ref.media_type != TRACKING_INPUT_MEDIA_TYPE
            or self.bundle_ref.format_id != TRACKING_INPUT_FORMAT_ID
        ):
            raise ValueError("tracking input bundle metadata differs")

    def identity_digest(self) -> Digest:
        return canonical_digest(
            {
                "snapshot_id": self.snapshot_id,
                "snapshot_digest": self.snapshot_digest,
                "membership_digest": self.membership_digest,
                "bundle": {
                    "digest": self.bundle_ref.digest,
                    "byte_count": self.bundle_ref.byte_count,
                    "media_type": self.bundle_ref.media_type,
                    "format_id": self.bundle_ref.format_id,
                },
            }
        )


def tracking_entry_key(entry: TrackingInputEntry) -> tuple[int, str, str]:
    return (
        int(entry.measurement.midpoint_utc_ns),
        str(entry.feature_set.feature_set_id),
        str(entry.measurement.feature_id),
    )


def tracking_input_membership_digest(entries: tuple[TrackingInputEntry, ...]) -> Digest:
    return canonical_digest(entries)


def tracking_input_snapshot_digest(
    schema: SchemaRef,
    durable_dataset: DurableDatasetIdentity,
    builder_ref: ArtifactRef,
    selector_ref: ArtifactRef,
    provenance: Provenance,
    entries: tuple[TrackingInputEntry, ...],
    membership_digest: Digest,
) -> Digest:
    return canonical_digest(
        {
            "schema": schema,
            "durable_dataset": durable_dataset,
            "builder_ref": builder_ref,
            "selector_ref": selector_ref,
            "provenance": provenance,
            "entries": entries,
            "membership_digest": membership_digest,
        }
    )


def _artifact_key(ref: ArtifactRef) -> tuple[str, str, str]:
    schema = (
        "" if ref.schema is None else f"{ref.schema.schema_id}/{ref.schema.version}"
    )
    return ref.artifact_id, str(ref.digest), schema


def _require_covariance(
    covariance: Covariance,
    basis: tuple[str, str],
    units: tuple[str, str],
    name: str,
    *,
    positive_diagonal: bool,
) -> None:
    if covariance.basis != basis or covariance.units != units:
        raise ValueError(f"{name} basis or units differ")
    if len(covariance.values) != 2 or any(len(row) != 2 for row in covariance.values):
        raise ValueError(f"{name} must be a full 2x2 matrix")
    if positive_diagonal and (
        covariance.values[0][0] <= 0.0 or covariance.values[1][1] <= 0.0
    ):
        raise ValueError(f"{name} diagonal must be positive")
