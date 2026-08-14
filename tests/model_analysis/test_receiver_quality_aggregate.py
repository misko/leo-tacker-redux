from __future__ import annotations

import ast
import math
from dataclasses import replace
from pathlib import Path

import pytest

from leo_flow.analysis.model import (
    ModelConfigurationError,
    ModelInputError,
    ReceiverQualityAggregateConfig,
    ReceiverQualityAggregateModel,
    receiver_quality_aggregate_algorithm_ref,
)
from leo_flow.contracts.core import Digest, SchemaRef
from leo_flow.contracts.model import FeatureDatasetSnapshotRef, ModelSnapshotBundle

from .fakes import (
    FakeEphemerisReader,
    FakeFeatureSetReader,
    FakeHardwareReader,
    dataset,
    digest,
    ephemeris_ref,
    execution_context,
    feature_set,
    hardware_snapshot,
    request,
)

ROOT = Path(__file__).resolve().parents[2]


def test_inverse_variance_aggregate_closes_exact_pinned_inputs() -> None:
    first = feature_set(0, (("rx_0", 10.0, 4.0),))
    second = feature_set(1, (("rx_0", 20.0, 1.0),))
    snapshot = dataset((first[0], second[0]))
    hw = hardware_snapshot(receivers=("rx_0",))
    eph = ephemeris_ref()
    config = ReceiverQualityAggregateConfig()
    model_request = request(snapshot, config, (hw[0],), (eph,))
    feature_reader = FakeFeatureSetReader((first, second))
    hardware_reader = FakeHardwareReader((hw,))
    ephemeris_reader = FakeEphemerisReader((eph,))

    bundle = ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
        model_request, feature_reader, ephemeris_reader, hardware_reader
    )

    assert isinstance(bundle, ModelSnapshotBundle)
    assert len(bundle.parameters) == 1
    parameter = bundle.parameters[0]
    assert parameter.parameter_id == "receiver-mean-rms-magnitude"
    assert parameter.subject_id == "rx_0"
    assert parameter.value == pytest.approx((18.0,))
    assert parameter.covariance.values[0][0] == pytest.approx(0.8)
    assert parameter.basis == parameter.covariance.basis
    assert parameter.units == parameter.covariance.units
    assert feature_reader.calls == [first[0], second[0]]
    assert [view.bundle_calls for view in feature_reader.views] == [1, 1]
    assert hardware_reader.calls == [hw[0]]
    assert ephemeris_reader.calls == [eph]
    assert ephemeris_reader.views[0].normalized_calls == 0
    assert bundle.dataset_membership_digest == snapshot.membership_digest
    assert bundle.hardware_snapshot_digests == (hw[0].digest,)
    assert bundle.ephemeris_snapshot_digests == (eph.normalized_digest,)
    assert bundle.provenance.input_digests == (
        snapshot.membership_digest,
        first[0].bundle_ref.digest,
        second[0].bundle_ref.digest,
    )
    assert bundle.provenance.dependency_digests == (
        model_request.algorithm_ref.digest,
        hw[0].digest,
        eph.raw_digest,
        eph.normalized_digest,
    )
    assert "rx_0:covariance-mode:inverse-variance" in bundle.warnings


def test_absent_uncertainty_uses_between_recording_covariance() -> None:
    entries = (
        feature_set(0, (("rx_0", 10.0, None),)),
        feature_set(1, (("rx_0", 20.0, None),)),
        feature_set(2, (("rx_0", 30.0, None),)),
    )
    snapshot = dataset(tuple(item[0] for item in entries))
    hw = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    bundle = ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
        request(snapshot, config, (hw[0],)),
        FakeFeatureSetReader(entries),
        FakeEphemerisReader(()),
        FakeHardwareReader((hw,)),
    )
    parameter = bundle.parameters[0]
    assert parameter.value == pytest.approx((20.0,))
    assert parameter.covariance.values[0][0] == pytest.approx(100.0 / 3.0)
    assert "rx_0:covariance-mode:between-recording-scatter" in bundle.warnings


