from __future__ import annotations

from dataclasses import replace

from leo_flow.contracts.capture import ActivityKind
from leo_flow.contracts.capture_batch import (
    CaptureBatchMode,
    PairedAnalysisEligibility,
)
from leo_flow.contracts.core import (
    V0_1,
    CaptureAttemptId,
    CaptureBatchId,
    DetectorEvaluationId,
    Digest,
    EvaluationRunId,
    ModelSnapshotId,
    PlanId,
    RadioId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.dashboard import (
    FeatureView,
    ModelView,
    RecordingSummary,
    StorageHealth,
    TrackView,
)
from leo_flow.contracts.dashboard_batch import (
    CaptureAttemptDashboardView,
    CaptureBatchDashboardView,
    CoordinationClaim,
    DashboardAnalysisState,
    DashboardCaptureState,
)
from leo_flow.contracts.evaluation import (
    DetectorEvaluationRef,
    DetectorEvaluationView,
    DetectorMethodSplitSummary,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.dashboard.repository import (
    ActivityProjection,
    CaptureBatchProjection,
    FeatureProjection,
    InMemoryDashboardRepository,
    ModelProjection,
    RecordingProjection,
    TrackProjection,
)

RADIO_A = RadioId("radio_a")
RADIO_B = RadioId("radio_b")
GAUSS_RADIO_15 = RadioId("radio_pluto_v5_canary_15")
GAUSS_RADIO_20 = RadioId("radio_pluto_5d4d")
GAUSS_RADIO_21 = RadioId("radio_pluto_19f2")
MODEL_A = ModelSnapshotId("model_a")
EVALUATION_DIGEST = Digest.sha256(b"dashboard-evaluation-report")
EVALUATION_ID = DetectorEvaluationId(f"eval_{EVALUATION_DIGEST.value}")
EVALUATION_RUN_ID = EvaluationRunId("erun_dashboard")
BATCH_READY = CaptureBatchId("cbatch_ready")
BATCH_PENDING = CaptureBatchId("cbatch_pending")
BATCH_PEER_FAILED = CaptureBatchId("cbatch_peer_failed")
BATCH_EXCESSIVE_SKEW = CaptureBatchId("cbatch_excessive_skew")
GAUSS_BATCH_20_21 = CaptureBatchId("cbatch_gauss_20_21")


def _attempt(
    suffix: str,
    radio: RadioId,
    requested: int,
    *,
    capture_state: DashboardCaptureState,
    observed: int | None = None,
    recording_id: str | None = None,
    failure_reason: str | None = None,
    analysis_state: DashboardAnalysisState = DashboardAnalysisState.UNAVAILABLE,
    result_available: bool = False,
) -> CaptureAttemptDashboardView:
    return CaptureAttemptDashboardView(
        CaptureAttemptId(f"cattempt_{suffix}"),
        radio,
        PlanId(f"plan_{suffix}"),
        UtcNs(requested),
        capture_state,
        UtcNs(observed) if observed is not None else None,
        RecordingId(recording_id) if recording_id is not None else None,
        failure_reason,
        analysis_state,
        result_available,
    )


def capture_batches() -> list[CaptureBatchProjection]:
    schema = SchemaRef(CaptureBatchDashboardView.SCHEMA_ID, V0_1)
    return [
        CaptureBatchProjection(
            CaptureBatchDashboardView(
                schema,
                BATCH_READY,
                CaptureBatchMode.COORDINATED,
                CoordinationClaim.MEASURED_SOFTWARE_COORDINATION,
                (
                    _attempt(
                        "ready_a",
                        RADIO_A,
                        400,
                        capture_state=DashboardCaptureState.SUCCEEDED,
                        observed=405,
                        recording_id="rec_ready_a",
                        analysis_state=DashboardAnalysisState.COMPLETE,
                        result_available=True,
                    ),
                    _attempt(
                        "ready_b",
                        RADIO_B,
                        400,
                        capture_state=DashboardCaptureState.SUCCEEDED,
                        observed=410,
                        recording_id="rec_ready_b",
                        analysis_state=DashboardAnalysisState.COMPLETE,
                        result_available=True,
                    ),
                ),
                2,
                0,
                5,
                10,
                PairedAnalysisEligibility.ELIGIBLE,
            ),
            1,
        ),
        CaptureBatchProjection(
            CaptureBatchDashboardView(
                schema,
                BATCH_PENDING,
                CaptureBatchMode.INDEPENDENT,
                CoordinationClaim.NONE,
                (
                    _attempt(
                        "pending_a",
                        RADIO_A,
                        300,
                        capture_state=DashboardCaptureState.SUCCEEDED,
                        observed=304,
                        recording_id="rec_pending_a",
                        analysis_state=DashboardAnalysisState.PENDING,
                    ),
                    _attempt(
                        "pending_b",
                        RADIO_B,
                        320,
                        capture_state=DashboardCaptureState.PENDING,
                    ),
                ),
                1,
                20,
                None,
                None,
                PairedAnalysisEligibility.PENDING,
            ),
            2,
        ),
        CaptureBatchProjection(
            CaptureBatchDashboardView(
                schema,
                BATCH_PEER_FAILED,
                CaptureBatchMode.INDEPENDENT,
                CoordinationClaim.NONE,
                (
                    _attempt(
                        "peer_failed_a",
                        RADIO_A,
                        200,
                        capture_state=DashboardCaptureState.SUCCEEDED,
                        observed=203,
                        recording_id="rec_solo_preserved",
                        analysis_state=DashboardAnalysisState.COMPLETE,
                        result_available=True,
                    ),
                    _attempt(
                        "peer_failed_b",
                        RADIO_B,
                        220,
                        capture_state=DashboardCaptureState.FAILED,
                        observed=225,
                        failure_reason="radio_unreachable",
                    ),
                ),
                2,
                20,
                22,
                None,
                PairedAnalysisEligibility.INELIGIBLE,
            ),
            3,
        ),
        CaptureBatchProjection(
            CaptureBatchDashboardView(
                schema,
                BATCH_EXCESSIVE_SKEW,
                CaptureBatchMode.COORDINATED,
                CoordinationClaim.MEASURED_SOFTWARE_COORDINATION,
                (
                    _attempt(
                        "skew_a",
                        RADIO_A,
                        100,
                        capture_state=DashboardCaptureState.SUCCEEDED,
                        observed=105,
                        recording_id="rec_skew_a",
                        analysis_state=DashboardAnalysisState.COMPLETE,
                        result_available=True,
                    ),
                    _attempt(
                        "skew_b",
                        RADIO_B,
                        100,
                        capture_state=DashboardCaptureState.SUCCEEDED,
                        observed=155,
                        recording_id="rec_skew_b",
                        analysis_state=DashboardAnalysisState.COMPLETE,
                        result_available=True,
                    ),
                ),
                2,
                0,
                50,
                10,
                PairedAnalysisEligibility.INELIGIBLE,
            ),
            4,
        ),
    ]


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
        capture_batches=capture_batches(),
        storage_health=StorageHealth(True, 1_000, 250),
        page_size=page_size,
    )


def gauss_three_radio_repository() -> InMemoryDashboardRepository:
    """Represent the single-radio .15 lane beside one .20/.21 capture batch."""

    ready = capture_batches()[0]
    attempt_20 = replace(
        ready.view.attempts[0],
        radio_id=GAUSS_RADIO_20,
        recording_id=RecordingId("rec_gauss_20"),
    )
    attempt_21 = replace(
        ready.view.attempts[1],
        radio_id=GAUSS_RADIO_21,
        recording_id=RecordingId("rec_gauss_21"),
    )
    batch = CaptureBatchProjection(
        replace(
            ready.view,
            batch_id=GAUSS_BATCH_20_21,
            attempts=(attempt_20, attempt_21),
        ),
        ready.projection_sequence,
    )
    recordings = [
        recording("rec_gauss_15", GAUSS_RADIO_15, 390, "complete", 1),
        recording("rec_gauss_20", GAUSS_RADIO_20, 405, "complete", 2),
        recording("rec_gauss_21", GAUSS_RADIO_21, 410, "complete", 3),
    ]
    activities = [
        ActivityProjection(
            f"activity_gauss_{suffix}",
            row.summary.recording_id,
            row.summary.radio_id,
            ActivityKind.DWELL,
            row.summary.started_utc_ns,
            sequence,
        )
        for sequence, (suffix, row) in enumerate(
            zip(("15", "20", "21"), recordings, strict=True), start=1
        )
    ]
    features = [
        FeatureProjection(
            row.summary.recording_id,
            FeatureView(
                f"feature_gauss_{suffix}",
                "gauss-quality-v1",
                float(sequence),
                "quality_score",
            ),
            sequence,
        )
        for sequence, (suffix, row) in enumerate(
            zip(("15", "20", "21"), recordings, strict=True), start=1
        )
    ]
    return InMemoryDashboardRepository(
        recordings=recordings,
        activities=activities,
        features=features,
        capture_batches=(batch,),
        storage_health=StorageHealth(True, 1_000, 250),
        page_size=50,
    )
