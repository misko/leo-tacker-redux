"""Strict bounded canonical codec for joint RF nuisance model outputs."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import NoReturn

from leo_flow.contracts.core import (
    ArtifactRef,
    DatasetSnapshotId,
    Digest,
    DigestAlgorithm,
    EphemerisSnapshotId,
    HardwareSnapshotId,
    ModelRunId,
    ModelSnapshotId,
    Provenance,
    ReceiverChainId,
    SchemaRef,
    SchemaVersion,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import EphemerisSource, RecordingInterval
from leo_flow.contracts.features import Covariance
from leo_flow.contracts.hardware import HardwareMetadataSnapshotRef
from leo_flow.contracts.tracking_input import (
    TRACKING_INPUT_FORMAT_ID,
    TRACKING_INPUT_MEDIA_TYPE,
    DurableDatasetIdentity,
    TrackingInputSnapshotIdentity,
)
from leo_flow.contracts.tracking_model_output import (
    MAX_ASSOCIATION_OUTCOMES,
    MAX_PARAMETER_BLOCKS,
    MAX_WARNINGS,
    AcceptedAssociationEvidence,
    CarrierAssociationCandidate,
    ReceiverLnbFrequencyEstimate,
    RejectedAssociationEvidence,
    SatelliteCarrierResidualEstimate,
    TrackingCalibrationEvidence,
    TrackingEphemerisEvidence,
    TrackingHardwareEvidence,
    TrackingModelEvidence,
    TrackingModelSnapshotBundle,
)

MAX_TRACKING_MODEL_SNAPSHOT_BYTES = 64 * 1024 * 1024
TRACKING_MODEL_SNAPSHOT_MEDIA_TYPE = "application/json"
TRACKING_MODEL_SNAPSHOT_FORMAT_ID = "tracking-model-snapshot-bundle-v0.1"


class MalformedTrackingModelSnapshotError(ValueError):
    """Tracking model output is oversized, noncanonical, or invalid."""


def encode_tracking_model_snapshot(bundle: TrackingModelSnapshotBundle) -> bytes:
    payload = canonical_json_bytes(bundle)
    if len(payload) > MAX_TRACKING_MODEL_SNAPSHOT_BYTES:
        raise MalformedTrackingModelSnapshotError(
            "tracking model snapshot exceeds size limit"
        )
    return payload


def decode_tracking_model_snapshot(data: bytes) -> TrackingModelSnapshotBundle:
    if len(data) > MAX_TRACKING_MODEL_SNAPSHOT_BYTES:
        raise MalformedTrackingModelSnapshotError(
            "tracking model snapshot exceeds size limit"
        )
    try:
        document = json.loads(data, object_pairs_hook=_unique_object)
        if canonical_json_bytes(document) != data:
            _bad("tracking model snapshot bytes are not canonical JSON")
        root = _object(document, "root")
        _keys(
            root,
            {
                "schema",
                "model_snapshot_id",
                "model_run_id",
                "evidence",
                "provenance",
                "receiver_lnb_estimates",
                "satellite_carrier_residual_estimates",
                "joint_covariance",
                "accepted_associations",
                "rejected_associations",
                "warnings",
            },
            "root",
        )
        receivers = _array(root["receiver_lnb_estimates"], "receiver_lnb_estimates")
        satellites = _array(
            root["satellite_carrier_residual_estimates"],
            "satellite_carrier_residual_estimates",
        )
        accepted = _array(root["accepted_associations"], "accepted_associations")
        rejected = _array(root["rejected_associations"], "rejected_associations")
        warnings = _array(root["warnings"], "warnings")
        if not 0 < len(receivers) + len(satellites) <= MAX_PARAMETER_BLOCKS:
            _bad("tracking model parameter block count is invalid")
        if len(accepted) + len(rejected) > MAX_ASSOCIATION_OUTCOMES:
            _bad("tracking model association count exceeds bound")
        if not 0 < len(warnings) <= MAX_WARNINGS:
            _bad("tracking model warning count is invalid")
        return TrackingModelSnapshotBundle(
            schema=_schema(root["schema"], "schema"),
            model_snapshot_id=ModelSnapshotId(
                _string(root["model_snapshot_id"], "model_snapshot_id")
            ),
            model_run_id=ModelRunId(_string(root["model_run_id"], "model_run_id")),
            evidence=_evidence(root["evidence"]),
            provenance=_provenance(root["provenance"]),
            receiver_lnb_estimates=tuple(
                _receiver(item, index) for index, item in enumerate(receivers)
            ),
            satellite_carrier_residual_estimates=tuple(
                _satellite(item, index) for index, item in enumerate(satellites)
            ),
            joint_covariance=_covariance(root["joint_covariance"], "joint_covariance"),
            accepted_associations=tuple(
                _accepted(item, index) for index, item in enumerate(accepted)
            ),
            rejected_associations=tuple(
                _rejected(item, index) for index, item in enumerate(rejected)
            ),
            warnings=tuple(
                _string(item, f"warnings[{index}]")
                for index, item in enumerate(warnings)
            ),
        )
    except MalformedTrackingModelSnapshotError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedTrackingModelSnapshotError(str(error)) from error


def _evidence(value: object) -> TrackingModelEvidence:
    item = _object(value, "evidence")
    _keys(
        item,
        {
            "tracking_input_identity",
            "durable_dataset",
            "ordered_entry_count",
            "ordered_entry_digest",
            "hardware",
            "calibrations",
            "ephemerides",
            "carrier_hypothesis_refs",
            "prediction_policy_refs",
            "algorithm_ref",
            "config_ref",
            "propagator_ref",
            "gravity_model_ref",
            "time_scale_ref",
            "earth_orientation_ref",
            "error_policy_ref",
        },
        "evidence",
    )
    hardware = _array(item["hardware"], "evidence.hardware")
    calibrations = _array(item["calibrations"], "evidence.calibrations")
    ephemerides = _array(item["ephemerides"], "evidence.ephemerides")
    carriers = _array(
        item["carrier_hypothesis_refs"], "evidence.carrier_hypothesis_refs"
    )
    predictions = _array(
        item["prediction_policy_refs"], "evidence.prediction_policy_refs"
    )
    for name, values in (
        ("hardware", hardware),
        ("calibrations", calibrations),
        ("ephemerides", ephemerides),
        ("carrier hypotheses", carriers),
        ("prediction policies", predictions),
    ):
        if not values or len(values) > MAX_PARAMETER_BLOCKS:
            _bad(f"evidence {name} count is invalid")
    return TrackingModelEvidence(
        tracking_input_identity=_tracking_identity(item["tracking_input_identity"]),
        durable_dataset=_dataset(item["durable_dataset"]),
        ordered_entry_count=_integer(
            item["ordered_entry_count"], "evidence.ordered_entry_count"
        ),
        ordered_entry_digest=_digest(
            item["ordered_entry_digest"], "evidence.ordered_entry_digest"
        ),
        hardware=tuple(_hardware(entry, index) for index, entry in enumerate(hardware)),
        calibrations=tuple(
            _calibration(entry, index) for index, entry in enumerate(calibrations)
        ),
        ephemerides=tuple(
            _ephemeris(entry, index) for index, entry in enumerate(ephemerides)
        ),
        carrier_hypothesis_refs=tuple(
            _artifact(entry, f"carrier_hypothesis_refs[{index}]")
            for index, entry in enumerate(carriers)
        ),
        prediction_policy_refs=tuple(
            _artifact(entry, f"prediction_policy_refs[{index}]")
            for index, entry in enumerate(predictions)
        ),
        algorithm_ref=_artifact(item["algorithm_ref"], "algorithm_ref"),
        config_ref=_artifact(item["config_ref"], "config_ref"),
        propagator_ref=_artifact(item["propagator_ref"], "propagator_ref"),
        gravity_model_ref=_artifact(item["gravity_model_ref"], "gravity_model_ref"),
        time_scale_ref=_artifact(item["time_scale_ref"], "time_scale_ref"),
        earth_orientation_ref=_artifact(
            item["earth_orientation_ref"], "earth_orientation_ref"
        ),
        error_policy_ref=_artifact(item["error_policy_ref"], "error_policy_ref"),
    )


def _tracking_identity(value: object) -> TrackingInputSnapshotIdentity:
    item = _object(value, "tracking_input_identity")
    _keys(
        item,
        {
            "snapshot_id",
            "snapshot_digest",
            "membership_digest",
            "bundle_digest",
            "bundle_byte_count",
            "bundle_media_type",
            "bundle_format_id",
        },
        "tracking_input_identity",
    )
    return TrackingInputSnapshotIdentity(
        _string(item["snapshot_id"], "tracking_input.snapshot_id"),
        _digest(item["snapshot_digest"], "tracking_input.snapshot_digest"),
        _digest(item["membership_digest"], "tracking_input.membership_digest"),
        _digest(item["bundle_digest"], "tracking_input.bundle_digest"),
        _integer(item["bundle_byte_count"], "tracking_input.bundle_byte_count"),
        _exact_string(
            item["bundle_media_type"],
            TRACKING_INPUT_MEDIA_TYPE,
            "tracking_input.bundle_media_type",
        ),
        _exact_string(
            item["bundle_format_id"],
            TRACKING_INPUT_FORMAT_ID,
            "tracking_input.bundle_format_id",
        ),
    )


def _dataset(value: object) -> DurableDatasetIdentity:
    item = _object(value, "durable_dataset")
    _keys(
        item,
        {"snapshot_id", "feature_membership_digest", "snapshot_digest"},
        "durable_dataset",
    )
    return DurableDatasetIdentity(
        DatasetSnapshotId(_string(item["snapshot_id"], "dataset.snapshot_id")),
        _digest(item["feature_membership_digest"], "dataset.feature_membership_digest"),
        _digest(item["snapshot_digest"], "dataset.snapshot_digest"),
    )


def _hardware(value: object, index: int) -> TrackingHardwareEvidence:
    name = f"hardware[{index}]"
    item = _object(value, name)
    _keys(item, {"snapshot_ref", "link_digests"}, name)
    snapshot = _object(item["snapshot_ref"], f"{name}.snapshot_ref")
    _keys(snapshot, {"snapshot_id", "digest"}, f"{name}.snapshot_ref")
    links = _array(item["link_digests"], f"{name}.link_digests")
    if not links or len(links) > MAX_ASSOCIATION_OUTCOMES:
        _bad(f"{name}.link_digests count is invalid")
    return TrackingHardwareEvidence(
        HardwareMetadataSnapshotRef(
            HardwareSnapshotId(_string(snapshot["snapshot_id"], f"{name}.snapshot_id")),
            _digest(snapshot["digest"], f"{name}.snapshot_digest"),
        ),
        tuple(
            _digest(link, f"{name}.link_digests[{link_index}]")
            for link_index, link in enumerate(links)
        ),
    )


def _calibration(value: object, index: int) -> TrackingCalibrationEvidence:
    name = f"calibrations[{index}]"
    item = _object(value, name)
    _keys(item, {"calibration_ref", "source_digests"}, name)
    sources = _array(item["source_digests"], f"{name}.source_digests")
    if not sources or len(sources) > MAX_PARAMETER_BLOCKS:
        _bad(f"{name}.source_digests count is invalid")
    return TrackingCalibrationEvidence(
        _artifact(item["calibration_ref"], f"{name}.calibration_ref"),
        tuple(
            _digest(source, f"{name}.source_digests[{source_index}]")
            for source_index, source in enumerate(sources)
        ),
    )


def _ephemeris(value: object, index: int) -> TrackingEphemerisEvidence:
    name = f"ephemerides[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {
            "source",
            "snapshot_id",
            "raw_digest",
            "normalized_digest",
            "link_digests",
            "selection_policy_digests",
        },
        name,
    )
    links = _array(item["link_digests"], f"{name}.link_digests")
    policies = _array(
        item["selection_policy_digests"], f"{name}.selection_policy_digests"
    )
    if not links or len(links) > MAX_ASSOCIATION_OUTCOMES:
        _bad(f"{name}.link_digests count is invalid")
    if not policies or len(policies) > MAX_PARAMETER_BLOCKS:
        _bad(f"{name}.selection_policy_digests count is invalid")
    return TrackingEphemerisEvidence(
        EphemerisSource(_string(item["source"], f"{name}.source")),
        EphemerisSnapshotId(_string(item["snapshot_id"], f"{name}.snapshot_id")),
        _digest(item["raw_digest"], f"{name}.raw_digest"),
        _digest(item["normalized_digest"], f"{name}.normalized_digest"),
        tuple(
            _digest(link, f"{name}.link_digests[{link_index}]")
            for link_index, link in enumerate(links)
        ),
        tuple(
            _digest(policy, f"{name}.selection_policy_digests[{policy_index}]")
            for policy_index, policy in enumerate(policies)
        ),
    )


def _receiver(value: object, index: int) -> ReceiverLnbFrequencyEstimate:
    name = f"receiver_lnb_estimates[{index}]"
    item = _estimate_object(value, name)
    hardware = _object(item["hardware_snapshot_ref"], f"{name}.hardware_snapshot_ref")
    _keys(
        hardware,
        {"snapshot_id", "digest"},
        f"{name}.hardware_snapshot_ref",
    )
    return ReceiverLnbFrequencyEstimate(
        ReceiverChainId(
            _string(item["receiver_chain_id"], f"{name}.receiver_chain_id")
        ),
        HardwareMetadataSnapshotRef(
            HardwareSnapshotId(
                _string(hardware["snapshot_id"], f"{name}.hardware_snapshot_id")
            ),
            _digest(hardware["digest"], f"{name}.hardware_snapshot_digest"),
        ),
        _interval(item["validity"], f"{name}.validity"),
        _integer(item["observation_count"], f"{name}.observation_count"),
        _vector2(item["value"], f"{name}.value"),
        _string_pair(item["basis"], f"{name}.basis"),
        _string_pair(item["units"], f"{name}.units"),
        _covariance(item["covariance"], f"{name}.covariance"),
    )


def _satellite(value: object, index: int) -> SatelliteCarrierResidualEstimate:
    name = f"satellite_carrier_residual_estimates[{index}]"
    item = _estimate_object(value, name, receiver=False)
    return SatelliteCarrierResidualEstimate(
        _integer(item["norad_id"], f"{name}.norad_id"),
        _artifact(item["carrier_hypothesis_ref"], f"{name}.carrier_hypothesis_ref"),
        _interval(item["validity"], f"{name}.validity"),
        _integer(item["observation_count"], f"{name}.observation_count"),
        _vector2(item["value"], f"{name}.value"),
        _string_pair(item["basis"], f"{name}.basis"),
        _string_pair(item["units"], f"{name}.units"),
        _covariance(item["covariance"], f"{name}.covariance"),
    )


def _estimate_object(
    value: object, name: str, *, receiver: bool = True
) -> Mapping[str, object]:
    item = _object(value, name)
    keys = {
        "validity",
        "observation_count",
        "value",
        "basis",
        "units",
        "covariance",
    }
    keys.update(
        {"receiver_chain_id", "hardware_snapshot_ref"}
        if receiver
        else {"norad_id", "carrier_hypothesis_ref"}
    )
    _keys(item, keys, name)
    return item


def _accepted(value: object, index: int) -> AcceptedAssociationEvidence:
    name = f"accepted_associations[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {
            "entry_index",
            "observed_utc_ns",
            "receiver_chain_id",
            "selected_candidate",
            "candidates",
            "decision_digest",
        },
        name,
    )
    candidates = _array(item["candidates"], f"{name}.candidates")
    if not candidates or len(candidates) > MAX_PARAMETER_BLOCKS:
        _bad(f"{name}.candidates count is invalid")
    return AcceptedAssociationEvidence(
        _integer(item["entry_index"], f"{name}.entry_index"),
        UtcNs(_integer(item["observed_utc_ns"], f"{name}.observed_utc_ns")),
        ReceiverChainId(
            _string(item["receiver_chain_id"], f"{name}.receiver_chain_id")
        ),
        _carrier_candidate(item["selected_candidate"], f"{name}.selected_candidate"),
        tuple(
            _carrier_candidate(candidate, f"{name}.candidates[{candidate_index}]")
            for candidate_index, candidate in enumerate(candidates)
        ),
        _digest(item["decision_digest"], f"{name}.decision_digest"),
    )


def _rejected(value: object, index: int) -> RejectedAssociationEvidence:
    name = f"rejected_associations[{index}]"
    item = _object(value, name)
    _keys(
        item,
        {
            "entry_index",
            "observed_utc_ns",
            "reason_code",
            "candidates",
            "decision_digest",
        },
        name,
    )
    candidates = _array(item["candidates"], f"{name}.candidates")
    if len(candidates) > MAX_PARAMETER_BLOCKS:
        _bad(f"{name}.candidates count exceeds bound")
    return RejectedAssociationEvidence(
        _integer(item["entry_index"], f"{name}.entry_index"),
        UtcNs(_integer(item["observed_utc_ns"], f"{name}.observed_utc_ns")),
        _string(item["reason_code"], f"{name}.reason_code"),
        tuple(
            _carrier_candidate(candidate, f"{name}.candidates[{candidate_index}]")
            for candidate_index, candidate in enumerate(candidates)
        ),
        _digest(item["decision_digest"], f"{name}.decision_digest"),
    )


def _carrier_candidate(value: object, name: str) -> CarrierAssociationCandidate:
    item = _object(value, name)
    _keys(item, {"norad_id", "carrier_hypothesis_ref"}, name)
    return CarrierAssociationCandidate(
        _integer(item["norad_id"], f"{name}.norad_id"),
        _artifact(item["carrier_hypothesis_ref"], f"{name}.carrier_hypothesis_ref"),
    )


def _artifact(value: object, name: str) -> ArtifactRef:
    item = _object(value, name)
    _keys(item, {"artifact_id", "digest", "schema"}, name)
    return ArtifactRef(
        _string(item["artifact_id"], f"{name}.artifact_id"),
        _digest(item["digest"], f"{name}.digest"),
        _schema(item["schema"], f"{name}.schema"),
    )


def _provenance(value: object) -> Provenance:
    item = _object(value, "provenance")
    _keys(
        item,
        {
            "producer_name",
            "producer_version",
            "git_commit",
            "environment_digest",
            "normalized_config_digest",
            "input_digests",
            "dependency_digests",
            "started_utc_ns",
            "completed_utc_ns",
            "host_class",
        },
        "provenance",
    )
    inputs = _array(item["input_digests"], "provenance.input_digests")
    dependencies = _array(item["dependency_digests"], "provenance.dependency_digests")
    if not inputs or len(inputs) > 100_000 or len(dependencies) > 100_000:
        _bad("provenance digest count is invalid")
    return Provenance(
        _string(item["producer_name"], "provenance.producer_name"),
        _string(item["producer_version"], "provenance.producer_version"),
        _string(item["git_commit"], "provenance.git_commit"),
        _digest(item["environment_digest"], "provenance.environment_digest"),
        _digest(
            item["normalized_config_digest"],
            "provenance.normalized_config_digest",
        ),
        tuple(
            _digest(entry, f"provenance.input_digests[{index}]")
            for index, entry in enumerate(inputs)
        ),
        tuple(
            _digest(entry, f"provenance.dependency_digests[{index}]")
            for index, entry in enumerate(dependencies)
        ),
        UtcNs(_integer(item["started_utc_ns"], "provenance.started_utc_ns")),
        UtcNs(_integer(item["completed_utc_ns"], "provenance.completed_utc_ns")),
        _string(item["host_class"], "provenance.host_class"),
    )


def _covariance(value: object, name: str) -> Covariance:
    item = _object(value, name)
    _keys(item, {"basis", "units", "values", "psd_tolerance"}, name)
    basis = _array(item["basis"], f"{name}.basis")
    units = _array(item["units"], f"{name}.units")
    rows = _array(item["values"], f"{name}.values")
    if not basis or len(basis) > MAX_PARAMETER_BLOCKS * 2 or len(rows) != len(basis):
        _bad(f"{name} dimension is invalid")
    return Covariance(
        tuple(
            _string(entry, f"{name}.basis[{index}]")
            for index, entry in enumerate(basis)
        ),
        tuple(
            _string(entry, f"{name}.units[{index}]")
            for index, entry in enumerate(units)
        ),
        tuple(
            tuple(
                _number(cell, f"{name}.values[{row_index}][{column_index}]")
                for column_index, cell in enumerate(
                    _array(row, f"{name}.values[{row_index}]")
                )
            )
            for row_index, row in enumerate(rows)
        ),
        _number(item["psd_tolerance"], f"{name}.psd_tolerance"),
    )


def _interval(value: object, name: str) -> RecordingInterval:
    item = _object(value, name)
    _keys(item, {"started_utc_ns", "finished_utc_ns"}, name)
    return RecordingInterval(
        UtcNs(_integer(item["started_utc_ns"], f"{name}.started_utc_ns")),
        UtcNs(_integer(item["finished_utc_ns"], f"{name}.finished_utc_ns")),
    )


def _schema(value: object, name: str) -> SchemaRef:
    item = _object(value, name)
    _keys(item, {"schema_id", "version"}, name)
    version = _object(item["version"], f"{name}.version")
    _keys(version, {"major", "minor"}, f"{name}.version")
    return SchemaRef(
        _string(item["schema_id"], f"{name}.schema_id"),
        SchemaVersion(
            _integer(version["major"], f"{name}.version.major"),
            _integer(version["minor"], f"{name}.version.minor"),
        ),
    )


def _digest(value: object, name: str) -> Digest:
    item = _object(value, name)
    _keys(item, {"algorithm", "value"}, name)
    return Digest(
        DigestAlgorithm(_string(item["algorithm"], f"{name}.algorithm")),
        _string(item["value"], f"{name}.value"),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _bad(f"duplicate object key: {key}")
        result[key] = value
    return result


def _keys(item: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(item) != expected:
        _bad(f"{name} fields differ from schema")


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _bad(f"{name} must be an object")
    return value


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        _bad(f"{name} must be an array")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        _bad(f"{name} must be a string")
    return value


def _exact_string(value: object, expected: str, name: str) -> str:
    result = _string(value, name)
    if result != expected:
        _bad(f"{name} differs")
    return result


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _bad(f"{name} must be an integer")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _bad(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        _bad(f"{name} must be finite")
    return result


def _vector2(value: object, name: str) -> tuple[float, float]:
    items = _array(value, name)
    if len(items) != 2:
        _bad(f"{name} must contain two values")
    return _number(items[0], f"{name}[0]"), _number(items[1], f"{name}[1]")


def _string_pair(value: object, name: str) -> tuple[str, str]:
    items = _array(value, name)
    if len(items) != 2:
        _bad(f"{name} must contain two values")
    return _string(items[0], f"{name}[0]"), _string(items[1], f"{name}[1]")


def _integer_tuple(value: object, name: str) -> tuple[int, ...]:
    items = _array(value, name)
    if len(items) > MAX_PARAMETER_BLOCKS:
        _bad(f"{name} exceeds bound")
    return tuple(_integer(item, f"{name}[{index}]") for index, item in enumerate(items))


def _bad(message: str) -> NoReturn:
    raise MalformedTrackingModelSnapshotError(message)
