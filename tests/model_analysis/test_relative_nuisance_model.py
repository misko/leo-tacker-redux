from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.model import (
    ModelInputError,
    NuisanceBatchConfig,
    NuisanceSimulationSpec,
    NuisanceTerm,
    ReceiverAssignment,
    RelativeRadioLnbNuisanceModel,
    nuisance_batch_algorithm_ref,
    nuisance_batch_config_ref,
    simulate_nuisance_observations,
)
from leo_flow.contracts.core import (
    AnalysisRunId,
    DatasetSnapshotId,
    FeatureSetId,
    HardwareSnapshotId,
    Provenance,
    RadioId,
    ReceiverChainId,
    SchemaRef,
    StationId,
    UtcNs,
)
from leo_flow.contracts.features import (
    FeatureObservation,
    FeatureSetBundle,
    FeatureSetRef,
)
from leo_flow.contracts.hardware import (
    HardwareMetadataSnapshot,
    HardwareMetadataSnapshotRef,
    ReceiverChainMetadata,
)
from leo_flow.contracts.model import (
    FeatureDatasetSnapshot,
    FeatureDatasetSnapshotRef,
    ModelAnalysisRequest,
    feature_dataset_membership_digest,
)
from leo_flow.contracts.storage import ObjectRef

from .fakes import (
    FakeEphemerisReader,
    FakeFeatureSetReader,
    FakeHardwareReader,
    digest,
    execution_context,
)


def _entries(
    observations: tuple[FeatureObservation, ...],
) -> tuple[tuple[FeatureSetRef, FeatureSetBundle], ...]:
    entries: list[tuple[FeatureSetRef, FeatureSetBundle]] = []
    for index, observation in enumerate(observations):
        feature_set_id = FeatureSetId(f"fset_nuisance_{index}")
        run_id = AnalysisRunId(f"arun_nuisance_{index}")
        object_ref = ObjectRef(
            digest(f"nuisance-bundle-{index}"),
            100 + index,
            "application/json",
            "feature-bundle-v0.1",
            f"memory://nuisance/{index}",
        )
        ref = FeatureSetRef(feature_set_id, run_id, object_ref)
        bundle = FeatureSetBundle(
            SchemaRef(FeatureSetBundle.SCHEMA_ID),
            feature_set_id,
            run_id,
            observation.recording_id,
            digest(f"recording-nuisance-{index}"),
            Provenance(
                "nuisance-fixture",
                "0.1.0",
                "fixture-commit",
                digest("fixture-environment"),
                digest("fixture-config"),
                (digest(f"recording-nuisance-{index}"),),
                (digest("fixture-algorithm"),),
                UtcNs(1),
                UtcNs(2),
                "fixture-host",
            ),
            (observation,),
            (),
        )
        entries.append((ref, bundle))
    return tuple(entries)


def _dataset(
    entries: tuple[tuple[FeatureSetRef, FeatureSetBundle], ...],
) -> FeatureDatasetSnapshot:
    refs = tuple(ref for ref, _ in entries)
    return FeatureDatasetSnapshot(
        SchemaRef(FeatureDatasetSnapshot.SCHEMA_ID),
        DatasetSnapshotId("dataset_nuisance"),
        refs,
        "fixture:explicit-nuisance-membership",
        UtcNs(10**15),
        feature_dataset_membership_digest(refs),
    )


def _hardware(
    assignments: tuple[ReceiverAssignment, ...],
    *,
    extra_chains: tuple[ReceiverChainMetadata, ...] = (),
) -> tuple[HardwareMetadataSnapshotRef, HardwareMetadataSnapshot]:
    radio_ids = tuple(sorted({assignment.radio_id for assignment in assignments}))
    ref = HardwareMetadataSnapshotRef(
        HardwareSnapshotId("hw_nuisance"), digest("hardware-nuisance")
    )
    snapshot = HardwareMetadataSnapshot(
        SchemaRef(HardwareMetadataSnapshot.SCHEMA_ID),
        ref.snapshot_id,
        StationId("station_nuisance"),
        tuple(RadioId(value) for value in radio_ids),
        tuple(
            ReceiverChainMetadata(
                ReceiverChainId(assignment.receiver_chain_id),
                RadioId(assignment.radio_id),
                index,
                assignment.lnb_id,
                None,
                None,
                UtcNs(0),
                None,
            )
            for index, assignment in enumerate(assignments)
        )
        + extra_chains,
    )
    return ref, snapshot


