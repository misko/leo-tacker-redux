from __future__ import annotations

import json

import pytest

from leo_flow.analysis.ephemeris.archive import (
    CasEphemerisProvenanceArchive,
    CasNormalizedEphemerisArchive,
    CasRawEphemerisArchive,
)
from leo_flow.analysis.ephemeris.catalog import (
    EphemerisCatalogConflictError,
    InMemoryEphemerisSnapshotCatalog,
)
from leo_flow.analysis.ephemeris.ingestion import (
    EphemerisIngestionConfig,
    EphemerisIngestionService,
    InvalidEphemerisCandidateError,
)
from leo_flow.analysis.ephemeris.linkage import resolve_recording_ephemeris
from leo_flow.analysis.ephemeris.normalization import (
    TLECatalogNormalizer,
    TLEValidationPolicy,
    TLEValidator,
    parse_tle_catalog,
)
from leo_flow.analysis.ephemeris.providers import (
    HttpResponse,
    HuggingFaceRetriever,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    EphemerisRetrievalId,
    RecordingId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.ephemeris import (
    EphemerisRetrievalRequest,
    EphemerisSelectionPolicy,
    EphemerisSource,
    RecordingInterval,
)
from leo_flow.storage.filesystem import FileSystemBlobStore
from testkit import digest

from ._fixtures import tle


class Clock:
    def __init__(self, initial_utc_ns: int = 1_721_177_000_000_000_000) -> None:
        self.value = initial_utc_ns

    def __call__(self) -> int:
        self.value += 1
        return self.value


class Transport:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls = 0

    def send(self, _request, *, credentials=None):
        assert credentials is None
        self.calls += 1
        return HttpResponse(
            200,
            (("Content-Type", "text/plain"), ("Content-Length", str(len(self.body)))),
            (self.body,),
        )


def make_service(
    tmp_path,
    body: bytes,
    *,
    minimum_satellites: int = 1,
    clock_start_utc_ns: int = 1_721_177_000_000_000_000,
):
    blobs = FileSystemBlobStore(tmp_path / "cas")
    raw_archive = CasRawEphemerisArchive(blobs)
    normalized_archive = CasNormalizedEphemerisArchive(blobs)
    provenance_archive = CasEphemerisProvenanceArchive(blobs)
    transport = Transport(body)
    catalog = InMemoryEphemerisSnapshotCatalog()
    parser = ArtifactRef("tle-parser-v1", digest("parser"))
    validation = ArtifactRef(
        "tle-validation-v1",
        digest("validation"),
        SchemaRef("org.leo-flow.tle-validation-policy"),
    )
    service = EphemerisIngestionService(
        HuggingFaceRetriever(transport, raw_archive, Clock(clock_start_utc_ns)),
        TLECatalogNormalizer(
            blobs,
            normalized_archive,
            source=EphemerisSource.HUGGING_FACE,
            scope="starlink",
            attribution="Hugging Face dataset",
        ),
        TLEValidator(
            (
                TLEValidationPolicy(
                    validation,
                    minimum_satellites,
                    100_000,
                    31 * 24 * 60 * 60,
                    31 * 24 * 60 * 60,
                ),
            )
        ),
        provenance_archive,
        catalog,
        EphemerisIngestionConfig(parser, validation),
    )
    return service, transport, blobs, catalog


def request(identity: str = "ephret_one") -> EphemerisRetrievalRequest:
    return EphemerisRetrievalRequest(
        EphemerisRetrievalId(identity),
        EphemerisSource.HUGGING_FACE,
        "starlink",
        "hf-starlink-main-v1",
    )


def test_end_to_end_archives_raw_normalized_and_provenance_in_cas(tmp_path) -> None:
    service, transport, blobs, catalog = make_service(tmp_path, tle())
    archived = service.acquire(request())

    with blobs.open(archived.snapshot.raw_object_ref) as stream:
        assert stream.read() == tle()
    with blobs.open(archived.snapshot.normalized_object_ref) as stream:
        normalized = json.load(stream)
    assert normalized["source"] == "huggingface"
    assert normalized["entries"][0]["norad_id"] == 12345
    with blobs.open(archived.provenance_object_ref) as stream:
        provenance = json.load(stream)
    assert provenance["provider"] == "huggingface"
    assert provenance["request_spec"] == "hf-starlink-main-v1"
    assert provenance["provider_query"].startswith("https://huggingface.co/")
    assert provenance["raw"]["digest"].startswith("sha256:")
    assert provenance["normalized"]["digest"].startswith("sha256:")
    assert catalog.get(archived.snapshot.snapshot_id) == archived
    assert transport.calls == 1


def test_retrieval_id_is_idempotent_without_second_network_call(tmp_path) -> None:
    service, transport, _, _ = make_service(tmp_path, tle())
    first = service.acquire(request())
    second = service.acquire(request())
    assert second == first
    assert transport.calls == 1

    changed = EphemerisRetrievalRequest(
        request().retrieval_id,
        request().source,
        request().scope,
        "changed-query",
    )
    with pytest.raises(ValueError, match="different request"):
        service.acquire(changed)
    assert transport.calls == 1


def test_relative_policy_remains_valid_across_widely_separated_retrievals(
    tmp_path,
) -> None:
    for index, epoch in enumerate(("24200.50000000", "25200.50000000")):
        body = tle(epoch=epoch)
        element_epoch = int(parse_tle_catalog(body)[0].epoch_utc_ns)
        service, _, _, _ = make_service(
            tmp_path / str(index),
            body,
            clock_start_utc_ns=element_epoch - 2,
        )
        archived = service.acquire(request(f"ephret_long_running_{index}"))
        assert int(archived.snapshot.retrieved_at_utc_ns) == element_epoch
        assert archived.snapshot.validation.valid


def test_retrieval_relative_provenance_is_deterministic(tmp_path) -> None:
    first_service, _, first_blobs, _ = make_service(tmp_path / "first", tle())
    second_service, _, second_blobs, _ = make_service(tmp_path / "second", tle())

    first = first_service.acquire(request("ephret_deterministic"))
    second = second_service.acquire(request("ephret_deterministic"))

    assert first.snapshot == second.snapshot
    assert first.provenance_object_ref.digest == second.provenance_object_ref.digest
    with first_blobs.open(first.provenance_object_ref) as first_stream:
        first_bytes = first_stream.read()
    with second_blobs.open(second.provenance_object_ref) as second_stream:
        second_bytes = second_stream.read()
    assert first_bytes == second_bytes


def test_invalid_candidate_preserves_raw_but_is_not_published(tmp_path) -> None:
    service, _, _, catalog = make_service(tmp_path, tle(), minimum_satellites=2)
    with pytest.raises(InvalidEphemerisCandidateError) as raised:
        service.acquire(request())
    assert raised.value.reason_codes == ("satellite_count_below_minimum",)
    assert catalog.get_by_retrieval(request().retrieval_id) is None


def test_catalog_refuses_retrieval_identity_conflict(tmp_path) -> None:
    service, _, _, catalog = make_service(tmp_path, tle())
    archived = service.acquire(request())
    from dataclasses import replace

    with pytest.raises(EphemerisCatalogConflictError):
        catalog.publish(replace(archived, request_spec_digest="0" * 64))


def test_cross_recording_link_is_exact_and_temporally_auditable(tmp_path) -> None:
    service, _, _, catalog = make_service(tmp_path, tle())
    archived = service.acquire(request())
    interval = RecordingInterval(
        UtcNs(int(archived.snapshot.retrieved_at_utc_ns) + 10),
        UtcNs(int(archived.snapshot.retrieved_at_utc_ns) + 20),
    )
    policy_ref = ArtifactRef("available-then-v1", digest("available-then"))
    linked = resolve_recording_ephemeris(
        catalog=catalog,
        recording_id=RecordingId("rec_one"),
        recording_interval=interval,
        source=EphemerisSource.HUGGING_FACE,
        scope="starlink",
        policy=EphemerisSelectionPolicy.AVAILABLE_THEN,
        policy_ref=policy_ref,
        as_of_utc_ns=UtcNs(int(interval.finished_utc_ns) + 1),
    )
    assert linked.selection.snapshot_ref.snapshot_id == archived.snapshot.snapshot_id
    assert linked.normalized_object_ref == archived.snapshot.normalized_object_ref
    assert linked.provenance_object_ref == archived.provenance_object_ref
