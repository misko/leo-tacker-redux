from __future__ import annotations

from dataclasses import dataclass

import pytest

from leo_flow.analysis.dataset import DatasetSnapshotRef
from leo_flow.analysis.model import (
    ReceiverQualityAggregateConfig,
    ReceiverQualityAggregateModel,
)
from leo_flow.analysis.model.tracking_input_codec import encode_tracking_input
from leo_flow.analysis.model.tracking_input_persistence import (
    DurableTrackingInputView,
    TrackingInputIntegrityError,
    TrackingInputNotFoundError,
)
from leo_flow.contracts._validation import thaw_value
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    JobId,
    ReceiverChainId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.contracts.tracking_input import (
    TRACKING_INPUT_FORMAT_ID,
    TRACKING_INPUT_MEDIA_TYPE,
    TrackingInputSnapshotIdentity,
    TrackingInputSnapshotRef,
)
from leo_flow.contracts.tracking_model import TrackingModelAnalysisRequest
from leo_flow.jobs import InMemoryJobLeaseRepository, JobLease, JobPayload, JobType
from leo_flow.services.analysis_router import TypedAnalysisRouterCycle
from leo_flow.services.model_analysis import (
    MODEL_ANALYSIS_JOB_SCHEMA,
    TRACKING_MODEL_ANALYSIS_JOB_SCHEMA,
    ModelAnalysisJobDispatcher,
    ModelAnalysisJobError,
    PreparedTrackingModelAnalysis,
    TrackingModelAnalysisJobPreparer,
    decode_model_analysis_payload,
    decode_tracking_model_analysis_payload,
    model_analysis_payload,
    tracking_model_analysis_payload,
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
from tests.model_analysis.test_tracking_input_builder import _case, _observation


def _artifact(name: str, schema: str) -> ArtifactRef:
    return ArtifactRef(name, Digest.sha256(name.encode()), SchemaRef(schema))


def _tracking_fixture() -> tuple[
    TrackingModelAnalysisRequest, DurableTrackingInputView
]:
    snapshot = _case().freeze()
    payload = encode_tracking_input(snapshot)
    ref = TrackingInputSnapshotRef(
        snapshot.snapshot_id,
        snapshot.snapshot_digest,
        snapshot.membership_digest,
        ObjectRef(
            Digest.sha256(payload),
            len(payload),
            TRACKING_INPUT_MEDIA_TYPE,
            TRACKING_INPUT_FORMAT_ID,
            "cas:authoritative-relocated-tracking-input",
        ),
    )
    model_request = TrackingModelAnalysisRequest(
        SchemaRef(TrackingModelAnalysisRequest.SCHEMA_ID),
        ref.identity(),
        _artifact("tracking-config", "org.leo-flow.tracking-model-config"),
        _artifact("tracking-algorithm", "org.leo-flow.model-algorithm"),
    )
    return model_request, DurableTrackingInputView(ref, snapshot)


def _descriptive_payload() -> JobPayload:
    feature = feature_set(201, (("rx_0", 10.0, 1.0),))
    model_dataset = dataset((feature[0],))
    hardware = hardware_snapshot(receivers=("rx_0",))
    config = ReceiverQualityAggregateConfig()
    model_request = request(model_dataset, config, (hardware[0],))
    # Constructing the model proves this remains a valid descriptive request,
    # without sharing any tracking execution surface.
    ReceiverQualityAggregateModel(model_dataset, config, execution_context()).fit(
        model_request,
        FakeFeatureSetReader((feature,)),
        FakeEphemerisReader(()),
        FakeHardwareReader((hardware,)),
    )
    durable = DatasetSnapshotRef(
        model_dataset.snapshot_id,
        model_dataset.membership_digest,
        Digest.sha256(b"descriptive-durable-dataset"),
    )
    return model_analysis_payload(model_request, durable)


def _lease(
    payload: JobPayload,
    *,
    job_type: JobType = JobType.MODEL_ANALYSIS,
    job_id: str = "job_tracking_execution",
) -> JobLease:
    return JobLease(
        JobId(job_id),
        job_type,
        payload,
        1,
        "lease-token",
        1,
        UtcNs(10_000),
    )


class _IdentityReader:
    def __init__(
        self,
        result: DurableTrackingInputView | Exception,
    ) -> None:
        self.result = result
        self.calls: list[TrackingInputSnapshotIdentity] = []

    def get_by_identity(
        self, identity: TrackingInputSnapshotIdentity
    ) -> DurableTrackingInputView:
        self.calls.append(identity)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_tracking_preparer_resolves_once_and_exposes_only_authoritative_ref() -> None:
    model_request, view = _tracking_fixture()
    reader = _IdentityReader(view)

    prepared = TrackingModelAnalysisJobPreparer(reader).prepare(
        _lease(tracking_model_analysis_payload(model_request))
    )

    assert prepared == PreparedTrackingModelAnalysis(
        model_request, view.ref, view.snapshot
    )
    assert reader.calls == [model_request.tracking_input_identity]
    assert prepared.tracking_input_ref.bundle_ref.locator == (
        "cas:authoritative-relocated-tracking-input"
    )
    assert not hasattr(prepared.request.tracking_input_identity, "locator")


@pytest.mark.parametrize(
    "error",
    [
        TrackingInputNotFoundError("missing exact tracking input"),
        TrackingInputIntegrityError("tracking input bundle is substituted"),
        TrackingInputIntegrityError("catalog projection differs"),
    ],
)
def test_tracking_preparer_propagates_closed_resolution_failures_once(
    error: Exception,
) -> None:
    model_request, _ = _tracking_fixture()
    reader = _IdentityReader(error)

    with pytest.raises(type(error), match=str(error)):
        TrackingModelAnalysisJobPreparer(reader).prepare(
            _lease(tracking_model_analysis_payload(model_request))
        )

    assert reader.calls == [model_request.tracking_input_identity]


def test_tracking_preparer_rejects_repository_postcondition_substitution() -> None:
    model_request, view = _tracking_fixture()
    substituted_identity = TrackingInputSnapshotIdentity(
        view.ref.snapshot_id,
        view.ref.snapshot_digest,
        Digest.sha256(b"substituted-membership"),
        view.ref.bundle_ref.digest,
        view.ref.bundle_ref.byte_count,
        view.ref.bundle_ref.media_type,
        view.ref.bundle_ref.format_id,
    )
    substituted_ref = TrackingInputSnapshotRef(
        substituted_identity.snapshot_id,
        substituted_identity.snapshot_digest,
        substituted_identity.membership_digest,
        view.ref.bundle_ref,
    )
    reader = _IdentityReader(DurableTrackingInputView(substituted_ref, view.snapshot))

    with pytest.raises(ModelAnalysisJobError, match="reference differs"):
        TrackingModelAnalysisJobPreparer(reader).prepare(
            _lease(tracking_model_analysis_payload(model_request))
        )
    assert reader.calls == [model_request.tracking_input_identity]


def test_tracking_preparer_rejects_substituted_content_behind_exact_ref() -> None:
    model_request, view = _tracking_fixture()
    other = _case(
        observations=(
            _observation(
                RecordingId("rec_tracking"),
                ReceiverChainId("rx_tracking"),
                frequency_hz=1_500_000_001.0,
            ),
        )
    ).freeze()
    reader = _IdentityReader(DurableTrackingInputView(view.ref, other))

    with pytest.raises(ModelAnalysisJobError, match="content differs"):
        TrackingModelAnalysisJobPreparer(reader).prepare(
            _lease(tracking_model_analysis_payload(model_request))
        )
    assert reader.calls == [model_request.tracking_input_identity]


def test_tracking_preparer_rejects_wrong_type_and_descriptive_schema_before_read() -> (
    None
):
    model_request, view = _tracking_fixture()
    reader = _IdentityReader(view)
    preparer = TrackingModelAnalysisJobPreparer(reader)
    payload = tracking_model_analysis_payload(model_request)

    with pytest.raises(ModelAnalysisJobError, match="jobs only"):
        preparer.prepare(_lease(payload, job_type=JobType.RECORDING_ANALYSIS))
    with pytest.raises(ModelAnalysisJobError, match="unsupported tracking"):
        preparer.prepare(_lease(_descriptive_payload()))
    assert reader.calls == []


@dataclass
class _DecodingExecutor:
    tracking: bool
    calls: list[JobId]

    def execute(self, lease: JobLease) -> str:
        if self.tracking:
            decode_tracking_model_analysis_payload(lease.payload)
        else:
            decode_model_analysis_payload(lease.payload)
        self.calls.append(lease.job_id)
        return "tracking" if self.tracking else "descriptive"


def _dispatcher() -> tuple[
    ModelAnalysisJobDispatcher, _DecodingExecutor, _DecodingExecutor
]:
    descriptive = _DecodingExecutor(False, [])
    tracking = _DecodingExecutor(True, [])
    return (
        ModelAnalysisJobDispatcher(descriptive, tracking),
        descriptive,
        tracking,
    )


def test_shared_queue_claim_routes_each_schema_to_only_its_decoder() -> None:
    jobs = InMemoryJobLeaseRepository(
        now_utc_ns=lambda: 1_000,
        token_factory=iter(("lease-1", "lease-2")).__next__,
    )
    tracking_request, _ = _tracking_fixture()
    jobs.enqueue(
        JobId("job_01_descriptive"),
        JobType.MODEL_ANALYSIS,
        _descriptive_payload(),
    )
    jobs.enqueue(
        JobId("job_02_tracking"),
        JobType.MODEL_ANALYSIS,
        tracking_model_analysis_payload(tracking_request),
    )
    dispatcher, descriptive, tracking = _dispatcher()
    cycle = TypedAnalysisRouterCycle(
        jobs,
        executors={JobType.MODEL_ANALYSIS: dispatcher},
        worker_id="model-schema-router",
        lease_ttl_s=10,
    )

    assert cycle.process_one_job()
    assert cycle.process_one_job()
    assert not cycle.process_one_job()
    assert descriptive.calls == [JobId("job_01_descriptive")]
    assert tracking.calls == [JobId("job_02_tracking")]


@pytest.mark.parametrize(
    "crossing", ["tracking-as-descriptive", "descriptive-as-tracking"]
)
def test_cross_schema_payload_shapes_never_reach_the_other_executor(
    crossing: str,
) -> None:
    tracking_request, _ = _tracking_fixture()
    tracking_payload = tracking_model_analysis_payload(tracking_request)
    descriptive_payload = _descriptive_payload()
    dispatcher, descriptive, tracking = _dispatcher()
    if crossing == "tracking-as-descriptive":
        crossed = JobPayload.create(
            MODEL_ANALYSIS_JOB_SCHEMA, thaw_value(tracking_payload.value)
        )
    else:
        crossed = JobPayload.create(
            TRACKING_MODEL_ANALYSIS_JOB_SCHEMA,
            thaw_value(descriptive_payload.value),
        )

    with pytest.raises(ModelAnalysisJobError):
        dispatcher.execute(_lease(crossed))
    assert descriptive.calls == []
    assert tracking.calls == []


def test_dispatcher_rejects_unknown_schema_and_wrong_type_before_execution() -> None:
    tracking_request, _ = _tracking_fixture()
    payload = tracking_model_analysis_payload(tracking_request)
    dispatcher, descriptive, tracking = _dispatcher()
    unknown = JobPayload.create(
        SchemaRef("org.leo-flow.unknown-model-analysis-job"),
        thaw_value(payload.value),
    )

    with pytest.raises(ModelAnalysisJobError, match="unsupported"):
        dispatcher.execute(_lease(unknown))
    with pytest.raises(ModelAnalysisJobError, match="jobs only"):
        dispatcher.execute(_lease(payload, job_type=JobType.RECORDING_ANALYSIS))
    assert descriptive.calls == []
    assert tracking.calls == []