def test_hardware_and_ephemeris_reference_order_is_not_scientific() -> None:
    first = feature_set(0, (("rx_0", 5.0, 1.0),))
    second = feature_set(1, (("rx_0", 7.0, 1.0),))
    snapshot = dataset((first[0], second[0]))
    hw0 = hardware_snapshot(0, ("rx_0",))
    hw1 = hardware_snapshot(1, ("rx_1",))
    eph0 = ephemeris_ref(0)
    eph1 = ephemeris_ref(1)
    config = ReceiverQualityAggregateConfig()
    model = ReceiverQualityAggregateModel(snapshot, config, execution_context())

    a = model.fit(
        request(snapshot, config, (hw0[0], hw1[0]), (eph0, eph1)),
        FakeFeatureSetReader((first, second)),
        FakeEphemerisReader((eph0, eph1)),
        FakeHardwareReader((hw0, hw1)),
    )
    b = model.fit(
        request(snapshot, config, (hw1[0], hw0[0]), (eph1, eph0)),
        FakeFeatureSetReader((first, second)),
        FakeEphemerisReader((eph0, eph1)),
        FakeHardwareReader((hw0, hw1)),
    )
    assert a == b


def test_dataset_membership_order_is_pinned_but_estimator_is_order_stable() -> None:
    first = feature_set(0, (("rx_0", 1e16, None),))
    second = feature_set(1, (("rx_0", 1.0, None),))
    third = feature_set(2, (("rx_0", -1e16, None),))
    forward = dataset((first[0], second[0], third[0]))
    reverse = replace(
        dataset((third[0], second[0], first[0])),
        snapshot_id=forward.snapshot_id,
    )
    hw = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()

    first_result = ReceiverQualityAggregateModel(
        forward, config, execution_context()
    ).fit(
        request(forward, config, (hw[0],)),
        FakeFeatureSetReader((first, second, third)),
        FakeEphemerisReader(()),
        FakeHardwareReader((hw,)),
    )
    second_result = ReceiverQualityAggregateModel(
        reverse, config, execution_context()
    ).fit(
        request(reverse, config, (hw[0],)),
        FakeFeatureSetReader((first, second, third)),
        FakeEphemerisReader(()),
        FakeHardwareReader((hw,)),
    )
    assert forward.membership_digest != reverse.membership_digest
    assert first_result.model_snapshot_id != second_result.model_snapshot_id
    assert first_result.parameters == second_result.parameters


def test_fit_is_deterministic_for_identical_immutable_inputs() -> None:
    entries = (
        feature_set(0, (("rx_0", 2.0, None), ("rx_1", 8.0, None))),
        feature_set(1, (("rx_0", 4.0, None), ("rx_1", 6.0, None))),
    )
    snapshot = dataset(tuple(item[0] for item in entries))
    hw = hardware_snapshot(receivers=("rx_0", "rx_1"))
    config = ReceiverQualityAggregateConfig()
    model_request = request(snapshot, config, (hw[0],))
    model = ReceiverQualityAggregateModel(snapshot, config, execution_context())
    first = model.fit(
        model_request,
        FakeFeatureSetReader(entries),
        FakeEphemerisReader(()),
        FakeHardwareReader((hw,)),
    )
    second = model.fit(
        model_request,
        FakeFeatureSetReader(entries),
        FakeEphemerisReader(()),
        FakeHardwareReader((hw,)),
    )
    assert first == second
    assert [parameter.subject_id for parameter in first.parameters] == ["rx_0", "rx_1"]


def test_request_must_pin_injected_snapshot_before_any_reader_call() -> None:
    entry = feature_set(0, (("rx_0", 2.0, None),))
    snapshot = dataset((entry[0],))
    hw = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    wrong = replace(
        request(snapshot, config, (hw[0],)),
        dataset_snapshot_ref=FeatureDatasetSnapshotRef(
            snapshot.snapshot_id, digest("wrong-membership")
        ),
    )
    features = FakeFeatureSetReader((entry,))
    hardware = FakeHardwareReader((hw,))
    ephemerides = FakeEphemerisReader(())
    with pytest.raises(ModelConfigurationError, match="dataset_snapshot_ref"):
        ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
            wrong, features, ephemerides, hardware
        )
    assert not features.calls
    assert not hardware.calls
    assert not ephemerides.calls


