from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from leo_flow.analysis.ephemeris.catalog import (
    ArchivedEphemerisSnapshot,
    InMemoryEphemerisSnapshotCatalog,
)
from leo_flow.contracts.core import (
    V0_1,
    ArtifactRef,
    Digest,
    EphemerisRetrievalId,
    EphemerisSnapshotId,
    JobId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.ephemeris import (
    EphemerisSnapshot,
    EphemerisSnapshotRef,
    EphemerisSource,
    ValidationResult,
)
from leo_flow.contracts.features import RecordingAnalysisRequest
from leo_flow.contracts.model import FeatureDatasetSnapshot, ModelAnalysisRequest
from leo_flow.contracts.ports import (
    EphemerisReader,
    FeatureSetReader,
    HardwareMetadataReader,
    ModelFitter,
    RecordingAnalyzer,
)
from leo_flow.deployments.offline_analysis_v1 import (
    FEATURE_PUBLISHER_REF,
    JOB_REPOSITORY_REF,
    MODEL_PUBLISHER_REF,
    RECORDING_READER_REF,
    AlgorithmKey,
    ExactModelFitterRegistry,
    ExactRecordingAnalyzerRegistry,
    FencedModelAnalysisExecutor,
    OfflineAnalysisCompositionError,
    OfflineAnalysisCycle,
    StationScientificFactories,
    build_station_plugin,
)
from leo_flow.jobs import InMemoryJobLeaseRepository, JobPayload, JobType
from leo_flow.jobs.contracts import JobLease
from leo_flow.jobs.memory import JobState
from leo_flow.services import (
    Capability,
    Process,
    RuntimeConfig,
    SecretRef,
    assemble_service,
)
from leo_flow.services.config import AnalysisServiceConfig, load_service_config
from leo_flow.storage.filesystem import FileSystemBlobStore
from leo_flow.storage.ports import RecordingView
from testkit import digest


def _artifact(name: str) -> ArtifactRef:
    return ArtifactRef(name, digest(name), SchemaRef(f"org.example.{name}"))


def test_example_configuration_is_strictly_parseable() -> None:
    path = (
        Path(__file__).parents[2]
        / "deploy"
        / "offline-analysis-v1"
        / "analysis.json"
    )
    config = load_service_config(path)
    assert isinstance(config, AnalysisServiceConfig)
    assert config.runtime.instance_id == "station-a-offline-analysis-1"


@dataclass
class _Executor:
    jobs: InMemoryJobLeaseRepository
    expected: JobType
    calls: list[JobLease] = field(default_factory=list)

    def execute(self, lease: JobLease) -> ArtifactRef:
        assert lease.job_type is self.expected
        self.calls.append(lease)
        result = _artifact(f"result-{self.expected.value}")
        self.jobs.complete(
            lease.job_id,
            lease.lease_token,
            lease.lease_generation,
            result,
        )
        return result


def _enqueue(jobs: InMemoryJobLeaseRepository, job_id: str, job_type: JobType) -> None:
    jobs.enqueue(
        JobId(job_id),
        job_type,
        JobPayload.create(SchemaRef(f"org.example.{job_type.value}"), {}),
        available_at_utc_ns=UtcNs(0),
    )


def test_cycle_routes_two_lanes_and_restart_is_idempotent() -> None:
    jobs = InMemoryJobLeaseRepository(
        now_utc_ns=lambda: 100, token_factory=lambda: "lease-offline"
    )
    _enqueue(jobs, "job_01_recording", JobType.RECORDING_ANALYSIS)
    _enqueue(jobs, "job_02_model", JobType.MODEL_ANALYSIS)
    recording = _Executor(jobs, JobType.RECORDING_ANALYSIS)
    model = _Executor(jobs, JobType.MODEL_ANALYSIS)
    cycle = OfflineAnalysisCycle(
        jobs,
        recording=recording,
        model=model,
        worker_id="offline-a",
        lease_ttl_s=10,
    )

    assert cycle.process_one_job()
    assert cycle.process_one_job()
    assert not cycle.process_one_job()
    assert jobs.snapshot(JobId("job_01_recording")).state is JobState.SUCCEEDED
    assert jobs.snapshot(JobId("job_02_model")).state is JobState.SUCCEEDED

    # Re-enqueue is the durable repository's idempotent restart path. A newly
    # assembled cycle cannot claim or duplicate either completed output.
    _enqueue(jobs, "job_01_recording", JobType.RECORDING_ANALYSIS)
    restarted = OfflineAnalysisCycle(
        jobs,
        recording=recording,
        model=model,
        worker_id="offline-b",
        lease_ttl_s=10,
    )
    assert not restarted.process_one_job()
    assert len(recording.calls) == len(model.calls) == 1


def test_cycle_does_not_claim_ephemeris_or_backfill_work() -> None:
    jobs = InMemoryJobLeaseRepository(
        now_utc_ns=lambda: 100, token_factory=lambda: "lease-offline"
    )
    _enqueue(jobs, "job_ephemeris", JobType.EPHEMERIS_RETRIEVAL)
    _enqueue(jobs, "job_backfill", JobType.EPHEMERIS_LINK_BACKFILL)
    recording = _Executor(jobs, JobType.RECORDING_ANALYSIS)
    model = _Executor(jobs, JobType.MODEL_ANALYSIS)
    cycle = OfflineAnalysisCycle(
        jobs,
        recording=recording,
        model=model,
        worker_id="offline-a",
        lease_ttl_s=10,
    )

    assert not cycle.process_one_job()
    assert not recording.calls
    assert not model.calls


class _BrokenModelProcessor:
    def execute(self, lease: JobLease) -> object:
        del lease
        raise LookupError("scientific plugin unavailable")


def test_model_preparation_failure_is_fenced_and_bounded() -> None:
    jobs = InMemoryJobLeaseRepository(
        now_utc_ns=lambda: 100, token_factory=lambda: "lease-offline"
    )
    _enqueue(jobs, "job_model_failure", JobType.MODEL_ANALYSIS)
    cycle = OfflineAnalysisCycle(
        jobs,
        recording=_Executor(jobs, JobType.RECORDING_ANALYSIS),
        model=FencedModelAnalysisExecutor(jobs, _BrokenModelProcessor()),
        worker_id="offline-a",
        lease_ttl_s=10,
    )

    with pytest.raises(LookupError, match="plugin unavailable"):
        cycle.process_one_job()
    snapshot = jobs.snapshot(JobId("job_model_failure"))
    assert snapshot.state is JobState.FAILED
    assert snapshot.last_error == "LookupError: model analysis failed"


class _Analyzer:
    def __init__(self, result: object) -> None:
        self.result = result

    def analyze(self, recording: object, request: object) -> object:
        del recording, request
        return self.result


def test_recording_registry_requires_complete_exact_refs() -> None:
    algorithm = _artifact("recording-algorithm-v1")
    config = _artifact("recording-config-v1")
    expected = object()
    registry = ExactRecordingAnalyzerRegistry(
        {AlgorithmKey(algorithm, config): cast(RecordingAnalyzer, _Analyzer(expected))}
    )
    request = cast(
        RecordingAnalysisRequest,
        type("Request", (), {"algorithm_ref": algorithm, "config_ref": config})(),
    )
    assert registry.analyze(cast(RecordingView, object()), request) is expected

    substituted = cast(
        RecordingAnalysisRequest,
        type(
            "Request",
            (),
            {"algorithm_ref": algorithm, "config_ref": _artifact("other-config")},
        )(),
    )
    with pytest.raises(OfflineAnalysisCompositionError, match="not registered"):
        registry.analyze(cast(RecordingView, object()), substituted)


class _Fitter:
    def __init__(self, result: object) -> None:
        self.result = result

    def fit(self, *args: object) -> object:
        del args
        return self.result


def test_model_registry_selects_only_after_reading_request_refs() -> None:
    algorithm = _artifact("model-algorithm-v1")
    config = _artifact("model-config-v1")
    expected = object()
    datasets: list[object] = []

    def build(dataset: FeatureDatasetSnapshot) -> ModelFitter:
        datasets.append(dataset)
        return cast(ModelFitter, _Fitter(expected))

    registry = ExactModelFitterRegistry({AlgorithmKey(algorithm, config): build})
    dataset = cast(FeatureDatasetSnapshot, object())
    selected = registry(dataset)
    request = cast(
        ModelAnalysisRequest,
        type(
            "Request",
            (),
            {"algorithm_ref": algorithm, "model_config_ref": config},
        )(),
    )
    assert (
        selected.fit(
            request,
            cast(FeatureSetReader, object()),
            cast(EphemerisReader, object()),
            cast(HardwareMetadataReader, object()),
        )
        is expected
    )
    assert datasets == [dataset]

    unknown = cast(
        ModelAnalysisRequest,
        type(
            "Request",
            (),
            {"algorithm_ref": _artifact("unknown"), "model_config_ref": config},
        )(),
    )
    with pytest.raises(OfflineAnalysisCompositionError, match="not registered"):
        selected.fit(
            unknown,
            cast(FeatureSetReader, object()),
            cast(EphemerisReader, object()),
            cast(HardwareMetadataReader, object()),
        )


def test_deployment_module_has_no_capture_radio_or_network_tle_imports() -> None:
    from leo_flow.deployments import offline_analysis_v1

    source = __import__("inspect").getsource(offline_analysis_v1)
    forbidden = (
        "leo_flow.capture",
        "RadioDevice",
        "ephemeris_http",
        "urllib",
        "requests",
        "skyfield",
        "sgp4",
    )
    assert not [name for name in forbidden if name in source]


class _Diagnostics:
    def emit(self, event: object) -> None:
        del event


def _station_config() -> AnalysisServiceConfig:
    return AnalysisServiceConfig(
        1,
        "analysis",
        RuntimeConfig(
            "offline-analysis-test",
            0.01,
            0.1,
            (SecretRef("systemd-credential", "catalog-dsn"),),
        ),
        JOB_REPOSITORY_REF,
        RECORDING_READER_REF,
        FEATURE_PUBLISHER_REF,
        MODEL_PUBLISHER_REF,
    )


def _scientific_factories() -> StationScientificFactories:
    recording_algorithm = _artifact("recording-science-v1")
    recording_config = _artifact("recording-science-config-v1")
    model_algorithm = _artifact("model-science-v1")
    model_config = _artifact("model-science-config-v1")
    return StationScientificFactories(
        recording_analyzers={
            AlgorithmKey(recording_algorithm, recording_config): cast(
                RecordingAnalyzer, _Analyzer(object())
            )
        },
        model_fitters={
            AlgorithmKey(model_algorithm, model_config): lambda dataset: cast(
                ModelFitter, _Fitter(dataset)
            )
        },
    )


def test_station_plugin_is_exact_analysis_only_and_assembly_has_no_io(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credential_dir = tmp_path / "credentials"
    credential_dir.mkdir()
    (credential_dir / "catalog-dsn").write_text(
        "postgresql://must-not-connect.invalid/catalog", encoding="utf-8"
    )
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credential_dir))
    cas_root = tmp_path / "cas"
    plugin = build_station_plugin(_scientific_factories(), cas_root=cas_root)

    assert set(plugin.builders) == {Process.ANALYSIS}
    assert set(plugin.secret_providers) == {"systemd-credential"}
    for capability, reference in (
        (Capability.JOB_REPOSITORY, JOB_REPOSITORY_REF),
        (Capability.RECORDING_READER, RECORDING_READER_REF),
        (Capability.FEATURE_PUBLISHER, FEATURE_PUBLISHER_REF),
        (Capability.MODEL_PUBLISHER, MODEL_PUBLISHER_REF),
    ):
        assert callable(
            plugin.manifest.factory(Process.ANALYSIS, capability, reference)
        )

    service = assemble_service(_station_config(), plugin, diagnostics=_Diagnostics())
    assert service.health().state.value == "stopped"
    assert not cas_root.exists()


