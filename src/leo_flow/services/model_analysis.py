"""Typed, exact-dataset preparation boundary for cross-recording model jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from leo_flow.analysis.dataset import DatasetSnapshotReader, DatasetSnapshotRef
from leo_flow.analysis.model import resolve_model_dataset
from leo_flow.analysis.model.persistence import model_snapshot_projection
from leo_flow.contracts._validation import thaw_value
from leo_flow.contracts.core import (
    ArtifactRef,
    DatasetSnapshotId,
    Digest,
    DigestAlgorithm,
    EphemerisSnapshotId,
    HardwareSnapshotId,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.ephemeris import EphemerisSnapshotRef, EphemerisSource
from leo_flow.contracts.hardware import HardwareMetadataSnapshotRef
from leo_flow.contracts.model import (
    FeatureDatasetSnapshot,
    FeatureDatasetSnapshotRef,
    ModelAnalysisRequest,
    ModelSnapshotBundle,
)
from leo_flow.contracts.ports import (
    EphemerisReader,
    FeatureSetReader,
    HardwareMetadataReader,
    ModelFitter,
)
from leo_flow.jobs.contracts import JobLease, JobPayload, JobType

MODEL_ANALYSIS_JOB_SCHEMA = SchemaRef("org.leo-flow.model-analysis-job")


class ModelAnalysisJobError(ValueError):
    """A job is not the exact supported model-analysis command."""


@dataclass(frozen=True)
class PreparedModelAnalysis:
    request: ModelAnalysisRequest
    durable_dataset_ref: DatasetSnapshotRef
    bundle: ModelSnapshotBundle


class ModelAnalysisPreparer(Protocol):
    def prepare(self, lease: JobLease) -> PreparedModelAnalysis: ...


class ModelAnalysisCommitter(Protocol):
    def commit(
        self, lease: JobLease, prepared: PreparedModelAnalysis
    ) -> ArtifactRef: ...


ModelFitterFactory = Callable[[FeatureDatasetSnapshot], ModelFitter]


class ModelAnalysisJobPreparer:
    """Resolve rich dataset identity before constructing and invoking a fitter."""

    def __init__(
        self,
        datasets: DatasetSnapshotReader,
        features: FeatureSetReader,
        ephemerides: EphemerisReader,
        hardware: HardwareMetadataReader,
        fitter_factory: ModelFitterFactory,
    ) -> None:
        self._datasets = datasets
        self._features = features
        self._ephemerides = ephemerides
        self._hardware = hardware
        self._fitter_factory = fitter_factory

    def prepare(self, lease: JobLease) -> PreparedModelAnalysis:
        if lease.job_type is not JobType.MODEL_ANALYSIS:
            raise ModelAnalysisJobError("worker accepts model-analysis jobs only")
        request, durable_ref = decode_model_analysis_payload(lease.payload)
        dataset = resolve_model_dataset(
            self._datasets, durable_ref, request.dataset_snapshot_ref
        )
        bundle = self._fitter_factory(dataset).fit(
            request, self._features, self._ephemerides, self._hardware
        )
        model_snapshot_projection(request, bundle)
        return PreparedModelAnalysis(request, durable_ref, bundle)


class ModelAnalysisJobProcessor:
    """Prepare a claimed model job, then cross the fenced commit boundary."""

    def __init__(
        self, preparer: ModelAnalysisPreparer, committer: ModelAnalysisCommitter
    ) -> None:
        self._preparer = preparer
        self._committer = committer

    def process(self, lease: JobLease) -> ArtifactRef:
        return self.execute(lease)

    def execute(self, lease: JobLease) -> ArtifactRef:
        """Execute an already-claimed lease for the typed analysis router."""

        if lease.job_type is not JobType.MODEL_ANALYSIS:
            raise ModelAnalysisJobError("worker accepts model-analysis jobs only")
        return self._committer.commit(lease, self._preparer.prepare(lease))


def model_analysis_payload(
    request: ModelAnalysisRequest, durable_dataset_ref: DatasetSnapshotRef
) -> JobPayload:
    if (
        durable_dataset_ref.snapshot_id != request.dataset_snapshot_ref.snapshot_id
        or durable_dataset_ref.feature_membership_digest
        != request.dataset_snapshot_ref.membership_digest
    ):
        raise ModelAnalysisJobError(
            "durable dataset reference differs from model request"
        )
    return JobPayload.create(
        MODEL_ANALYSIS_JOB_SCHEMA,
        {
            "request": _request_document(request),
            "durable_dataset_ref": {
                "snapshot_id": str(durable_dataset_ref.snapshot_id),
                "feature_membership_digest": _digest_document(
                    durable_dataset_ref.feature_membership_digest
                ),
                "snapshot_digest": _digest_document(
                    durable_dataset_ref.snapshot_digest
                ),
            },
        },
    )


def decode_model_analysis_payload(
    payload: JobPayload,
) -> tuple[ModelAnalysisRequest, DatasetSnapshotRef]:
    if payload.schema != MODEL_ANALYSIS_JOB_SCHEMA:
        raise ModelAnalysisJobError("unsupported model-analysis job schema")
    document = cast(dict[str, object], thaw_value(payload.value))
    _keys(document, {"request", "durable_dataset_ref"}, "payload")
    try:
        request = _request(document["request"])
        durable = _durable_dataset_ref(document["durable_dataset_ref"])
        if (
            durable.snapshot_id != request.dataset_snapshot_ref.snapshot_id
            or durable.feature_membership_digest
            != request.dataset_snapshot_ref.membership_digest
        ):
            raise ModelAnalysisJobError(
                "durable dataset reference differs from model request"
            )
        return request, durable
    except ModelAnalysisJobError:
        raise
    except (TypeError, ValueError) as error:
        raise ModelAnalysisJobError(str(error)) from error


def _request(value: object) -> ModelAnalysisRequest:
    item = _object(value, "request")
    _keys(
        item,
        {
            "schema",
            "dataset_snapshot_ref",
            "hardware_metadata_snapshot_refs",
            "ephemeris_snapshot_refs",
            "model_config_ref",
            "algorithm_ref",
        },
        "request",
    )
    dataset = _object(item["dataset_snapshot_ref"], "dataset_snapshot_ref")
    _keys(dataset, {"snapshot_id", "membership_digest"}, "dataset_snapshot_ref")
    hardware = _indexed(
        item["hardware_metadata_snapshot_refs"], "hardware_metadata_snapshot_refs"
    )
    ephemerides = _indexed(item["ephemeris_snapshot_refs"], "ephemeris_snapshot_refs")
    return ModelAnalysisRequest(
        schema=_schema(item["schema"], "request.schema"),
        dataset_snapshot_ref=FeatureDatasetSnapshotRef(
            DatasetSnapshotId(_string(dataset["snapshot_id"], "dataset.snapshot_id")),
            _digest(dataset["membership_digest"], "dataset.membership_digest"),
        ),
        hardware_metadata_snapshot_refs=tuple(
            _hardware_ref(entry, index) for index, entry in enumerate(hardware)
        ),
        ephemeris_snapshot_refs=tuple(
            _ephemeris_ref(entry, index) for index, entry in enumerate(ephemerides)
        ),
        model_config_ref=_artifact(item["model_config_ref"], "model_config_ref"),
        algorithm_ref=_artifact(item["algorithm_ref"], "algorithm_ref"),
    )


def _durable_dataset_ref(value: object) -> DatasetSnapshotRef:
    item = _object(value, "durable_dataset_ref")
    _keys(
        item,
        {"snapshot_id", "feature_membership_digest", "snapshot_digest"},
        "durable_dataset_ref",
    )
    return DatasetSnapshotRef(
        DatasetSnapshotId(_string(item["snapshot_id"], "dataset.snapshot_id")),
        _digest(item["feature_membership_digest"], "feature_membership_digest"),
        _digest(item["snapshot_digest"], "snapshot_digest"),
    )


def _hardware_ref(value: object, index: int) -> HardwareMetadataSnapshotRef:
    name = f"hardware_metadata_snapshot_refs[{index}]"
    item = _object(value, name)
    _keys(item, {"snapshot_id", "digest"}, name)
    return HardwareMetadataSnapshotRef(
        HardwareSnapshotId(_string(item["snapshot_id"], f"{name}.snapshot_id")),
        _digest(item["digest"], f"{name}.digest"),
    )


def _ephemeris_ref(value: object, index: int) -> EphemerisSnapshotRef:
    name = f"ephemeris_snapshot_refs[{index}]"
    item = _object(value, name)
    _keys(item, {"snapshot_id", "source", "raw_digest", "normalized_digest"}, name)
    return EphemerisSnapshotRef(
        EphemerisSnapshotId(_string(item["snapshot_id"], f"{name}.snapshot_id")),
        EphemerisSource(_string(item["source"], f"{name}.source")),
        _digest(item["raw_digest"], f"{name}.raw_digest"),
        _digest(item["normalized_digest"], f"{name}.normalized_digest"),
    )


def _artifact(value: object, name: str) -> ArtifactRef:
    item = _object(value, name)
    _keys(item, {"artifact_id", "digest", "schema"}, name)
    return ArtifactRef(
        _string(item["artifact_id"], f"{name}.artifact_id"),
        _digest(item["digest"], f"{name}.digest"),
        None if item["schema"] is None else _schema(item["schema"], f"{name}.schema"),
    )


def _schema(value: object, name: str) -> SchemaRef:
    item = _object(value, name)
    _keys(item, {"schema_id", "version"}, name)
    return SchemaRef(
        _string(item["schema_id"], f"{name}.schema_id"),
        SchemaVersion.parse(_string(item["version"], f"{name}.version")),
    )


def _digest(value: object, name: str) -> Digest:
    item = _object(value, name)
    _keys(item, {"algorithm", "value"}, name)
    return Digest(
        DigestAlgorithm(_string(item["algorithm"], f"{name}.algorithm")),
        _string(item["value"], f"{name}.value"),
    )


def _request_document(value: ModelAnalysisRequest) -> dict[str, object]:
    return {
        "schema": _schema_document(value.schema),
        "dataset_snapshot_ref": {
            "snapshot_id": str(value.dataset_snapshot_ref.snapshot_id),
            "membership_digest": _digest_document(
                value.dataset_snapshot_ref.membership_digest
            ),
        },
        "hardware_metadata_snapshot_refs": {
            str(index): {
                "snapshot_id": str(ref.snapshot_id),
                "digest": _digest_document(ref.digest),
            }
            for index, ref in enumerate(value.hardware_metadata_snapshot_refs)
        },
        "ephemeris_snapshot_refs": {
            str(index): {
                "snapshot_id": str(ref.snapshot_id),
                "source": ref.source.value,
                "raw_digest": _digest_document(ref.raw_digest),
                "normalized_digest": _digest_document(ref.normalized_digest),
            }
            for index, ref in enumerate(value.ephemeris_snapshot_refs)
        },
        "model_config_ref": _artifact_document(value.model_config_ref),
        "algorithm_ref": _artifact_document(value.algorithm_ref),
    }


def _schema_document(value: SchemaRef) -> dict[str, object]:
    return {"schema_id": value.schema_id, "version": str(value.version)}


def _digest_document(value: Digest) -> dict[str, object]:
    return {"algorithm": value.algorithm.value, "value": value.value}


def _artifact_document(value: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": value.artifact_id,
        "digest": _digest_document(value.digest),
        "schema": None if value.schema is None else _schema_document(value.schema),
    }


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ModelAnalysisJobError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _indexed(value: object, name: str) -> list[object]:
    item = _object(value, name)
    expected = {str(index) for index in range(len(item))}
    if set(item) != expected:
        raise ModelAnalysisJobError(f"{name} indices are not contiguous")
    return [item[str(index)] for index in range(len(item))]


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ModelAnalysisJobError(f"{name} must be a string")
    return value


def _keys(value: dict[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ModelAnalysisJobError(f"{name} fields differ from the schema")
