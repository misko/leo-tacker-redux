from __future__ import annotations

import json
from dataclasses import replace

import pytest

from leo_flow.analysis.model.tracking_model_codec import (
    MAX_TRACKING_MODEL_SNAPSHOT_BYTES,
    MalformedTrackingModelSnapshotError,
    decode_tracking_model_snapshot,
    encode_tracking_model_snapshot,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    DatasetSnapshotId,
    Digest,
    EphemerisSnapshotId,
    HardwareSnapshotId,
    ModelRunId,
    ModelSnapshotId,
    Provenance,
    ReceiverChainId,
    SchemaRef,
    UtcNs,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import EphemerisSource, RecordingInterval
from leo_flow.contracts.features import Covariance
from leo_flow.contracts.hardware import HardwareMetadataSnapshotRef
from leo_flow.contracts.tracking_input import (
    RF_CALIBRATION_BASIS,
    RF_UNITS,
    TRACKING_INPUT_FORMAT_ID,
    TRACKING_INPUT_MEDIA_TYPE,
    DurableDatasetIdentity,
    TrackingInputSnapshotIdentity,
)
from leo_flow.contracts.tracking_model_output import (
    JOINT_COVARIANCE_PSD_TOLERANCE,
    NOT_ORBIT_STATE_WARNING,
    SATELLITE_CARRIER_RESIDUAL_BASIS,
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
    tracking_model_dependency_digests,
    tracking_model_input_digests,
    tracking_model_snapshot_digest,
)


def _digest(label: str) -> Digest:
    return Digest.sha256(label.encode())


def _artifact(label: str) -> ArtifactRef:
    return ArtifactRef(label, _digest(label), SchemaRef(f"org.leo-flow.{label}"))


def _carrier(label: str = "a") -> ArtifactRef:
    return _artifact(f"carrier-hypothesis-{label}")


def _evidence(**changes: object) -> TrackingModelEvidence:
    snapshot_digest = _digest("tracking-input-snapshot")
    membership = _digest("ordered-entries")
    values: dict[str, object] = {
        "tracking_input_identity": TrackingInputSnapshotIdentity(
            f"trackinput_{snapshot_digest.value[:32]}",
            snapshot_digest,
            membership,
            _digest("tracking-input-bundle"),
            8_192,
            TRACKING_INPUT_MEDIA_TYPE,
            TRACKING_INPUT_FORMAT_ID,
        ),
        "durable_dataset": DurableDatasetIdentity(
            DatasetSnapshotId("dataset_tracking_output"),
            _digest("dataset-membership"),
            _digest("dataset-snapshot"),
        ),
        "ordered_entry_count": 2,
        "ordered_entry_digest": membership,
        "hardware": (
            TrackingHardwareEvidence(
                HardwareMetadataSnapshotRef(
                    HardwareSnapshotId("hw_tracking_output"),
                    _digest("hardware-snapshot"),
                ),
                tuple(
                    sorted(
                        (_digest("hardware-link-a"), _digest("hardware-link-b")),
                        key=str,
                    )
                ),
            ),
        ),
        "calibrations": (
            TrackingCalibrationEvidence(
                _artifact("calibration"), (_digest("calibration-source"),)
            ),
        ),
        "ephemerides": (
            TrackingEphemerisEvidence(
                EphemerisSource.SPACE_TRACK,
                EphemerisSnapshotId("eph_tracking_output"),
                _digest("tle-raw"),
                _digest("tle-normalized"),
                tuple(
                    sorted(
                        (_digest("ephemeris-link-a"), _digest("ephemeris-link-b")),
                        key=str,
                    )
                ),
                tuple(
                    sorted(
                        (
                            _digest("ephemeris-selection-policy-a"),
                            _digest("ephemeris-selection-policy-b"),
                        ),
                        key=str,
                    )
                ),
            ),
        ),
        "carrier_hypothesis_refs": (_carrier(),),
        "prediction_policy_refs": (_artifact("prediction-policy"),),
        "algorithm_ref": _artifact("joint-rf-nuisance-algorithm"),
        "config_ref": _artifact("joint-rf-nuisance-config"),
        "propagator_ref": _artifact("propagator"),
        "gravity_model_ref": _artifact("gravity-model"),
        "time_scale_ref": _artifact("time-scale"),
        "earth_orientation_ref": _artifact("earth-orientation"),
        "error_policy_ref": _artifact("propagation-error-policy"),
    }
    values.update(changes)
    return TrackingModelEvidence(**values)  # type: ignore[arg-type]


def _receiver() -> ReceiverLnbFrequencyEstimate:
    return ReceiverLnbFrequencyEstimate(
        ReceiverChainId("rx_tracking"),
        HardwareMetadataSnapshotRef(
            HardwareSnapshotId("hw_tracking_output"), _digest("hardware-snapshot")
        ),
        RecordingInterval(UtcNs(100), UtcNs(300)),
        1,
        (120.0, -0.2),
        RF_CALIBRATION_BASIS,
        RF_UNITS,
        Covariance(
            RF_CALIBRATION_BASIS,
            RF_UNITS,
            ((4.0, 0.5), (0.5, 1.0)),
            JOINT_COVARIANCE_PSD_TOLERANCE,
        ),
    )


def _satellite(carrier: str = "a") -> SatelliteCarrierResidualEstimate:
    return SatelliteCarrierResidualEstimate(
        54321,
        _carrier(carrier),
        RecordingInterval(UtcNs(100), UtcNs(300)),
        1,
        (35.0, 0.1),
        SATELLITE_CARRIER_RESIDUAL_BASIS,
        RF_UNITS,
        Covariance(
            SATELLITE_CARRIER_RESIDUAL_BASIS,
            RF_UNITS,
            ((9.0, 0.25), (0.25, 2.0)),
            JOINT_COVARIANCE_PSD_TOLERANCE,
        ),
    )


def _joint(
    receiver: ReceiverLnbFrequencyEstimate,
    satellite: SatelliteCarrierResidualEstimate,
) -> Covariance:
    return Covariance(
        receiver.joint_basis() + satellite.joint_basis(),
        RF_UNITS + RF_UNITS,
        (
            (4.0, 0.5, 0.3, 0.1),
            (0.5, 1.0, 0.1, 0.2),
            (0.3, 0.1, 9.0, 0.25),
            (0.1, 0.2, 0.25, 2.0),
        ),
        JOINT_COVARIANCE_PSD_TOLERANCE,
    )


def _provenance(evidence: TrackingModelEvidence) -> Provenance:
    return Provenance(
        "joint-rf-nuisance-model",
        "1.0.0",
        "abc123",
        _digest("environment"),
        evidence.config_ref.digest,
        tracking_model_input_digests(evidence),
        tracking_model_dependency_digests(evidence),
        UtcNs(1_000),
        UtcNs(2_000),
        "offline-analysis",
    )


def _bundle(
    *,
    evidence: TrackingModelEvidence | None = None,
    receiver: ReceiverLnbFrequencyEstimate | None = None,
    satellite: SatelliteCarrierResidualEstimate | None = None,
    joint: Covariance | None = None,
    accepted: tuple[AcceptedAssociationEvidence, ...] | None = None,
    rejected: tuple[RejectedAssociationEvidence, ...] | None = None,
    warnings: tuple[str, ...] = (NOT_ORBIT_STATE_WARNING,),
    provenance: Provenance | None = None,
) -> TrackingModelSnapshotBundle:
    actual_evidence = evidence or _evidence()
    actual_receiver = receiver or _receiver()
    actual_satellite = satellite or _satellite()
    actual_accepted = accepted or (
        AcceptedAssociationEvidence(
            0,
            UtcNs(150),
            actual_receiver.receiver_chain_id,
            CarrierAssociationCandidate(
                actual_satellite.norad_id,
                actual_satellite.carrier_hypothesis_ref,
            ),
            (
                CarrierAssociationCandidate(
                    actual_satellite.norad_id,
                    actual_satellite.carrier_hypothesis_ref,
                ),
            ),
            _digest("accepted-decision"),
        ),
    )
    actual_rejected = (
        rejected
        if rejected is not None
        else (
            RejectedAssociationEvidence(
                1,
                UtcNs(200),
                "ambiguous-association",
                (),
                _digest("rejected-decision"),
            ),
        )
    )
    actual_joint = joint or _joint(actual_receiver, actual_satellite)
    schema = SchemaRef(TrackingModelSnapshotBundle.SCHEMA_ID)
    snapshot_digest = tracking_model_snapshot_digest(
        schema,
        actual_evidence,
        (actual_receiver,),
        (actual_satellite,),
        actual_joint,
        actual_accepted,
        actual_rejected,
        warnings,
    )
    actual_provenance = provenance or _provenance(actual_evidence)
    run_digest = canonical_digest(
        {"snapshot_digest": snapshot_digest, "provenance": actual_provenance}
    )
    return TrackingModelSnapshotBundle(
        schema,
        ModelSnapshotId(f"model_{snapshot_digest.value[:32]}"),
        ModelRunId(f"mrun_{run_digest.value[:32]}"),
        actual_evidence,
        actual_provenance,
        (actual_receiver,),
        (actual_satellite,),
        actual_joint,
        actual_accepted,
        actual_rejected,
        warnings,
    )


def test_canonical_round_trip_preserves_joint_and_marginal_covariance() -> None:
    bundle = _bundle()
    encoded = encode_tracking_model_snapshot(bundle)

    assert decode_tracking_model_snapshot(encoded) == bundle
    assert (
        encode_tracking_model_snapshot(decode_tracking_model_snapshot(encoded))
        == encoded
    )
    assert bundle.receiver_lnb_estimates[0].covariance.values[0][1] == 0.5
    assert (
        bundle.satellite_carrier_residual_estimates[0].covariance.values[0][1] == 0.25
    )
    assert bundle.joint_covariance.values[0][2] == 0.3
    assert "orbit" not in bundle.satellite_carrier_residual_estimates[0].basis[0]


def test_two_carriers_on_one_norad_have_distinct_identity_and_support() -> None:
    receiver = replace(_receiver(), observation_count=2)
    first = _satellite("a")
    second = _satellite("b")
    candidates = (
        CarrierAssociationCandidate(first.norad_id, first.carrier_hypothesis_ref),
        CarrierAssociationCandidate(second.norad_id, second.carrier_hypothesis_ref),
    )
    accepted = (
        AcceptedAssociationEvidence(
            0,
            UtcNs(150),
            receiver.receiver_chain_id,
            candidates[0],
            candidates,
            _digest("carrier-a-decision"),
        ),
        AcceptedAssociationEvidence(
            1,
            UtcNs(200),
            receiver.receiver_chain_id,
            candidates[1],
            candidates,
            _digest("carrier-b-decision"),
        ),
    )
    blocks = (receiver, first, second)
    joint = Covariance(
        tuple(name for block in blocks for name in block.joint_basis()),
        RF_UNITS * 3,
        (
            (4.0, 0.5, 0.0, 0.0, 0.0, 0.0),
            (0.5, 1.0, 0.0, 0.0, 0.0, 0.0),
            (0.0, 0.0, 9.0, 0.25, 0.0, 0.0),
            (0.0, 0.0, 0.25, 2.0, 0.0, 0.0),
            (0.0, 0.0, 0.0, 0.0, 9.0, 0.25),
            (0.0, 0.0, 0.0, 0.0, 0.25, 2.0),
        ),
        JOINT_COVARIANCE_PSD_TOLERANCE,
    )
    evidence = _evidence(carrier_hypothesis_refs=(_carrier("a"), _carrier("b")))
    schema = SchemaRef(TrackingModelSnapshotBundle.SCHEMA_ID)
    warnings = (NOT_ORBIT_STATE_WARNING,)
    snapshot_digest = tracking_model_snapshot_digest(
        schema,
        evidence,
        (receiver,),
        (first, second),
        joint,
        accepted,
        (),
        warnings,
    )
    provenance = _provenance(evidence)
    run_digest = canonical_digest(
        {"snapshot_digest": snapshot_digest, "provenance": provenance}
    )
    bundle = TrackingModelSnapshotBundle(
        schema,
        ModelSnapshotId(f"model_{snapshot_digest.value[:32]}"),
        ModelRunId(f"mrun_{run_digest.value[:32]}"),
        evidence,
        provenance,
        (receiver,),
        (first, second),
        joint,
        accepted,
        (),
        warnings,
    )

    assert first.joint_basis() != second.joint_basis()
    assert (
        decode_tracking_model_snapshot(encode_tracking_model_snapshot(bundle)) == bundle
    )

    document = json.loads(encode_tracking_model_snapshot(bundle))
    document["accepted_associations"][0]["selected_candidate"][
        "carrier_hypothesis_ref"
    ]["digest"]["value"] = _digest("substituted-carrier").value
    with pytest.raises(MalformedTrackingModelSnapshotError):
        decode_tracking_model_snapshot(canonical_json_bytes(document))


def test_joint_covariance_requires_exact_basis_marginals_symmetry_and_psd() -> None:
    bundle = _bundle()
    values = bundle.joint_covariance.values
    wrong_block = replace(
        bundle.joint_covariance,
        values=((4.1, *values[0][1:]), *values[1:]),
    )
    with pytest.raises(ValueError, match="marginal covariance"):
        _bundle(joint=wrong_block)

    near_symmetric = Covariance(
        RF_CALIBRATION_BASIS,
        RF_UNITS,
        ((4.0, 0.5), (0.50000000001, 1.0)),
        JOINT_COVARIANCE_PSD_TOLERANCE,
    )
    with pytest.raises(ValueError, match="exactly symmetric"):
        replace(
            _receiver(),
            covariance=near_symmetric,
        )

    with pytest.raises(ValueError, match="positive semidefinite"):
        Covariance(
            RF_CALIBRATION_BASIS,
            RF_UNITS,
            ((1.0, 2.0), (2.0, 1.0)),
            JOINT_COVARIANCE_PSD_TOLERANCE,
        )


def test_evidence_one_bit_change_changes_content_and_run_ids() -> None:
    first = _bundle()
    ephemeris = first.evidence.ephemerides[0]
    changed = _evidence(
        ephemerides=(replace(ephemeris, raw_digest=_digest("tle-raw-changed")),)
    )
    second = _bundle(evidence=changed)

    assert first.model_snapshot_id != second.model_snapshot_id
    assert first.model_run_id != second.model_run_id
    assert first.provenance.input_digests != second.provenance.input_digests


def test_provenance_must_close_every_input_and_dependency() -> None:
    bundle = _bundle()
    with pytest.raises(ValueError, match="input provenance"):
        replace(
            bundle,
            provenance=replace(
                bundle.provenance,
                input_digests=bundle.provenance.input_digests[:-1],
            ),
        )
    with pytest.raises(ValueError, match="dependency provenance"):
        replace(
            bundle,
            provenance=replace(
                bundle.provenance,
                dependency_digests=bundle.provenance.dependency_digests[:-1],
            ),
        )


def test_associations_cover_entries_and_support_exact_parameter_counts() -> None:
    bundle = _bundle()
    with pytest.raises(ValueError, match="cover ordered entries"):
        _bundle(rejected=())
    with pytest.raises(ValueError, match="support count"):
        _bundle(receiver=replace(_receiver(), observation_count=2))
    with pytest.raises(ValueError, match="observation count"):
        replace(_receiver(), observation_count=0)
    with pytest.raises(ValueError, match="cover ordered entries"):
        _bundle(
            accepted=(replace(bundle.accepted_associations[0], entry_index=0),),
            rejected=(replace(bundle.rejected_associations[0], entry_index=0),),
        )
    receiver = replace(_receiver(), observation_count=2)
    satellite = replace(_satellite(), observation_count=2)
    first = bundle.accepted_associations[0]
    second = replace(
        first,
        entry_index=1,
        observed_utc_ns=UtcNs(175),
        decision_digest=_digest("second-accepted-decision"),
    )
    with pytest.raises(ValueError, match="not canonical"):
        _bundle(
            evidence=_evidence(ordered_entry_count=3),
            receiver=receiver,
            satellite=satellite,
            joint=_joint(receiver, satellite),
            accepted=(second, first),
            rejected=(replace(bundle.rejected_associations[0], entry_index=2),),
        )


def test_contract_rejects_duplicate_or_open_scientific_evidence() -> None:
    evidence = _evidence()
    with pytest.raises(ValueError, match="IDs are duplicated"):
        _evidence(
            prediction_policy_refs=(
                evidence.prediction_policy_refs[0],
                evidence.prediction_policy_refs[0],
            )
        )
    with pytest.raises(ValueError, match="requires a schema"):
        _evidence(
            algorithm_ref=ArtifactRef("unversioned-algorithm", _digest("algorithm"))
        )
    with pytest.raises(ValueError, match="canonical"):
        _evidence(
            calibrations=(
                replace(
                    evidence.calibrations[0],
                    source_digests=(
                        _digest("z-source"),
                        _digest("a-source"),
                    ),
                ),
            )
        )


@pytest.mark.parametrize("mutation", ["missing", "unknown", "one-bit"])
def test_decoder_rejects_missing_unknown_and_content_substitution(
    mutation: str,
) -> None:
    document = json.loads(encode_tracking_model_snapshot(_bundle()))
    if mutation == "missing":
        del document["joint_covariance"]
    elif mutation == "unknown":
        document["unexpected"] = True
    else:
        document["evidence"]["ephemerides"][0]["raw_digest"]["value"] = _digest(
            "substituted"
        ).value

    with pytest.raises(MalformedTrackingModelSnapshotError):
        decode_tracking_model_snapshot(canonical_json_bytes(document))


def test_decoder_rejects_duplicate_noncanonical_nonfinite_and_oversized_bytes() -> None:
    encoded = encode_tracking_model_snapshot(_bundle())
    duplicate = b'{"schema":null,' + encoded[1:]
    with pytest.raises(MalformedTrackingModelSnapshotError, match="duplicate"):
        decode_tracking_model_snapshot(duplicate)
    with pytest.raises(MalformedTrackingModelSnapshotError, match="canonical"):
        decode_tracking_model_snapshot(json.dumps(json.loads(encoded)).encode())
    nonfinite_document = json.loads(encoded)
    nonfinite_document["receiver_lnb_estimates"][0]["value"][0] = float("nan")
    nonfinite = json.dumps(nonfinite_document, separators=(",", ":")).encode()
    with pytest.raises(MalformedTrackingModelSnapshotError):
        decode_tracking_model_snapshot(nonfinite)
    with pytest.raises(MalformedTrackingModelSnapshotError, match="size"):
        decode_tracking_model_snapshot(b"x" * (MAX_TRACKING_MODEL_SNAPSHOT_BYTES + 1))


def test_warning_must_explicitly_disclaim_orbit_state() -> None:
    with pytest.raises(ValueError, match="warnings"):
        _bundle(warnings=("joint-rf-estimate",))