def _request(
    snapshot: FeatureDatasetSnapshot,
    config: NuisanceBatchConfig,
    hardware_ref: HardwareMetadataSnapshotRef,
) -> ModelAnalysisRequest:
    return ModelAnalysisRequest(
        SchemaRef(ModelAnalysisRequest.SCHEMA_ID),
        FeatureDatasetSnapshotRef(snapshot.snapshot_id, snapshot.membership_digest),
        (hardware_ref,),
        (),
        nuisance_batch_config_ref(config),
        nuisance_batch_algorithm_ref(),
    )


def _fit(
    observations: tuple[FeatureObservation, ...],
    assignments: tuple[ReceiverAssignment, ...],
    *,
    config: NuisanceBatchConfig | None = None,
    hardware: tuple[HardwareMetadataSnapshotRef, HardwareMetadataSnapshot]
    | None = None,
):
    config = config or NuisanceBatchConfig()
    entries = _entries(observations)
    snapshot = _dataset(entries)
    hw = hardware or _hardware(assignments)
    bundle = RelativeRadioLnbNuisanceModel(snapshot, config, execution_context()).fit(
        _request(snapshot, config, hw[0]),
        FakeFeatureSetReader(entries),
        FakeEphemerisReader(()),
        FakeHardwareReader((hw,)),
    )
    return bundle, entries, snapshot, hw


def _parameter_map(bundle) -> dict[tuple[str, str], tuple[float, float]]:
    return {
        (parameter.parameter_id, parameter.subject_id): (
            parameter.value[0],
            parameter.covariance.values[0][0],
        )
        for parameter in bundle.parameters
    }


def _truth_spec(**changes: object) -> NuisanceSimulationSpec:
    assignments = (
        ReceiverAssignment("rx_0_a", "radio_0", "lnb_a"),
        ReceiverAssignment("rx_0_b", "radio_0", "lnb_b"),
        ReceiverAssignment("rx_1_a", "radio_1", "lnb_a"),
        ReceiverAssignment("rx_1_b", "radio_1", "lnb_b"),
    )
    values = {
        "radios": (
            NuisanceTerm("radio_0", 0.0, 0.0),
            NuisanceTerm("radio_1", 100.0, 0.2),
        ),
        "lnbs": (
            NuisanceTerm("lnb_a", 10.0, 0.05),
            NuisanceTerm("lnb_b", -20.0, -0.03),
        ),
        "assignments": assignments,
        "samples_per_assignment": 40,
        "frequency_sigma_hz": 1.0,
        "drift_sigma_hz_s": 0.01,
        "seed": 418,
    }
    values.update(changes)
    return NuisanceSimulationSpec(**values)  # type: ignore[arg-type]


