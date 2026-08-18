"""Durable exact refinement of complete-IQ OFDM/power-selected windows."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from leo_flow.analysis.recording.starlink_pilot_refinement import (
    ExactStarlinkPilotRefinementAnalyzerV0_1,
)
from leo_flow.analysis.recording.starlink_pilot_refinement_persistence import (
    DurableStarlinkPilotRefinementStoreV0_1,
)
from leo_flow.analysis.recording.starlink_suite_persistence import (
    DurableStarlinkSuiteStoreV0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    starlink_search_grid_v0_1,
)
from leo_flow.contracts.core import V0_1, ArtifactRef, Digest, SchemaRef
from leo_flow.contracts.starlink_pilot_prescreen import (
    StarlinkPilotPrescreenBundleV0_1,
    StarlinkPilotPrescreenProductRefV0_1,
)
from leo_flow.contracts.starlink_pilot_refinement import (
    StarlinkPilotRefinementBundleV0_1,
    StarlinkPilotRefinementRequestV0_1,
    StarlinkPilotRefinementSeedV0_1,
    StarlinkPilotRefinementStreamSelectionV0_1,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
)
from leo_flow.contracts.storage import RecordingObjectRef
from leo_flow.services.capture_batch_analysis import PublishedRecordingCatalog
from leo_flow.services.starlink_full_dwell_pipeline import FullDwellAnalysisProfileV0_1
from leo_flow.storage.ports import RecordingObjectReader


class StalePilotRefinementLeaseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PilotRefinementWorkLeaseV0_1:
    work_id: str
    recording_ref: RecordingObjectRef
    source_prescreen_ref: StarlinkPilotPrescreenProductRefV0_1
    source_suite_ref: StarlinkDetectorSuiteProductRefV0_2
    source_suite_request_digest: Digest
    lease_token: str
    lease_generation: int
    attempt: int


class PilotRefinementWorkRepositoryV0_1(Protocol):
    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> PilotRefinementWorkLeaseV0_1 | None: ...
    def complete(
        self, lease: PilotRefinementWorkLeaseV0_1, result: ArtifactRef
    ) -> None: ...
    def retry(self, lease: PilotRefinementWorkLeaseV0_1, reason: str) -> None: ...


class PilotRefinementLeaseProducerV0_1(Protocol):
    def produce(self, lease: PilotRefinementWorkLeaseV0_1) -> ArtifactRef: ...


class BoundedPilotRefinementServiceV0_1:
    def __init__(
        self,
        work: PilotRefinementWorkRepositoryV0_1,
        producer: PilotRefinementLeaseProducerV0_1,
        *,
        worker_id: str,
        lease_ttl_s: float = 7200,
        maximum_attempts: int = 3,
    ) -> None:
        if not worker_id or lease_ttl_s <= 0 or maximum_attempts <= 0:
            raise ValueError("pilot-refinement worker bounds are invalid")
        self._work, self._producer = work, producer
        self._worker_id, self._lease_ttl_s = worker_id, lease_ttl_s
        self._maximum_attempts = maximum_attempts

    def run_once(self) -> bool:
        lease = self._work.claim(self._worker_id, self._lease_ttl_s)
        if lease is None:
            return False
        try:
            result = self._producer.produce(lease)
            self._work.complete(lease, result)
        except StalePilotRefinementLeaseError:
            pass
        except Exception:  # noqa: BLE001 - optional bounded worker boundary
            reason = (
                "pilot-refinement-attempts-exhausted"
                if lease.attempt >= self._maximum_attempts
                else "pilot-refinement-transient-failure"
            )
            try:
                self._work.retry(lease, reason)
            except StalePilotRefinementLeaseError:
                pass
        return True


class DurablePilotRefinementLeaseProducerV0_1:
    def __init__(
        self,
        recordings: PublishedRecordingCatalog,
        reader: RecordingObjectReader,
        prescreens: PilotPrescreenReaderV0_1,
        suites: DurableStarlinkSuiteStoreV0_2,
        products: DurableStarlinkPilotRefinementStoreV0_1,
        profiles: tuple[FullDwellAnalysisProfileV0_1, ...],
    ) -> None:
        refs = tuple(profile.source_config_ref for profile in profiles)
        if not profiles or len(refs) != len(set(refs)):
            raise ValueError("pilot-refinement profiles are invalid")
        self._recordings, self._reader = recordings, reader
        self._prescreens, self._suites, self._products = prescreens, suites, products
        self._profiles = profiles

    def produce(self, lease: PilotRefinementWorkLeaseV0_1) -> ArtifactRef:
        published = self._recordings.get(lease.recording_ref.recording_id)
        if published is None or published.recording_object != lease.recording_ref:
            raise ValueError("pilot-refinement recording is not exact and published")
        with (
            self._prescreens.open(lease.source_prescreen_ref) as prescreen,
            self._suites.open(lease.source_suite_ref) as suite,
        ):
            profile = self._profile(suite)
            request = pilot_refinement_request_v0_1(lease, prescreen, suite, profile)
            with self._reader.open(lease.recording_ref) as recording:
                bundle = ExactStarlinkPilotRefinementAnalyzerV0_1(
                    profile.config, profile.execution
                ).analyze(recording, request)
        result = self._products.publish(
            request,
            bundle,
            idempotency_key=(
                f"pilot-refinement:{lease.source_prescreen_ref.analysis_id}:"
                f"{request.digest.value}"
            ),
        )
        return ArtifactRef(result.analysis_id, result.bundle_ref.digest, bundle.schema)

    def _profile(
        self, suite: StarlinkDetectorSuiteRecordingBundleV0_2
    ) -> FullDwellAnalysisProfileV0_1:
        refs = {
            method.config_ref for stream in suite.suites for method in stream.methods
        }
        matches = tuple(
            profile for profile in self._profiles if refs == {profile.source_config_ref}
        )
        if len(matches) != 1:
            raise ValueError("pilot-refinement source has no unique profile")
        return matches[0]


class PilotPrescreenReaderV0_1(Protocol):
    def open(
        self, ref: StarlinkPilotPrescreenProductRefV0_1
    ) -> AbstractContextManager[StarlinkPilotPrescreenBundleV0_1]: ...


def pilot_refinement_request_v0_1(
    lease: PilotRefinementWorkLeaseV0_1,
    prescreen: StarlinkPilotPrescreenBundleV0_1,
    suite: StarlinkDetectorSuiteRecordingBundleV0_2,
    profile: FullDwellAnalysisProfileV0_1,
) -> StarlinkPilotRefinementRequestV0_1:
    if (
        prescreen.recording_id != suite.recording_id
        or prescreen.recording_id != lease.recording_ref.recording_id
        or prescreen.recording_identity_digest != suite.recording_identity_digest
        or prescreen.recording_identity_digest != lease.recording_ref.identity_digest()
    ):
        raise ValueError("pilot-refinement source identities differ")
    suite_keys = {
        (stream.segment_id, stream.receiver_chain_id, stream.edge)
        for stream in suite.suites
    }
    streams = []
    for stream in prescreen.streams:
        selection = stream.selection
        if (
            selection.segment_id,
            selection.receiver_chain_id,
            selection.edge,
        ) not in suite_keys:
            continue
        selected = tuple(
            window for window in stream.windows if window.selected_for_exact_refinement
        )
        if not selected:
            continue
        streams.append(
            StarlinkPilotRefinementStreamSelectionV0_1(
                selection.radio_id,
                selection.lnb_id,
                selection.segment_id,
                selection.receiver_chain_id,
                selection.channel_number,
                selection.edge,
                selection.sample_rate_hz,
                selection.segment_sample_count,
                tuple(
                    StarlinkPilotRefinementSeedV0_1(
                        index,
                        window.start_sample,
                        window.stop_sample,
                        window.ofdm_periodicity_score,
                        window.mean_power_counts_squared,
                        window.periodicity_rank,
                        window.power_rank,
                    )
                    for index, window in enumerate(selected)
                ),
            )
        )
    streams.sort(key=lambda stream: stream.identity)
    return StarlinkPilotRefinementRequestV0_1(
        SchemaRef(StarlinkPilotRefinementRequestV0_1.SCHEMA_ID, V0_1),
        lease.recording_ref.recording_id,
        lease.recording_ref,
        ArtifactRef(
            prescreen.analysis_id,
            lease.source_prescreen_ref.bundle_ref.digest,
            prescreen.schema,
        ),
        ArtifactRef(
            suite.analysis_id, lease.source_suite_ref.bundle_ref.digest, suite.schema
        ),
        lease.source_suite_request_digest,
        starlink_search_grid_v0_1(profile.config),
        tuple(streams),
        4,
        SchemaRef(StarlinkPilotRefinementBundleV0_1.SCHEMA_ID, V0_1),
    )
