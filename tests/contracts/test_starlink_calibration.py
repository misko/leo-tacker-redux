from __future__ import annotations

from dataclasses import replace

import pytest

from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    RecordingId,
    SchemaRef,
)
from leo_flow.contracts.starlink_calibration import (
    StarlinkCalibrationCorpusItemV0_1,
    StarlinkCalibrationCorpusRole,
    StarlinkCalibrationCorpusV0_1,
)


def _item(role: StarlinkCalibrationCorpusRole, suffix: str):
    injection = role is StarlinkCalibrationCorpusRole.POSITIVE_INJECTION
    return StarlinkCalibrationCorpusItemV0_1(
        f"slcalitem_{suffix}",
        "slcalcell_radio20_rx0_ch1_lower",
        role,
        RecordingId(f"rec_{suffix}"),
        Digest.sha256(f"recording-{suffix}".encode()),
        f"slcandidate_{suffix}",
        Digest.sha256(f"candidate-{suffix}".encode()),
        0.2,
        ArtifactRef(
            f"truth-{suffix}",
            Digest.sha256(f"truth-{suffix}".encode()),
            SchemaRef("org.leo-flow.calibration-truth-assertion", V0_1),
        ),
        ArtifactRef(
            f"injection-{suffix}",
            Digest.sha256(f"injection-{suffix}".encode()),
            SchemaRef("org.leo-flow.starlink-positive-injection", V0_1),
        )
        if injection
        else None,
        -9.0 if injection else None,
    )


def test_corpus_requires_disjoint_truth_roles_and_exact_injection_identity() -> None:
    corpus = StarlinkCalibrationCorpusV0_1(
        SchemaRef(StarlinkCalibrationCorpusV0_1.SCHEMA_ID, V0_1),
        "slcalcorpus_radio20_rx0_ch1_lower",
        Digest.sha256(b"cell-plan"),
        (
            _item(StarlinkCalibrationCorpusRole.TRAIN_NULL, "train"),
            _item(StarlinkCalibrationCorpusRole.HOLDOUT_NULL, "holdout"),
            _item(StarlinkCalibrationCorpusRole.POSITIVE_INJECTION, "positive"),
        ),
    )

    assert corpus.digest == corpus.digest
    with pytest.raises(ValueError, match="requires train null"):
        replace(corpus, items=corpus.items[:2])
    with pytest.raises(ValueError, match="requires exact injection"):
        replace(corpus.items[2], injection_ref=None)
    with pytest.raises(ValueError, match="cannot carry an injection"):
        replace(corpus.items[0], injection_ref=corpus.items[2].injection_ref)


def test_corpus_cannot_pool_cells_or_repeat_a_whole_search() -> None:
    train = _item(StarlinkCalibrationCorpusRole.TRAIN_NULL, "train")
    holdout = _item(StarlinkCalibrationCorpusRole.HOLDOUT_NULL, "holdout")
    positive = _item(StarlinkCalibrationCorpusRole.POSITIVE_INJECTION, "positive")
    with pytest.raises(ValueError, match="cannot pool"):
        StarlinkCalibrationCorpusV0_1(
            SchemaRef(StarlinkCalibrationCorpusV0_1.SCHEMA_ID, V0_1),
            "slcalcorpus_mixed",
            Digest.sha256(b"cell-plan"),
            (train, replace(holdout, cell_id="slcalcell_other"), positive),
        )
    with pytest.raises(ValueError, match="cannot occur in two"):
        StarlinkCalibrationCorpusV0_1(
            SchemaRef(StarlinkCalibrationCorpusV0_1.SCHEMA_ID, V0_1),
            "slcalcorpus_duplicate",
            Digest.sha256(b"cell-plan"),
            (train, replace(holdout, candidate_id=train.candidate_id), positive),
        )
