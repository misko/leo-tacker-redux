"""Bounded optional worker and exact producer for adaptive pilot responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leo_flow.analysis.recording.starlink_adaptive_response import (
    ExactStarlinkAdaptiveResponseAnalyzerV0_1,
)
from leo_flow.analysis.recording.starlink_adaptive_response_persistence import (
    DurableStarlinkAdaptiveResponseStoreV0_1,
)
from leo_flow.analysis.recording.starlink_suite_persistence import (
    DurableStarlinkSuiteStoreV0_2,
)
from leo_flow.analysis.recording.starlink_surrogate_null import (
    starlink_search_grid_v0_1,
)
from leo_flow.contracts.capture import RecordingManifest
from leo_flow.contracts.core import ArtifactRef, Digest, SchemaRef
from leo_flow.contracts.starlink_adaptive_refinement import (
    StarlinkAdaptiveRefinementPlanV0_1,
)
from leo_flow.contracts.starlink_adaptive_response import (
    V0_1,
    StarlinkAdaptivePowerSeedV0_1,
    StarlinkAdaptiveResponseBundleV0_1,
    StarlinkAdaptiveResponseProductRefV0_1,
    StarlinkAdaptiveResponseRequestV0_1,
    StarlinkAdaptiveStreamSelectionV0_1,
)
from leo_flow.contracts.starlink_full_dwell_timeline_product import (
    FullDwellRefinementRequestV0_1,
    FullDwellRefinementWindowV0_1,
)
from leo_flow.contracts.starlink_suite_pipeline import (
    StarlinkDetectorSuiteProductRefV0_2,
    StarlinkDetectorSuiteRecordingBundleV0_2,
)
from leo_flow.contracts.storage import RecordingObjectRef
from leo_flow.services.capture_batch_analysis import PublishedRecordingCatalog
from leo_flow.services.starlink_full_dwell_pipeline import (
    FullDwellAnalysisProfileV0_1,
)
from leo_flow.storage.ports import RecordingObjectReader


class StaleAdaptiveResponseLeaseError(RuntimeError):
    pass


# One exact sentinel per second is the production compromise between the legacy
# 100 ms oracle cadence and the cost of running all eight methods for Qin plus
# every precommitted surrogate.  The complete-IQ prescreen remains the 100%
# coverage layer; these sentinels guarantee that exact evidence is distributed
# across the dwell instead of clustering only at global score maxima.
ADAPTIVE_SENTINEL_STRIDE_SECONDS = 1.0
# Once a pattern wins a one-second sentinel, replay the same Qin/control union
# at the legacy 100 ms cadence across +/-500 ms.  With Qin plus four controls,
# 61 sentinels and eight power seeds this remains below the 128-window bound.
ADAPTIVE_LOCAL_RADIUS_SECONDS = 0.5
ADAPTIVE_LOCAL_STRIDE_SECONDS = 0.1
ADAPTIVE_CENTERS_PER_PATTERN = 1
ADAPTIVE_MAXIMUM_POWER_SEEDS = 8
ADAPTIVE_MAXIMUM_BASE_WINDOWS = 128
ADAPTIVE_MAXIMUM_EXACT_WINDOWS = 128


@dataclass(frozen=True)
class AdaptiveResponseWorkLeaseV0_1:
    refinement_request: FullDwellRefinementRequestV0_1
    source_suite_ref: StarlinkDetectorSuiteProductRefV0_2
    source_suite_request_digest: Digest
    lease_token: str
    lease_generation: int
    attempt: int


class AdaptiveResponseWorkRepositoryV0_1(Protocol):
    def claim(
        self, worker_id: str, lease_ttl_s: float
    ) -> AdaptiveResponseWorkLeaseV0_1 | None: ...

    def complete(
        self, lease: AdaptiveResponseWorkLeaseV0_1, result: ArtifactRef
    ) -> None: ...

    def retry(self, lease: AdaptiveResponseWorkLeaseV0_1, reason: str) -> None: ...


class AdaptiveResponseLeaseProducerV0_1(Protocol):
    def produce(self, lease: AdaptiveResponseWorkLeaseV0_1) -> ArtifactRef: ...


class AdaptiveQamPublisherPortV0_4(Protocol):
    def publish(
        self,
        recording_ref: RecordingObjectRef,
        source_suite_ref: StarlinkDetectorSuiteProductRefV0_2,
        source_suite_request_digest: Digest,
        source_suite: StarlinkDetectorSuiteRecordingBundleV0_2,
        source_response_ref: StarlinkAdaptiveResponseProductRefV0_1,
        source_response: StarlinkAdaptiveResponseBundleV0_1,
    ) -> object: ...


class BoundedAdaptiveResponseServiceV0_1:
    def __init__(
        self,
        work: AdaptiveResponseWorkRepositoryV0_1,
        producer: AdaptiveResponseLeaseProducerV0_1,
        *,
        worker_id: str,
        lease_ttl_s: float = 7200.0,
        maximum_attempts: int = 3,
    ) -> None:
        if not worker_id or lease_ttl_s <= 0 or maximum_attempts <= 0:
            raise ValueError("adaptive response worker bounds are invalid")
        self._work, self._producer = work, producer
        self._worker_id = worker_id
        self._lease_ttl_s = lease_ttl_s
        self._maximum_attempts = maximum_attempts

    def run_once(self) -> bool:
        lease = self._work.claim(self._worker_id, self._lease_ttl_s)
        if lease is None:
            return False
        try:
            result = self._producer.produce(lease)
            self._work.complete(lease, result)
        except StaleAdaptiveResponseLeaseError:
            pass
        except ValueError:
            self._safe_retry(lease, "adaptive-response-invalid-input")
        except Exception:  # noqa: BLE001 - isolated optional-work retry boundary
            reason = (
                "adaptive-response-attempts-exhausted"
                if lease.attempt >= self._maximum_attempts
                else "adaptive-response-transient-failure"
            )
            self._safe_retry(lease, reason)
        return True

    def _safe_retry(self, lease: AdaptiveResponseWorkLeaseV0_1, reason: str) -> None:
        try:
            self._work.retry(lease, reason)
        except StaleAdaptiveResponseLeaseError:
            pass


class DurableAdaptiveResponseLeaseProducerV0_1:
    def __init__(
        self,
        recordings: PublishedRecordingCatalog,
        reader: RecordingObjectReader,
        suites: DurableStarlinkSuiteStoreV0_2,
        products: DurableStarlinkAdaptiveResponseStoreV0_1,
        profiles: tuple[FullDwellAnalysisProfileV0_1, ...],
        adaptive_qam: AdaptiveQamPublisherPortV0_4 | None = None,
    ) -> None:
        refs = tuple(item.source_config_ref for item in profiles)
        if not profiles or len(refs) != len(set(refs)):
            raise ValueError("adaptive response profiles are invalid")
        self._recordings, self._reader = recordings, reader
        self._suites, self._products, self._profiles = suites, products, profiles
        self._adaptive_qam = adaptive_qam

    def produce(self, lease: AdaptiveResponseWorkLeaseV0_1) -> ArtifactRef:
        refinement = lease.refinement_request
        published = self._recordings.get(refinement.recording_id)
        if (
            published is None
            or published.recording_object != refinement.recording_object_ref
        ):
            raise ValueError("adaptive response recording is not exact and published")
        with (
            self._suites.open(lease.source_suite_ref) as suite,
            self._reader.open(refinement.recording_object_ref) as recording,
        ):
            profile = self._profile(suite)
            request = adaptive_response_request_v0_1(
                refinement,
                suite,
                lease.source_suite_ref,
                lease.source_suite_request_digest,
                recording.manifest,
                profile,
            )
            bundle = ExactStarlinkAdaptiveResponseAnalyzerV0_1(
                profile.config, profile.execution
            ).analyze(recording, request)
        result = self._products.publish(
            request,
            bundle,
            idempotency_key=f"adaptive:{refinement.timeline_ref.artifact_id}:{request.digest.value}",
        )
        if self._adaptive_qam is not None:
            self._adaptive_qam.publish(
                refinement.recording_object_ref,
                lease.source_suite_ref,
                lease.source_suite_request_digest,
                suite,
                result,
                bundle,
            )
        return result.artifact_ref

    def _profile(
        self, suite: StarlinkDetectorSuiteRecordingBundleV0_2
    ) -> FullDwellAnalysisProfileV0_1:
        source_refs = {
            method.config_ref for item in suite.suites for method in item.methods
        }
        matches = tuple(
            item for item in self._profiles if source_refs == {item.source_config_ref}
        )
        if len(matches) != 1:
            raise ValueError("adaptive response source has no unique profile")
        return matches[0]


def adaptive_response_request_v0_1(
    refinement: FullDwellRefinementRequestV0_1,
    suite: StarlinkDetectorSuiteRecordingBundleV0_2,
    source_suite_ref: StarlinkDetectorSuiteProductRefV0_2,
    source_suite_request_digest: Digest,
    manifest: RecordingManifest,
    profile: FullDwellAnalysisProfileV0_1,
) -> StarlinkAdaptiveResponseRequestV0_1:
    if (
        suite.recording_id != refinement.recording_id
        or suite.analysis_id != source_suite_ref.analysis_id
        or suite.recording_identity_digest
        != refinement.recording_object_ref.identity_digest()
    ):
        raise ValueError("adaptive response source identities differ")
    segments = {item.segment_id: item for item in manifest.segments}
    suite_keys = {
        (item.segment_id, item.receiver_chain_id, item.edge) for item in suite.suites
    }
    grouped: dict[tuple[str, ...], list[FullDwellRefinementWindowV0_1]] = {}
    for window in refinement.windows:
        key = tuple(
            map(
                str,
                (
                    window.radio_id,
                    window.lnb_id,
                    window.segment_id,
                    window.receiver_chain_id,
                    window.channel_number,
                    window.edge,
                ),
            )
        )
        grouped.setdefault(key, []).append(window)
    streams = []
    for key in sorted(grouped):
        # The prompt timeline ranks these windows using a pattern-blind power
        # statistic over 100% of the IQ.  Exact Qin/surrogate searches are the
        # expensive second stage, so retain a fixed top-ranked subset rather
        # than allowing the queue producer's larger transport bound to dictate
        # scientific runtime.  Fixed sentinels below still span the full dwell.
        windows = _bounded_power_seeds(grouped[key])
        first = windows[0]
        segment = segments.get(first.segment_id)
        if (
            segment is None
            or (first.segment_id, first.receiver_chain_id, first.edge) not in suite_keys
            or manifest.radio_id != first.radio_id
        ):
            raise ValueError("adaptive response refinement stream is not authoritative")
        streams.append(
            StarlinkAdaptiveStreamSelectionV0_1(
                first.radio_id,
                first.lnb_id,
                first.segment_id,
                first.receiver_chain_id,
                first.channel_number,
                first.edge,
                segment.actual_sample_rate_hz,
                segment.sample_count,
                tuple(
                    StarlinkAdaptivePowerSeedV0_1(
                        item.rank, item.start_sample, item.stop_sample
                    )
                    for item in windows
                ),
            )
        )
    sample_rates = {item.sample_rate_hz for item in streams}
    if len(sample_rates) != 1:
        raise ValueError("adaptive response streams require one sample-rate profile")
    sample_rate = next(iter(sample_rates))
    plan = adaptive_response_plan_v0_1(sample_rate)
    return StarlinkAdaptiveResponseRequestV0_1(
        SchemaRef(StarlinkAdaptiveResponseRequestV0_1.SCHEMA_ID, V0_1),
        refinement.recording_id,
        refinement.recording_object_ref,
        refinement.timeline_ref,
        refinement.timeline_request_digest,
        ArtifactRef(
            source_suite_ref.analysis_id,
            source_suite_ref.bundle_ref.digest,
            suite.schema,
        ),
        source_suite_request_digest,
        starlink_search_grid_v0_1(profile.config),
        plan,
        tuple(streams),
        4,
        SchemaRef(StarlinkAdaptiveResponseBundleV0_1.SCHEMA_ID, V0_1),
    )


def adaptive_response_plan_v0_1(
    sample_rate_hz: float,
) -> StarlinkAdaptiveRefinementPlanV0_1:
    """Return the bounded, full-dwell-spanning exact-refinement policy."""

    if sample_rate_hz <= 0:
        raise ValueError("adaptive response sample rate must be positive")
    probe = round(sample_rate_hz * 0.008)
    return StarlinkAdaptiveRefinementPlanV0_1(
        probe,
        round(sample_rate_hz * ADAPTIVE_SENTINEL_STRIDE_SECONDS),
        round(sample_rate_hz * ADAPTIVE_LOCAL_RADIUS_SECONDS),
        round(sample_rate_hz * ADAPTIVE_LOCAL_STRIDE_SECONDS),
        ADAPTIVE_CENTERS_PER_PATTERN,
        ADAPTIVE_MAXIMUM_POWER_SEEDS,
        ADAPTIVE_MAXIMUM_BASE_WINDOWS,
        ADAPTIVE_MAXIMUM_EXACT_WINDOWS,
    )


def _bounded_power_seeds(
    windows: list[FullDwellRefinementWindowV0_1],
) -> list[FullDwellRefinementWindowV0_1]:
    ranks = [item.rank for item in windows]
    if len(ranks) != len(set(ranks)) or any(rank < 0 for rank in ranks):
        raise ValueError("adaptive response power-seed ranks are invalid")
    return sorted(windows, key=lambda item: item.rank)[:ADAPTIVE_MAXIMUM_POWER_SEEDS]
