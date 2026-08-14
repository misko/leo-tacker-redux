from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from leo_flow.analysis.dataset import DatasetSnapshotRef
from leo_flow.analysis.model import (
    ReceiverQualityAggregateConfig,
    ReceiverQualityAggregateModel,
)
from leo_flow.contracts._validation import thaw_value
from leo_flow.contracts.core import Digest, JobId, SchemaRef, UtcNs
from leo_flow.jobs import JobLease, JobPayload, JobType
from leo_flow.services.model_analysis import (
    MODEL_ANALYSIS_JOB_SCHEMA,
    ModelAnalysisJobError,
    ModelAnalysisJobPreparer,
    decode_model_analysis_payload,
    model_analysis_payload,
)
from tests.model_analysis.fakes import (
    FakeEphemerisReader,
    FakeFeatureSetReader,
    FakeHardwareReader,
    dataset,
    execution_context,
    feature_set,
    hardware_snapshot,
    request,
)


def _fixture():
    feature = feature_set(201, (("rx_0", 10.0, 1.0),))
    model_dataset = dataset((feature[0],))
    hardware = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    model_request = request(model_dataset, config, (hardware[0],))
    bundle = ReceiverQualityAggregateModel(
        model_dataset, config, execution_context()
    ).fit(
        model_request,
        FakeFeatureSetReader((feature,)),
        FakeEphemerisReader(()),
        FakeHardwareReader((hardware,)),
    )
    durable = DatasetSnapshotRef(
        model_dataset.snapshot_id,
        model_dataset.membership_digest,
        Digest.sha256(b"rich-dataset-truth-and-splits"),
    )
    return model_request, durable, model_dataset, bundle


def _lease(payload: JobPayload, job_type: JobType = JobType.MODEL_ANALYSIS) -> JobLease:
    return JobLease(
        JobId("job_model_unit"),
        job_type,
        payload,
        1,
        "lease-token",
        1,
        UtcNs(10_000),
    )


def test_payload_round_trip_pins_rich_and_model_dataset_identity() -> None:
    model_request, durable, _, _ = _fixture()

    assert decode_model_analysis_payload(
        model_analysis_payload(model_request, durable)
    ) == (model_request, durable)


def test_payload_rejects_unknown_fields_and_crossed_dataset_refs() -> None:
    model_request, durable, _, _ = _fixture()
    payload = model_analysis_payload(model_request, durable)
    document = thaw_value(payload.value)
    document["unexpected"] = True
    with pytest.raises(ModelAnalysisJobError, match="fields differ"):
        decode_model_analysis_payload(
            JobPayload.create(MODEL_ANALYSIS_JOB_SCHEMA, document)
        )
    with pytest.raises(ModelAnalysisJobError, match="differs"):
        model_analysis_payload(
            model_request,
            DatasetSnapshotRef(
                durable.snapshot_id,
                Digest.sha256(b"wrong-membership"),
                durable.snapshot_digest,
            ),
        )


@dataclass
class _RichSnapshot:
    ref: DatasetSnapshotRef
    feature_dataset: Any


class _DatasetReader:
    def __init__(self, returned: _RichSnapshot) -> None:
        self.returned = returned
        self.calls: list[DatasetSnapshotRef] = []

    def get(self, ref: DatasetSnapshotRef) -> Any:
        self.calls.append(ref)
        return self.returned


class _Fitter:
    def __init__(self, bundle: Any) -> None:
        self.bundle = bundle
        self.calls: list[tuple[object, ...]] = []

    def fit(self, *args: object) -> Any:
        self.calls.append(args)
        return self.bundle


def test_preparer_resolves_exact_rich_dataset_before_fitting() -> None:
    model_request, durable, model_dataset, bundle = _fixture()
    datasets = _DatasetReader(_RichSnapshot(durable, model_dataset))
    features = FakeFeatureSetReader(())
    ephemerides = FakeEphemerisReader(())
    hardware = FakeHardwareReader(())
    fitter = _Fitter(bundle)
    factory_calls: list[object] = []

    def factory(resolved: object) -> Any:
        factory_calls.append(resolved)
        return fitter

    prepared = ModelAnalysisJobPreparer(
        datasets, features, ephemerides, hardware, factory
    ).prepare(_lease(model_analysis_payload(model_request, durable)))

    assert prepared.request == model_request
    assert prepared.durable_dataset_ref == durable
    assert prepared.bundle == bundle
    assert datasets.calls == [durable]
    assert factory_calls == [model_dataset]
    assert fitter.calls == [(model_request, features, ephemerides, hardware)]


def test_preparer_rejects_other_job_types_before_access() -> None:
    model_request, durable, model_dataset, bundle = _fixture()
    datasets = _DatasetReader(_RichSnapshot(durable, model_dataset))
    preparer = ModelAnalysisJobPreparer(
        datasets,
        FakeFeatureSetReader(()),
        FakeEphemerisReader(()),
        FakeHardwareReader(()),
        lambda _: _Fitter(bundle),
    )
    with pytest.raises(ModelAnalysisJobError, match="model-analysis"):
        preparer.prepare(
            _lease(
                model_analysis_payload(model_request, durable),
                JobType.RECORDING_ANALYSIS,
            )
        )
    assert datasets.calls == []


def test_payload_schema_is_not_interchangeable() -> None:
    model_request, durable, _, _ = _fixture()
    payload = model_analysis_payload(model_request, durable)
    with pytest.raises(ModelAnalysisJobError, match="unsupported"):
        decode_model_analysis_payload(
            JobPayload.create(SchemaRef("org.example.other"), thaw_value(payload.value))
        )