def test_seeded_truth_recovery_covariance_and_gauge_are_explicit() -> None:
    spec = _truth_spec()
    observations = simulate_nuisance_observations(spec)
    bundle, _, _, _ = _fit(observations, spec.assignments)
    values = _parameter_map(bundle)

    assert values[("radio-frequency-offset", "radio_0")][0] == 0.0
    assert values[("radio-frequency-offset", "radio_1")][0] == pytest.approx(
        100.0, abs=0.6
    )
    assert values[("lnb-frequency-offset", "lnb_a")][0] == pytest.approx(10.0, abs=0.4)
    assert values[("lnb-frequency-offset", "lnb_b")][0] == pytest.approx(-20.0, abs=0.4)
    assert values[("radio-frequency-drift", "radio_1")][0] == pytest.approx(
        0.2, abs=0.006
    )
    assert all(variance > 0.0 for _, variance in values.values())
    truth = {
        ("radio-frequency-offset", "radio_1"): 100.0,
        ("lnb-frequency-offset", "lnb_a"): 10.0,
        ("lnb-frequency-offset", "lnb_b"): -20.0,
        ("radio-frequency-drift", "radio_1"): 0.2,
        ("lnb-frequency-drift", "lnb_a"): 0.05,
        ("lnb-frequency-drift", "lnb_b"): -0.03,
    }
    for key, expected in truth.items():
        estimate, variance = values[key]
        assert abs(estimate - expected) <= 3.0 * variance**0.5
    assert "frequency:gauge-reference-radio:radio_0" in bundle.warnings
    assert "drift:gauge-reference-radio:radio_0" in bundle.warnings


def test_robust_fit_handles_outliers_heteroscedasticity_and_missing_values() -> None:
    spec = _truth_spec(
        outlier_indices=(3, 54, 117),
        missing_frequency_indices=tuple(range(0, 160, 9)),
        missing_drift_indices=tuple(range(0, 160, 7)),
    )
    observations = list(simulate_nuisance_observations(spec))
    # Make alternating observations much less informative without changing truth.
    for index in range(1, len(observations), 2):
        covariance = observations[index].covariance
        assert covariance is not None
        observations[index] = replace(
            observations[index],
            covariance=replace(
                covariance,
                values=((25.0, 0.0), (0.0, 0.0025)),
            ),
        )
    bundle, _, _, _ = _fit(tuple(observations), spec.assignments)
    values = _parameter_map(bundle)
    assert values[("radio-frequency-offset", "radio_1")][0] == pytest.approx(
        100.0, abs=1.5
    )
    assert values[("radio-frequency-drift", "radio_1")][0] == pytest.approx(
        0.2, abs=0.015
    )
    assert any(
        warning.startswith("frequency:robust-downweighted:")
        for warning in bundle.warnings
    )
    assert any(
        warning.startswith("drift:robust-downweighted:") for warning in bundle.warnings
    )


def test_parameter_fit_is_invariant_to_dataset_and_reader_order() -> None:
    spec = _truth_spec(samples_per_assignment=8)
    observations = simulate_nuisance_observations(spec)
    first, entries, snapshot, hw = _fit(observations, spec.assignments)
    reversed_entries = tuple(reversed(entries))
    reversed_snapshot = replace(
        _dataset(reversed_entries), snapshot_id=snapshot.snapshot_id
    )
    config = NuisanceBatchConfig()
    second = RelativeRadioLnbNuisanceModel(
        reversed_snapshot, config, execution_context()
    ).fit(
        _request(reversed_snapshot, config, hw[0]),
        FakeFeatureSetReader(tuple(reversed(reversed_entries))),
        FakeEphemerisReader(()),
        FakeHardwareReader((hw,)),
    )
    assert first.parameters == second.parameters
    assert first.model_snapshot_id != second.model_snapshot_id