def test_reader_must_return_exact_feature_membership_identity() -> None:
    first = feature_set(0, (("rx_0", 2.0, None),))
    second = feature_set(1, (("rx_0", 3.0, None),))
    snapshot = dataset((first[0],))
    hw = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    features = FakeFeatureSetReader((first, second))
    features.returned_ref_override = second[0]
    with pytest.raises(ModelInputError, match="pinned membership"):
        ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
            request(snapshot, config, (hw[0],)),
            features,
            FakeEphemerisReader(()),
            FakeHardwareReader((hw,)),
        )
    assert features.calls == [first[0]]


def test_feature_reader_must_return_the_full_pinned_object_reference() -> None:
    first = feature_set(0, (("rx_0", 2.0, None),))
    second = feature_set(1, (("rx_0", 4.0, None),))
    snapshot = dataset((first[0], second[0]))
    moved = replace(
        first[0],
        bundle_ref=replace(first[0].bundle_ref, locator="memory://moved/feature-0"),
    )
    hw = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    features = FakeFeatureSetReader(((moved, first[1]), second))
    with pytest.raises(ModelInputError, match="pinned membership"):
        ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
            request(snapshot, config, (hw[0],)),
            features,
            FakeEphemerisReader(()),
            FakeHardwareReader((hw,)),
        )


@pytest.mark.parametrize(
    ("entries", "warning"),
    [
        (
            (feature_set(10, (("rx_0", 1.0, None),)),),
            "rx_0:insufficient-feature-sets:1<2",
        ),
        (
            (
                feature_set(11, (("rx_0", 1.0, None),)),
                feature_set(12, (("rx_0", 2.0, 1.0),)),
            ),
            "rx_0:partial-score-variance:not-identifiable",
        ),
        (
            (
                feature_set(13, (("rx_0", 1.0, None),), duplicate_first=True),
                feature_set(14, (("rx_0", 2.0, None),)),
            ),
            "rx_0:fset_13:ambiguous-observation-count:2",
        ),
    ],
)
def test_non_identifiable_inputs_return_no_false_parameter(
    entries: tuple[tuple[object, object], ...], warning: str
) -> None:
    typed_entries = tuple(entries)  # fixture parameterization keeps values immutable
    refs = tuple(entry[0] for entry in typed_entries)
    snapshot = dataset(refs)  # type: ignore[arg-type]
    hw = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    bundle = ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
        request(snapshot, config, (hw[0],)),
        FakeFeatureSetReader(typed_entries),  # type: ignore[arg-type]
        FakeEphemerisReader(()),
        FakeHardwareReader((hw,)),
    )
    assert not bundle.parameters
    assert warning in bundle.warnings
    assert "model:no-identifiable-parameters" in bundle.warnings


@pytest.mark.parametrize("variance", [-1.0, 0.0, math.nan, math.inf, True, "bad"])
def test_invalid_score_variance_is_rejected_with_feature_context(
    variance: object,
) -> None:
    entry = feature_set(20, (("rx_0", 1.0, None),))
    observation = replace(
        entry[1].observations[0], uncertainty=(("score_variance", variance),)
    )
    entry = (entry[0], replace(entry[1], observations=(observation,)))
    snapshot = dataset((entry[0],))
    hw = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    with pytest.raises(ModelInputError, match="fset_20:feature_20_0:score_variance"):
        ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
            request(snapshot, config, (hw[0],)),
            FakeFeatureSetReader((entry,)),
            FakeEphemerisReader(()),
            FakeHardwareReader((hw,)),
        )


def test_extreme_heteroscedastic_weights_do_not_overflow() -> None:
    entries = (
        feature_set(30, (("rx_0", 10.0, 5e-324),)),
        feature_set(31, (("rx_0", 20.0, 1.0),)),
    )
    snapshot = dataset(tuple(entry[0] for entry in entries))
    hw = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    bundle = ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
        request(snapshot, config, (hw[0],)),
        FakeFeatureSetReader(entries),
        FakeEphemerisReader(()),
        FakeHardwareReader((hw,)),
    )
    assert bundle.parameters[0].value == (10.0,)
    assert bundle.parameters[0].covariance.values == ((config.covariance_floor,),)


