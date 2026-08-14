from __future__ import annotations

from leo_flow.contracts.capture import ActivityKind
from leo_flow.contracts.core import (
    DetectorEvaluationId,
    Digest,
    EvaluationRunId,
    ModelSnapshotId,
    RadioId,
    RecordingId,
    UtcNs,
)
from leo_flow.contracts.dashboard import (
    FeatureView,
    ModelView,
    RecordingSummary,
    StorageHealth,
    TrackView,
)
from leo_flow.contracts.evaluation import (
    DetectorEvaluationRef,
    DetectorEvaluationView,
    DetectorMethodSplitSummary,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.dashboard.repository import (
    ActivityProjection,
    FeatureProjection,
    InMemoryDashboardRepository,
    ModelProjection,
    RecordingProjection,
    TrackProjection,
)

RADIO_A = RadioId("radio_a")
RADIO_B = RadioId("radio_b")
MODEL_A = ModelSnapshotId("model_a")
EVALUATION_DIGEST = Digest.sha256(b"dashboard-evaluation-report")
EVALUATION_ID = DetectorEvaluationId(f"eval_{EVALUATION_DIGEST.value}")
EVALUATION_RUN_ID = EvaluationRunId("erun_dashboard")


def evaluation() -> DetectorEvaluationView:
    report_object = ObjectRef(
        EVALUATION_DIGEST,
        123,
        "application/json",
        "detector-evaluation-report-v0.1",
        f"cas:sha256:{EVALUATION_DIGEST.value}",
    )
    methods = tuple(
        DetectorMethodSplitSummary(
            "energy@1",
            split,
            2.5,
            "power",
            2,
            2,
            4,
            3,
            1,
            2,
            1,
            1,
            1,
            0,
            3,
            1,
        )
        for split in ("train", "validation", "locked_test")
    )
    return DetectorEvaluationView(
        DetectorEvaluationRef(
            EVALUATION_ID,
            EVALUATION_RUN_ID,
            EVALUATION_DIGEST,
            report_object,
        ),
        "dataset_dashboard",
        Digest.sha256(b"dataset"),
        Digest.sha256(b"membership"),
        "train-quantile-v1",
        Digest.sha256(b"rule"),
        "dataset_dashboard",
        "train",
        1,
        4,
        ("fixture warning",),
        methods,
    )


def recording(
    identity: str,
    radio: RadioId,
    started: int,
    state: str,
    sequence: int,
    *,
    available: bool = True,
    kinds: tuple[ActivityKind, ...] = (ActivityKind.DWELL,),
) -> RecordingProjection:
    return RecordingProjection(
        RecordingSummary(
            RecordingId(identity),
            radio,
            UtcNs(started),
            UtcNs(started + 10),
            kinds,
            state,
        ),
        segment_count=len(kinds),
        recording_object_available=available,
        projection_sequence=sequence,
    )


def repository(page_size: int = 2) -> InMemoryDashboardRepository:
    recordings = [
        recording("rec_1", RADIO_A, 100, "complete", 1),
        recording("rec_2", RADIO_A, 110, "partial", 2, kinds=(ActivityKind.SCAN,)),
        recording("rec_3", RADIO_B, 120, "failed", 3),
        recording("rec_4", RADIO_A, 130, "superseded", 4, available=False),
    ]
    activities = [
        ActivityProjection(
            "activity_1",
            RecordingId("rec_1"),
            RADIO_A,
            ActivityKind.DWELL,
            UtcNs(100),
            1,
        ),
        ActivityProjection(
            "activity_2",
            RecordingId("rec_2"),
            RADIO_A,
            ActivityKind.SCAN,
            UtcNs(110),
            2,
        ),
        ActivityProjection(
            "activity_3",
            RecordingId("rec_3"),
            RADIO_B,
            ActivityKind.DWELL,
            UtcNs(120),
            3,
        ),
        ActivityProjection(
            "activity_4",
            RecordingId("rec_4"),
            RADIO_A,
            ActivityKind.SCAN,
            UtcNs(130),
            4,
        ),
        ActivityProjection(
            "activity_5",
            RecordingId("rec_4"),
            RADIO_A,
            ActivityKind.DWELL,
            UtcNs(130),
            5,
        ),
    ]
    features = [
        FeatureProjection(
            RecordingId("rec_1"),
            FeatureView("feature_a", "glrt32", 2.0, "log_likelihood_ratio"),
            1,
        ),
        FeatureProjection(
            RecordingId("rec_1"),
            FeatureView("feature_b", "coarse-E", 3.0, "snr_like"),
            2,
        ),
        FeatureProjection(
            RecordingId("rec_1"),
            FeatureView("feature_c", "glrt32", 4.0, "log_likelihood_ratio"),
            3,
        ),
    ]
    models = [
        ModelProjection(ModelView(MODEL_A, None, 2, ("draft",)), 1),
        ModelProjection(ModelView(MODEL_A, "production", 2, ()), 2),
        ModelProjection(
            ModelView(ModelSnapshotId("model_b"), None, 0, ("rank-deficient",)), 3
        ),
    ]
    tracks = [
        TrackProjection(
            RADIO_A, TrackView("track_1", MODEL_A, UtcNs(105), UtcNs(115)), 1
        ),
        TrackProjection(
            RADIO_B, TrackView("track_2", MODEL_A, UtcNs(120), UtcNs(125)), 2
        ),
        TrackProjection(
            RADIO_A, TrackView("track_3", MODEL_A, UtcNs(130), UtcNs(140)), 3
        ),
    ]
    return InMemoryDashboardRepository(
        recordings=recordings,
        activities=activities,
        features=features,
        models=models,
        evaluations=(evaluation(),),
        tracks=tracks,
        storage_health=StorageHealth(True, 1_000, 250),
        page_size=page_size,
    )
