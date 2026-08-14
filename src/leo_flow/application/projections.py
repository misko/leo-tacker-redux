"""Explicit write side for the otherwise read-only dashboard projections."""

from __future__ import annotations

from leo_flow.contracts.capture import RecordingManifest
from leo_flow.contracts.dashboard import (
    FeatureView,
    ModelView,
    RecordingSummary,
    StorageHealth,
)
from leo_flow.contracts.evaluation import DetectorEvaluationView
from leo_flow.contracts.features import FeatureSetBundle
from leo_flow.contracts.model import ModelRelease, ModelSnapshotBundle, ModelSnapshotRef
from leo_flow.dashboard import (
    ActivityProjection,
    DashboardJsonApplication,
    FeatureProjection,
    InMemoryDashboardRepository,
    ModelProjection,
    RecordingProjection,
)


class ProjectionInputError(ValueError):
    """A projection attempted to cross an unpublished or inconsistent boundary."""


class DashboardProjectionStore:
    """Own mutable DTO projections and expose only read-only query applications."""

    def __init__(self) -> None:
        self._recordings: list[RecordingProjection] = []
        self._activities: list[ActivityProjection] = []
        self._features: list[FeatureProjection] = []
        self._models: list[ModelProjection] = []
        self._evaluations: dict[str, DetectorEvaluationView] = {}
        self._evaluation_runs: dict[str, str] = {}
        self._recording_ids: set[str] = set()
        self._published_models: dict[str, ModelSnapshotRef] = {}
        self._next_sequence = 0

    def project_recording(
        self,
        manifest: RecordingManifest,
        *,
        recording_object_available: bool,
        analysis_state: str,
    ) -> None:
        if not analysis_state:
            raise ProjectionInputError("analysis_state must be non-empty")
        self._recordings.append(
            RecordingProjection(
                summary=RecordingSummary(
                    recording_id=manifest.recording_id,
                    radio_id=manifest.radio_id,
                    started_utc_ns=manifest.capture_started_utc_ns,
                    finished_utc_ns=manifest.capture_finished_utc_ns,
                    activity_kinds=tuple(
                        activity.kind for activity in manifest.activities
                    ),
                    analysis_state=analysis_state,
                ),
                segment_count=len(manifest.segments),
                recording_object_available=recording_object_available,
                projection_sequence=self._sequence(),
            )
        )
        self._recording_ids.add(str(manifest.recording_id))
        for activity in manifest.activities:
            self._activities.append(
                ActivityProjection(
                    activity_id=str(activity.activity_id),
                    recording_id=manifest.recording_id,
                    radio_id=manifest.radio_id,
                    kind=activity.kind,
                    started_utc_ns=activity.started_utc_ns,
                    projection_sequence=self._sequence(),
                )
            )

    def project_features(self, bundle: FeatureSetBundle) -> None:
        if str(bundle.recording_id) not in self._recording_ids:
            raise ProjectionInputError(
                "feature set cannot be projected before its recording"
            )
        feature_ids = [observation.feature_id for observation in bundle.observations]
        if len(feature_ids) != len(set(feature_ids)):
            raise ProjectionInputError("feature set contains duplicate feature IDs")
        for observation in bundle.observations:
            self._features.append(
                FeatureProjection(
                    recording_id=bundle.recording_id,
                    view=FeatureView(
                        feature_id=str(observation.feature_id),
                        method_id=observation.method_id,
                        score=observation.score,
                        score_semantics=observation.score_semantics,
                    ),
                    projection_sequence=self._sequence(),
                )
            )

    def project_model(
        self,
        bundle: ModelSnapshotBundle,
        published_ref: ModelSnapshotRef,
        *,
        release: ModelRelease | None = None,
    ) -> None:
        if (
            bundle.model_snapshot_id != published_ref.model_snapshot_id
            or bundle.model_run_id != published_ref.model_run_id
        ):
            raise ProjectionInputError("model bundle and published reference differ")
        identity = str(bundle.model_snapshot_id)
        if release is None:
            existing = self._published_models.get(identity)
            if existing is not None and existing != published_ref:
                raise ProjectionInputError(
                    "model ID already identifies another published reference"
                )
            self._published_models[identity] = published_ref
        else:
            if self._published_models.get(identity) != published_ref:
                raise ProjectionInputError(
                    "model release cannot be projected before model publication"
                )
            if release.model_ref != published_ref:
                raise ProjectionInputError(
                    "release and published model references differ"
                )
        self._models.append(
            ModelProjection(
                view=ModelView(
                    model_snapshot_id=bundle.model_snapshot_id,
                    release_alias=release.alias if release is not None else None,
                    parameter_count=len(bundle.parameters),
                    warnings=bundle.warnings,
                ),
                projection_sequence=self._sequence(),
            )
        )

    def project_evaluation(self, view: DetectorEvaluationView) -> None:
        """Retain one immutable evaluation under both of its exact identities."""

        evaluation_id = str(view.ref.evaluation_id)
        run_id = str(view.ref.run_id)
        existing = self._evaluations.get(evaluation_id)
        if existing is not None and existing != view:
            raise ProjectionInputError(
                "evaluation ID already identifies another published view"
            )
        existing_evaluation_id = self._evaluation_runs.get(run_id)
        if (
            existing_evaluation_id is not None
            and existing_evaluation_id != evaluation_id
        ):
            raise ProjectionInputError(
                "evaluation run ID already identifies another published view"
            )
        self._evaluations[evaluation_id] = view
        self._evaluation_runs[run_id] = evaluation_id

    def repository(
        self,
        *,
        storage_health: StorageHealth | None = None,
        page_size: int = 50,
    ) -> InMemoryDashboardRepository:
        return InMemoryDashboardRepository(
            recordings=self._recordings,
            activities=self._activities,
            features=self._features,
            models=self._models,
            evaluations=tuple(self._evaluations.values()),
            storage_health=storage_health or StorageHealth(False, None, None),
            page_size=page_size,
        )

    def json_application(
        self,
        *,
        storage_health: StorageHealth | None = None,
        page_size: int = 50,
    ) -> DashboardJsonApplication:
        return DashboardJsonApplication(
            self.repository(storage_health=storage_health, page_size=page_size)
        )

    def _sequence(self) -> int:
        value = self._next_sequence
        self._next_sequence += 1
        return value
