from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.analysis.dataset.association import MethodAssociationReport
from leo_flow.analysis.dataset.evaluation import (
    BinaryClassificationCounts,
    DetectorEvaluationReport,
    MethodEvaluation,
    SplitAssociationReport,
    SplitMethodReport,
)
from leo_flow.analysis.dataset.evaluation_codec import (
    MalformedDetectorEvaluationError,
    decode_detector_evaluation,
    encode_detector_evaluation,
)
from leo_flow.analysis.dataset.evaluation_persistence import (
    CatalogedEvaluation,
    DetectorEvaluationIntegrityError,
    DurableDetectorEvaluationRepository,
    EvaluationCatalog,
    EvaluationCatalogProjection,
)
from leo_flow.contracts.core import Digest, EvaluationRunId, SchemaRef
from leo_flow.contracts.evaluation import DetectorEvaluationRef
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.filesystem import FileSystemBlobStore


def evaluation_report() -> DetectorEvaluationReport:
    association = MethodAssociationReport(
        ("energy@1",), ((0.25,),), ((1.0,),), ((4,),), ((400,),), (4,), 4, (0,)
    )
    truth = BinaryClassificationCounts(1, 0, 1, 0, 2, 2, 0, 0, 0)
    splits = tuple(
        SplitMethodReport(name, 2, 2, 4, 4, 0, 2, truth)
        for name in ("train", "validation", "locked_test")
    )
    return DetectorEvaluationReport(
        SchemaRef(DetectorEvaluationReport.SCHEMA_ID),
        "dataset_fixture",
        Digest.sha256(b"snapshot"),
        Digest.sha256(b"membership"),
        "rule_fixture",
        Digest.sha256(b"rule"),
        "dataset_calibration",
        "train",
        (MethodEvaluation("energy@1", 0.5, "normalized", splits),),
        association,
        tuple(
            SplitAssociationReport(name, association)
            for name in ("train", "validation", "locked_test")
        ),
        ("fixture-warning",),
    )


class MemoryCatalog(EvaluationCatalog):
    def __init__(self) -> None:
        self.entry: CatalogedEvaluation | None = None

    def publish(
        self,
        projection: EvaluationCatalogProjection,
        report_object: ObjectRef,
        report: DetectorEvaluationReport,
        *,
        idempotency_key: str,
    ) -> DetectorEvaluationRef:
        del report, idempotency_key
        candidate = CatalogedEvaluation(projection, report_object)
        if self.entry is not None and self.entry != candidate:
            raise RuntimeError("conflict")
        self.entry = candidate
        return candidate.ref

    def get(self, ref: DetectorEvaluationRef) -> CatalogedEvaluation | None:
        return self.entry if self.entry is not None and self.entry.ref == ref else None


def test_codec_is_strict_and_round_trips() -> None:
    report = evaluation_report()
    payload = encode_detector_evaluation(report)
    assert decode_detector_evaluation(payload) == report
    with pytest.raises(MalformedDetectorEvaluationError, match="canonical"):
        decode_detector_evaluation(b"{ }")


def test_repository_writes_one_report_object_and_round_trips(tmp_path) -> None:
    blobs = FileSystemBlobStore(tmp_path / "cas")
    repository = DurableDetectorEvaluationRepository(blobs, MemoryCatalog())
    report = evaluation_report()
    ref = repository.publish(
        EvaluationRunId("erun_fixture"), report, idempotency_key="evaluation:fixture"
    )
    assert str(ref.evaluation_id) == f"eval_{report.digest.value}"
    assert ref.report_digest == report.digest
    with repository.open(ref) as opened:
        assert opened.report == report
    assert len(tuple((tmp_path / "cas" / "sha256").glob("*/*"))) == 1


def test_reader_rejects_catalog_projection_disagreement(tmp_path) -> None:
    blobs = FileSystemBlobStore(tmp_path / "cas")
    catalog = MemoryCatalog()
    repository = DurableDetectorEvaluationRepository(blobs, catalog)
    ref = repository.publish(
        EvaluationRunId("erun_fixture"),
        evaluation_report(),
        idempotency_key="evaluation:fixture",
    )
    assert catalog.entry is not None
    catalog.entry = replace(
        catalog.entry,
        projection=replace(catalog.entry.projection, threshold_rule_id="changed"),
    )
    with (
        pytest.raises(DetectorEvaluationIntegrityError, match="disagrees"),
        repository.open(ref),
    ):
        pass


def test_projection_requires_every_frozen_split(tmp_path) -> None:
    report = evaluation_report()
    method = replace(report.methods[0], by_split=report.methods[0].by_split[:2])
    repository = DurableDetectorEvaluationRepository(
        FileSystemBlobStore(tmp_path / "cas"), MemoryCatalog()
    )
    with pytest.raises(DetectorEvaluationIntegrityError, match="exactly"):
        repository.publish(
            EvaluationRunId("erun_incomplete"),
            replace(report, methods=(method,)),
            idempotency_key="evaluation:incomplete",
        )