def test_station_plugin_requires_both_exact_registries_and_absolute_cas(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="both exact"):
        StationScientificFactories({}, {})
    with pytest.raises(ValueError, match="absolute CAS"):
        build_station_plugin(_scientific_factories(), cas_root=Path("relative"))


def test_checked_unit_template_cannot_run_without_operator_station_plugin() -> None:
    from leo_flow.deployments import offline_analysis_v1

    assert not hasattr(offline_analysis_v1, "PLUGIN")
    root = Path(__file__).parents[2] / "deploy" / "offline-analysis-v1"
    config = load_service_config(root / "analysis.json")
    assert isinstance(config, AnalysisServiceConfig)
    unit = (root / "leo-offline-analysis.service.example").read_text(encoding="utf-8")
    assert "leo_station.analysis_v1:PLUGIN" in unit
    assert "LoadCredential=catalog-dsn:" in unit
    assert not any(line.strip() == "[Install]" for line in unit.splitlines())


def test_archived_ephemeris_reader_requires_exact_catalog_and_cas_identity(
    tmp_path: Path,
) -> None:
    from leo_flow.deployments import offline_analysis_v1

    payload = b'{"schema":"normalized-rehearsal-fixture"}'
    blobs = FileSystemBlobStore(tmp_path / "cas")
    normalized = blobs.put(
        io.BytesIO(payload),
        expected_digest=Digest.sha256(payload),
        expected_bytes=len(payload),
        media_type="application/json",
        format_id="tle-normalized-v1",
        idempotency_key="normalized-fixture",
    )
    raw = type(normalized)(digest("raw"), 3, "text/plain", "tle-raw-v1", "fixture:raw")
    provenance = type(normalized)(
        digest("provenance"),
        10,
        "application/json",
        "ephemeris-provenance-v1",
        "fixture:provenance",
    )
    snapshot = EphemerisSnapshot(
        SchemaRef(EphemerisSnapshot.SCHEMA_ID, V0_1),
        EphemerisSnapshotId("eph_offline_reader"),
        EphemerisRetrievalId("ephret_offline_reader"),
        EphemerisSource.HUGGING_FACE,
        "starlink",
        UtcNs(10),
        raw,
        normalized,
        _artifact("parser-v1"),
        1,
        digest("norad-set"),
        UtcNs(1),
        UtcNs(2),
        ValidationResult(True, _artifact("policy-v1")),
        "fixture attribution",
    )
    archived = ArchivedEphemerisSnapshot(snapshot, provenance, digest("request").value)
    catalog = InMemoryEphemerisSnapshotCatalog()
    catalog.publish(archived)
    reader = offline_analysis_v1._ExactArchivedEphemerisReader(catalog, blobs)

    expected = archived.snapshot_ref()
    with reader.open(expected) as view:
        assert view.ref == expected
        assert view.normalized_bytes() == payload
    substituted = EphemerisSnapshotRef(
        expected.snapshot_id,
        expected.source,
        expected.raw_digest,
        digest("substituted-normalized"),
    )
    with (
        pytest.raises(OfflineAnalysisCompositionError, match="exactly matches"),
        reader.open(substituted),
    ):
        pass