def test_unrepresentable_empirical_covariance_fails_explicitly() -> None:
    entries = (
        feature_set(32, (("rx_0", -1e308, None),)),
        feature_set(33, (("rx_0", 1e308, None),)),
    )
    snapshot = dataset(tuple(entry[0] for entry in entries))
    hw = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    with pytest.raises(ModelInputError, match="rx_0:aggregate-result-not-finite"):
        ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
            request(snapshot, config, (hw[0],)),
            FakeFeatureSetReader(entries),
            FakeEphemerisReader(()),
            FakeHardwareReader((hw,)),
        )


def test_hardware_validity_is_required_for_each_observation() -> None:
    entries = (
        feature_set(0, (("rx_0", 1.0, None),), midpoint_utc_ns=1_000),
        feature_set(1, (("rx_0", 2.0, None),), midpoint_utc_ns=1_000),
    )
    snapshot = dataset(tuple(entry[0] for entry in entries))
    hw = hardware_snapshot(
        receivers=("rx_0",), valid_from_utc_ns=2_000, valid_until_utc_ns=3_000
    )
    config = ReceiverQualityAggregateConfig()
    bundle = ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
        request(snapshot, config, (hw[0],)),
        FakeFeatureSetReader(entries),
        FakeEphemerisReader(()),
        FakeHardwareReader((hw,)),
    )
    assert not bundle.parameters
    assert "rx_0:fset_0:hardware-not-effective" in bundle.warnings
    assert "rx_0:fset_1:hardware-not-effective" in bundle.warnings


@pytest.mark.parametrize(
    "values",
    [
        {"minimum_feature_sets": 1},
        {"minimum_feature_sets": True},
        {"score_variance_key": ""},
        {"score_variance_key": "not a token"},
        {"covariance_floor": 0.0},
        {"covariance_floor": math.nan},
        {"covariance_floor": True},
    ],
)
def test_config_rejects_invalid_values(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ReceiverQualityAggregateConfig(**values)  # type: ignore[arg-type]


def test_wrong_algorithm_ref_fails_before_capability_use() -> None:
    entry = feature_set(0, (("rx_0", 1.0, None),))
    snapshot = dataset((entry[0],))
    hw = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    model_request = replace(
        request(snapshot, config, (hw[0],)),
        algorithm_ref=replace(
            receiver_quality_aggregate_algorithm_ref(), digest=Digest.sha256(b"wrong")
        ),
    )
    features = FakeFeatureSetReader((entry,))
    hardware = FakeHardwareReader((hw,))
    with pytest.raises(ModelConfigurationError, match="algorithm_ref"):
        ReceiverQualityAggregateModel(snapshot, config, execution_context()).fit(
            model_request, features, FakeEphemerisReader(()), hardware
        )
    assert not features.calls
    assert not hardware.calls


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_model_has_no_raw_recording_or_external_capabilities() -> None:
    source = ROOT / "src" / "leo_flow" / "analysis" / "model"
    forbidden_prefixes = (
        "asyncio",
        "http",
        "requests",
        "socket",
        "subprocess",
        "urllib",
        "leo_tracker",
        "leo_flow.capture",
        "leo_flow.analysis.recording",
        "leo_flow.storage",
        "leo_flow.jobs",
        "psycopg",
        "sqlalchemy",
    )
    for path in source.rglob("*.py"):
        modules = imported_modules(path)
        assert not any(
            module == prefix or module.startswith(prefix + ".")
            for module in modules
            for prefix in forbidden_prefixes
        ), path
        text = path.read_text(encoding="utf-8")
        assert "read_iq_bytes" not in text
        assert "normalized_bytes()" not in text


def test_algorithm_descriptor_is_frozen_and_typed() -> None:
    ref = receiver_quality_aggregate_algorithm_ref()
    assert ref.artifact_id == "receiver-quality-aggregate-v0.1"
    assert ref.schema == SchemaRef("org.leo-flow.model-algorithm")
    assert ref == receiver_quality_aggregate_algorithm_ref()
