"""CAS-first durable publication for detector evaluation reports."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.core import DetectorEvaluationId, Digest, EvaluationRunId
from leo_flow.contracts.evaluation import DetectorEvaluationRef
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.ports import BlobReader, BlobWriter

from .evaluation import (
    DETECTOR_EVALUATION_FORMAT_ID,
    DETECTOR_EVALUATION_MEDIA_TYPE,
    DetectorEvaluationReport,
)
from .evaluation_codec import (
    MAX_DETECTOR_EVALUATION_BYTES,
    decode_detector_evaluation,
    encode_detector_evaluation,
)


class DetectorEvaluationPersistenceError(RuntimeError):
    pass


class DetectorEvaluationNotFound(DetectorEvaluationPersistenceError):
    pass


class DetectorEvaluationIntegrityError(DetectorEvaluationPersistenceError):
    pass


@dataclass(frozen=True)
class EvaluationCatalogProjection:
    evaluation_id: str
    run_id: str
    dataset_snapshot_id: str
    dataset_snapshot_digest: Digest
    feature_membership_digest: Digest
    threshold_rule_id: str
    threshold_rule_digest: Digest
    calibration_dataset_id: str
    calibration_split: str
    method_count: int
    union_window_count: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class CatalogedEvaluation:
    projection: EvaluationCatalogProjection
    report_object: ObjectRef

    @property
    def ref(self) -> DetectorEvaluationRef:
        return DetectorEvaluationRef(
            DetectorEvaluationId(self.projection.evaluation_id),
            EvaluationRunId(self.projection.run_id),
            self.report_object.digest,
            self.report_object,
        )


class EvaluationCatalog(Protocol):
    def publish(
        self,
        projection: EvaluationCatalogProjection,
        report_object: ObjectRef,
        report: DetectorEvaluationReport,
        *,
        idempotency_key: str,
    ) -> DetectorEvaluationRef: ...

    def get(self, ref: DetectorEvaluationRef) -> CatalogedEvaluation | None: ...


class _BlobStore(BlobWriter, BlobReader, Protocol):
    pass


@dataclass(frozen=True)
class DurableEvaluationView:
    ref: DetectorEvaluationRef
    report: DetectorEvaluationReport


class DurableDetectorEvaluationRepository:
    def __init__(self, blobs: _BlobStore, catalog: EvaluationCatalog) -> None:
        self._blobs = blobs
        self._catalog = catalog

    def publish(
        self,
        run_id: EvaluationRunId,
        report: DetectorEvaluationReport,
        *,
        idempotency_key: str,
    ) -> DetectorEvaluationRef:
        if not idempotency_key:
            raise ValueError("idempotency_key cannot be empty")
        payload = encode_detector_evaluation(report)
        projection = evaluation_projection(run_id, report)
        report_object = self._blobs.put(
            io.BytesIO(payload),
            expected_digest=Digest.sha256(payload),
            expected_bytes=len(payload),
            media_type=DETECTOR_EVALUATION_MEDIA_TYPE,
            format_id=DETECTOR_EVALUATION_FORMAT_ID,
            idempotency_key=f"{idempotency_key}:report",
        )
        return self._catalog.publish(
            projection, report_object, report, idempotency_key=idempotency_key
        )

    def open(
        self, ref: DetectorEvaluationRef
    ) -> AbstractContextManager[DurableEvaluationView]:
        return self._open(ref)

    @contextmanager
    def _open(self, ref: DetectorEvaluationRef) -> Iterator[DurableEvaluationView]:
        cataloged = self._catalog.get(ref)
        if cataloged is None:
            raise DetectorEvaluationNotFound("no exact detector evaluation exists")
        obj = cataloged.report_object
        if (
            obj != ref.report_object
            or obj.digest != ref.report_digest
            or obj.media_type != DETECTOR_EVALUATION_MEDIA_TYPE
            or obj.format_id != DETECTOR_EVALUATION_FORMAT_ID
            or obj.byte_count > MAX_DETECTOR_EVALUATION_BYTES
        ):
            raise DetectorEvaluationIntegrityError("report object metadata is invalid")
        metadata = self._blobs.head(obj)
        if metadata.ref != obj or not metadata.verified:
            raise DetectorEvaluationIntegrityError("report object was not verified")
        with self._blobs.open(obj) as stream:
            payload = stream.read(MAX_DETECTOR_EVALUATION_BYTES + 1)
        if len(payload) != obj.byte_count or Digest.sha256(payload) != obj.digest:
            raise DetectorEvaluationIntegrityError("report bytes differ from catalog")
        try:
            report = decode_detector_evaluation(payload)
        except ValueError as error:
            raise DetectorEvaluationIntegrityError(
                "report bytes are malformed"
            ) from error
        if evaluation_projection(ref.run_id, report) != cataloged.projection:
            raise DetectorEvaluationIntegrityError("report disagrees with catalog")
        yield DurableEvaluationView(ref, report)


def evaluation_projection(
    run_id: EvaluationRunId, report: DetectorEvaluationReport
) -> EvaluationCatalogProjection:
    method_ids = tuple(method.method_id for method in report.methods)
    if len(method_ids) != len(set(method_ids)):
        raise DetectorEvaluationIntegrityError(
            "report method identities are duplicated"
        )
    for method in report.methods:
        splits = tuple(item.split for item in method.by_split)
        if set(splits) != {"train", "validation", "locked_test"} or len(splits) != 3:
            raise DetectorEvaluationIntegrityError(
                "every report method requires exactly train, validation, and locked_test"
            )
    if report.overall_association.method_ids != method_ids:
        raise DetectorEvaluationIntegrityError(
            "report method summary and association identities differ"
        )
    digest = report.digest
    return EvaluationCatalogProjection(
        evaluation_id=f"eval_{digest.value}",
        run_id=str(run_id),
        dataset_snapshot_id=report.dataset_snapshot_id,
        dataset_snapshot_digest=report.dataset_snapshot_digest,
        feature_membership_digest=report.feature_membership_digest,
        threshold_rule_id=report.threshold_rule_id,
        threshold_rule_digest=report.threshold_rule_digest,
        calibration_dataset_id=report.threshold_calibration_dataset_id,
        calibration_split=report.threshold_calibration_split,
        method_count=len(report.methods),
        union_window_count=report.overall_association.union_window_count,
        warnings=report.warnings,
    )