def test_effective_dated_lnb_swap_selects_the_observation_epoch() -> None:
    assignments = (
        ReceiverAssignment("rx_swap", "radio_0", "lnb_a"),
        ReceiverAssignment("rx_other", "radio_0", "lnb_b"),
    )
    spec = NuisanceSimulationSpec(
        radios=(NuisanceTerm("radio_0", 0.0, 0.0),),
        lnbs=(
            NuisanceTerm("lnb_a", 15.0, 0.1),
            NuisanceTerm("lnb_b", -5.0, -0.1),
        ),
        assignments=assignments,
        samples_per_assignment=8,
        frequency_sigma_hz=0.1,
        drift_sigma_hz_s=0.001,
        seed=9,
        start_utc_ns=UtcNs(100),
        spacing_ns=10,
    )
    observations = list(simulate_nuisance_observations(spec))
    # The first receiver's later half was acquired after an LNB swap.
    for index in range(4, 8):
        observations[index] = replace(
            observations[index],
            frequency_offset_hz=-5.0,
            drift_hz_s=-0.1,
            midpoint_utc_ns=UtcNs(1_100 + index),
        )
    ref = HardwareMetadataSnapshotRef(
        HardwareSnapshotId("hw_nuisance"), digest("hardware-nuisance")
    )
    hardware = HardwareMetadataSnapshot(
        SchemaRef(HardwareMetadataSnapshot.SCHEMA_ID),
        ref.snapshot_id,
        StationId("station_nuisance"),
        (RadioId("radio_0"),),
        (
            ReceiverChainMetadata(
                ReceiverChainId("rx_swap"),
                RadioId("radio_0"),
                0,
                "lnb_a",
                None,
                None,
                UtcNs(0),
                UtcNs(1_000),
            ),
            ReceiverChainMetadata(
                ReceiverChainId("rx_swap"),
                RadioId("radio_0"),
                0,
                "lnb_b",
                None,
                None,
                UtcNs(1_000),
                None,
            ),
            ReceiverChainMetadata(
                ReceiverChainId("rx_other"),
                RadioId("radio_0"),
                1,
                "lnb_b",
                None,
                None,
                UtcNs(0),
                None,
            ),
        ),
    )
    bundle, _, _, _ = _fit(tuple(observations), assignments, hardware=(ref, hardware))
    values = _parameter_map(bundle)
    assert values[("lnb-frequency-offset", "lnb_a")][0] == pytest.approx(15.0, abs=0.2)
    assert values[("lnb-frequency-offset", "lnb_b")][0] == pytest.approx(-5.0, abs=0.2)


def test_non_identifiable_inputs_fail_closed_without_false_precision() -> None:
    spec = _truth_spec(samples_per_assignment=1)
    observations = simulate_nuisance_observations(spec)[:1]
    bundle, _, _, _ = _fit(observations, spec.assignments)
    assert not bundle.parameters
    assert "frequency:insufficient-measurements:1<3" in bundle.warnings
    assert "drift:insufficient-measurements:1<3" in bundle.warnings
    assert "model:no-identifiable-parameters" in bundle.warnings


def test_covariance_units_are_not_silently_reinterpreted() -> None:
    spec = _truth_spec(samples_per_assignment=1)
    observations = list(simulate_nuisance_observations(spec))
    covariance = observations[0].covariance
    assert covariance is not None
    observations[0] = replace(
        observations[0], covariance=replace(covariance, units=("counts", "Hz/s"))
    )
    with pytest.raises(ModelInputError, match="unit-must-be-Hz"):
        _fit(tuple(observations), spec.assignments)


def test_later_block_prediction_and_exact_replay_are_stable() -> None:
    training = _truth_spec(samples_per_assignment=25, seed=81)
    held_out = replace(training, samples_per_assignment=10, seed=82)
    bundle, _, _, _ = _fit(
        simulate_nuisance_observations(training), training.assignments
    )
    replay, _, _, _ = _fit(
        simulate_nuisance_observations(training), training.assignments
    )
    assert bundle == replay
    values = _parameter_map(bundle)
    assignments = {item.receiver_chain_id: item for item in held_out.assignments}
    residuals: list[float] = []
    for observation in simulate_nuisance_observations(held_out):
        assignment = assignments[str(observation.receiver_chain_id)]
        predicted = (
            values[("radio-frequency-offset", assignment.radio_id)][0]
            + values[("lnb-frequency-offset", assignment.lnb_id)][0]
        )
        assert observation.frequency_offset_hz is not None
        residuals.append(observation.frequency_offset_hz - predicted)
    rmse = (sum(value * value for value in residuals) / len(residuals)) ** 0.5
    assert rmse < 1.4 * held_out.frequency_sigma_hz
