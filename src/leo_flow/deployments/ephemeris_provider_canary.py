"""One-shot, operator-gated ephemeris provider canary.

Without ``--allow-network`` this executes the complete archive and verification
tail against a deterministic in-process TLE fixture.  Live provider I/O requires
both a reviewed config opt-in and the command-line flag.
"""

from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

from leo_flow.adapters.ephemeris_http import (
    SpaceTrackSessionTransport,
    UrllibHttpTransport,
)
from leo_flow.adapters.systemd_credentials import SystemdCredentialProvider
from leo_flow.analysis.ephemeris.archive import (
    CasEphemerisProvenanceArchive,
    CasNormalizedEphemerisArchive,
    CasRawEphemerisArchive,
)
from leo_flow.analysis.ephemeris.catalog import InMemoryEphemerisSnapshotCatalog
from leo_flow.analysis.ephemeris.ingestion import (
    EphemerisIngestionConfig,
    EphemerisIngestionService,
)
from leo_flow.analysis.ephemeris.normalization import (
    TLECatalogNormalizer,
    TLEValidationPolicy,
    TLEValidator,
    decode_normalized_catalog,
    parse_tle_catalog,
)
from leo_flow.analysis.ephemeris.providers import (
    HttpResponse,
    HttpTransport,
    HuggingFaceRetriever,
    ProviderCredentials,
    SpaceTrackRetriever,
)
from leo_flow.analysis.orbit.association import StationGeometrySnapshot
from leo_flow.analysis.orbit.sgp4_adapter import (
    Sgp4OrbitPropagator,
    sgp4_vallado_wgs72_specification,
)
from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    EphemerisRetrievalId,
    SchemaRef,
    StationId,
    UtcNs,
    canonical_digest,
    canonical_json_bytes,
)
from leo_flow.contracts.ephemeris import (
    EphemerisRetrievalRequest,
    EphemerisSource,
)
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.filesystem import FileSystemBlobStore

CONFIG_SCHEMA = "org.leo-flow.ephemeris-provider-canary-config"
CONFIG_VERSION = "0.1"
RECEIPT_SCHEMA = "org.leo-flow.ephemeris-provider-canary-receipt/v1"
RECEIPT_FORMAT_ID = "ephemeris-provider-canary-receipt-v1"
MAX_CANARY_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_CANARY_TIMEOUT_S = 60.0
MIN_RATE_INTERVAL_S = 60
MAX_RATE_INTERVAL_S = 24 * 60 * 60
_MAX_RELATIVE_EPOCH_S = 366 * 24 * 60 * 60
_FIXTURE_EPOCH = "24200.50000000"
_NS_PER_SECOND = 1_000_000_000


class ProviderCanaryError(RuntimeError):
    """Stable operator-facing canary failure without provider diagnostics."""


class NamedCredentialProvider(Protocol):
    def resolve(self, name: str) -> str: ...


@dataclass(frozen=True)
class CredentialCapabilityNames:
    provider: str
    identity_name: str
    password_name: str

    def __post_init__(self) -> None:
        if self.provider != "systemd-credential":
            raise ValueError("Space-Track requires the systemd-credential provider")
        for value in (self.identity_name, self.password_name):
            if not value or value in {".", ".."} or "/" in value or "\x00" in value:
                raise ValueError("credential capability name is invalid")
        if self.identity_name == self.password_name:
            raise ValueError("Space-Track credential capabilities must be distinct")


@dataclass(frozen=True)
class CanaryBounds:
    timeout_s: float
    max_response_bytes: int
    minimum_request_interval_s: int

    def __post_init__(self) -> None:
        if not 0 < self.timeout_s <= MAX_CANARY_TIMEOUT_S:
            raise ValueError("timeout_s must lie in (0, 60]")
        if not 0 < self.max_response_bytes <= MAX_CANARY_RESPONSE_BYTES:
            raise ValueError("max_response_bytes must lie in [1, 16777216]")
        if not (
            MIN_RATE_INTERVAL_S
            <= self.minimum_request_interval_s
            <= MAX_RATE_INTERVAL_S
        ):
            raise ValueError("minimum request interval must lie in [60, 86400]")


@dataclass(frozen=True)
class CanaryValidation:
    minimum_satellites: int
    maximum_satellites: int
    maximum_epoch_age_s: int
    maximum_future_skew_s: int

    def __post_init__(self) -> None:
        if not 0 < self.minimum_satellites <= self.maximum_satellites <= 100_000:
            raise ValueError("satellite count bounds are invalid")
        if not 0 < self.maximum_epoch_age_s <= _MAX_RELATIVE_EPOCH_S:
            raise ValueError("maximum_epoch_age_s is invalid")
        if not 0 <= self.maximum_future_skew_s <= _MAX_RELATIVE_EPOCH_S:
            raise ValueError("maximum_future_skew_s is invalid")


@dataclass(frozen=True)
class ProviderCanaryConfig:
    source: EphemerisSource
    endpoint_profile: str
    scope: str
    request_spec: str
    attribution: str
    network_approved: bool
    bounds: CanaryBounds
    validation: CanaryValidation
    credential_capabilities: CredentialCapabilityNames | None

    def __post_init__(self) -> None:
        expected_profile = {
            EphemerisSource.HUGGING_FACE: "huggingface-starlink-main-v1",
            EphemerisSource.SPACE_TRACK: "space-track-starlink-gp-v1",
        }[self.source]
        if self.endpoint_profile != expected_profile:
            raise ValueError("provider endpoint profile is unsupported")
        for name, value in (
            ("scope", self.scope),
            ("request_spec", self.request_spec),
        ):
            if not value or any(character.isspace() for character in value):
                raise ValueError(f"{name} must be a token")
        if not self.attribution:
            raise ValueError("provider attribution is required")
        if not isinstance(self.network_approved, bool):
            raise TypeError("network_approved must be boolean")
        if (
            self.source is EphemerisSource.HUGGING_FACE
            and self.credential_capabilities is not None
        ):
            raise ValueError("Hugging Face canary refuses credential capabilities")
        if (
            self.source is EphemerisSource.SPACE_TRACK
            and self.credential_capabilities is None
        ):
            raise ValueError("Space-Track requires named credential capabilities")

    @property
    def digest(self) -> Digest:
        return canonical_digest(self)


@dataclass(frozen=True)
class CanaryOutcome:
    mode: str
    live_retrieval_performed: bool
    snapshot_id: str
    receipt_ref: ObjectRef


@dataclass(frozen=True)
class _FixtureTransport:
    body: bytes
    expected_credentials: bool
    calls: int = 0

    def send(
        self,
        request: object,
        *,
        credentials: ProviderCredentials | None = None,
    ) -> HttpResponse:
        del request
        if (credentials is not None) is not self.expected_credentials:
            raise ProviderCanaryError("fixture credential boundary differs")
        object.__setattr__(self, "calls", self.calls + 1)
        return HttpResponse(
            200,
            (
                ("Content-Type", "text/plain"),
                ("Content-Length", str(len(self.body))),
            ),
            (self.body,),
        )


class _FixedClock:
    def __init__(self, values: tuple[int, ...]) -> None:
        self._values = iter(values)

    def __call__(self) -> int:
        try:
            return next(self._values)
        except StopIteration as error:
            raise ProviderCanaryError("fixture clock call bound exceeded") from error


@dataclass(frozen=True)
class _CanaryEphemerisView:
    ref: Any
    payload: bytes

    def normalized_bytes(self) -> bytes:
        return self.payload


class _CanaryEphemerisReader:
    """Read only the exact cataloged snapshot and verify its CAS object."""

    def __init__(
        self,
        catalog: InMemoryEphemerisSnapshotCatalog,
        blobs: FileSystemBlobStore,
    ) -> None:
        self._catalog = catalog
        self._blobs = blobs

    @contextmanager
    def open(self, ref: Any) -> Iterator[_CanaryEphemerisView]:
        archived = self._catalog.get(ref.snapshot_id)
        if archived is None or archived.snapshot_ref() != ref:
            raise ProviderCanaryError("cataloged ephemeris identity differs")
        normalized_ref = archived.snapshot.normalized_object_ref
        if not self._blobs.head(normalized_ref).verified:
            raise ProviderCanaryError("cataloged normalized object is not verified")
        with self._blobs.open(normalized_ref) as stream:
            payload = stream.read(normalized_ref.byte_count + 1)
        if (
            len(payload) != normalized_ref.byte_count
            or Digest.sha256(payload) != normalized_ref.digest
        ):
            raise ProviderCanaryError("cataloged normalized bytes differ")
        yield _CanaryEphemerisView(ref, payload)


def load_canary_config(path: Path) -> ProviderCanaryConfig:
    """Load exact provider labels and capability names, never secret values."""

    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ProviderCanaryError("canary configuration cannot be read") from error
    root = _object(value, "configuration")
    _exact_keys(
        root,
        {
            "schema_id",
            "schema_version",
            "provider",
            "endpoint_profile",
            "scope",
            "request_spec",
            "attribution",
            "network_approved",
            "bounds",
            "validation",
            "credential_capabilities",
        },
        "configuration",
    )
    if root["schema_id"] != CONFIG_SCHEMA or root["schema_version"] != CONFIG_VERSION:
        raise ProviderCanaryError("unsupported canary configuration schema")
    try:
        source = EphemerisSource(_string(root, "provider"))
        bounds_value = _object(root["bounds"], "bounds")
        _exact_keys(
            bounds_value,
            {"timeout_s", "max_response_bytes", "minimum_request_interval_s"},
            "bounds",
        )
        validation_value = _object(root["validation"], "validation")
        _exact_keys(
            validation_value,
            {
                "minimum_satellites",
                "maximum_satellites",
                "maximum_epoch_age_s",
                "maximum_future_skew_s",
            },
            "validation",
        )
        capabilities = _credential_capabilities(root["credential_capabilities"])
        network_approved = root["network_approved"]
        if not isinstance(network_approved, bool):
            raise TypeError("network_approved must be boolean")
        return ProviderCanaryConfig(
            source,
            _string(root, "endpoint_profile"),
            _string(root, "scope"),
            _string(root, "request_spec"),
            _string(root, "attribution"),
            network_approved,
            CanaryBounds(
                _number(bounds_value, "timeout_s"),
                _integer(bounds_value, "max_response_bytes"),
                _integer(bounds_value, "minimum_request_interval_s"),
            ),
            CanaryValidation(
                _integer(validation_value, "minimum_satellites"),
                _integer(validation_value, "maximum_satellites"),
                _integer(validation_value, "maximum_epoch_age_s"),
                _integer(validation_value, "maximum_future_skew_s"),
            ),
            capabilities,
        )
    except ProviderCanaryError:
        raise
    except (TypeError, ValueError) as error:
        raise ProviderCanaryError(str(error)) from error


def run_provider_canary(
    config: ProviderCanaryConfig,
    root: Path,
    *,
    allow_network: bool = False,
    credential_provider: NamedCredentialProvider | None = None,
    transport: HttpTransport | None = None,
    now_utc_ns: Callable[[], int] = time.time_ns,
) -> CanaryOutcome:
    """Run exactly one fixture or explicitly authorized provider retrieval."""

    _safe_root(root)
    if allow_network and not config.network_approved:
        raise ProviderCanaryError(
            "network requires both reviewed config approval and --allow-network"
        )
    mode = "network" if allow_network else "fixture"
    live = allow_network and transport is None
    closer: Callable[[], None] = lambda: None
    with (
        _network_rate_permit(config, root, UtcNs(now_utc_ns()))
        if allow_network
        else nullcontext()
    ):
        blobs = FileSystemBlobStore(root / "cas")
        raw_archive = CasRawEphemerisArchive(blobs)
        normalized_archive = CasNormalizedEphemerisArchive(blobs)
        provenance_archive = CasEphemerisProvenanceArchive(blobs)
        if allow_network:
            clock = now_utc_ns
            selected_transport, credentials, closer = _network_capabilities(
                config,
                credential_provider or SystemdCredentialProvider(),
                transport,
            )
        else:
            body = _fixture_tle()
            epoch = int(parse_tle_catalog(body)[0].epoch_utc_ns)
            clock = _FixedClock((epoch, epoch + 1))
            selected_transport = _FixtureTransport(
                body,
                config.source is EphemerisSource.SPACE_TRACK,
            )
            credentials = (
                ProviderCredentials("fixture-identity", "fixture-password")
                if config.source is EphemerisSource.SPACE_TRACK
                else None
            )
        retriever = _retriever(
            config,
            selected_transport,
            raw_archive,
            clock,
            credentials,
        )
        parser_ref = _parser_ref()
        validation_ref = _validation_ref(config)
        catalog = InMemoryEphemerisSnapshotCatalog()
        service = EphemerisIngestionService(
            retriever,
            TLECatalogNormalizer(
                blobs,
                normalized_archive,
                source=config.source,
                scope=config.scope,
                attribution=config.attribution,
                max_raw_bytes=config.bounds.max_response_bytes,
            ),
            TLEValidator(
                (
                    TLEValidationPolicy(
                        validation_ref,
                        config.validation.minimum_satellites,
                        config.validation.maximum_satellites,
                        config.validation.maximum_epoch_age_s,
                        config.validation.maximum_future_skew_s,
                    ),
                )
            ),
            provenance_archive,
            catalog,
            EphemerisIngestionConfig(parser_ref, validation_ref),
        )
        slot = (
            now_utc_ns() // (config.bounds.minimum_request_interval_s * 1_000_000_000)
            if allow_network
            else 0
        )
        retrieval_identity = canonical_digest(
            {"config_digest": config.digest, "mode": mode, "cadence_slot": slot}
        )
        request = EphemerisRetrievalRequest(
            EphemerisRetrievalId(f"ephret_canary_{retrieval_identity.value[:32]}"),
            config.source,
            config.scope,
            config.request_spec,
        )
        try:
            archived = service.acquire(request)
        finally:
            closer()
        receipt_body = _receipt_body(config, mode, live, archived, blobs, catalog)
        receipt_bytes = _receipt_bytes(receipt_body)
        verify_canary_receipt(receipt_bytes)
        receipt_digest = Digest.sha256(receipt_bytes)
        receipt_ref = blobs.put(
            io.BytesIO(receipt_bytes),
            expected_digest=receipt_digest,
            expected_bytes=len(receipt_bytes),
            media_type="application/json",
            format_id=RECEIPT_FORMAT_ID,
            idempotency_key=f"ephemeris:canary-receipt:{receipt_digest.value}",
        )
        with blobs.open(receipt_ref) as stream:
            if stream.read() != receipt_bytes:
                raise ProviderCanaryError("archived canary receipt verification failed")
        return CanaryOutcome(
            mode, live, str(archived.snapshot.snapshot_id), receipt_ref
        )


def verify_canary_receipt(payload: bytes) -> Mapping[str, Any]:
    """Verify canonical receipt bytes and their internal scientific digest."""

    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderCanaryError("canary receipt is not JSON") from error
    if canonical_json_bytes(value) != payload or not isinstance(value, dict):
        raise ProviderCanaryError("canary receipt is not canonical")
    if set(value) != {"schema", "receipt", "receipt_digest"}:
        raise ProviderCanaryError("canary receipt fields differ")
    if value["schema"] != RECEIPT_SCHEMA or not isinstance(value["receipt"], dict):
        raise ProviderCanaryError("canary receipt schema differs")
    expected = str(Digest.sha256(canonical_json_bytes(value["receipt"])))
    if value["receipt_digest"] != expected:
        raise ProviderCanaryError("canary receipt digest differs")
    return value["receipt"]


def _receipt_body(
    config: ProviderCanaryConfig,
    mode: str,
    live: bool,
    archived: Any,
    blobs: FileSystemBlobStore,
    catalog: InMemoryEphemerisSnapshotCatalog,
) -> dict[str, Any]:
    snapshot = archived.snapshot
    refs = (
        snapshot.raw_object_ref,
        snapshot.normalized_object_ref,
        archived.provenance_object_ref,
    )
    for ref in refs:
        if not blobs.head(ref).verified:
            raise ProviderCanaryError("archived ephemeris object was not verified")
    with blobs.open(snapshot.normalized_object_ref) as stream:
        normalized = stream.read()
    entries = decode_normalized_catalog(normalized, snapshot.source)
    with blobs.open(archived.provenance_object_ref) as stream:
        provenance_bytes = stream.read()
    try:
        provenance = json.loads(provenance_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderCanaryError("archived provenance is not JSON") from error
    if canonical_json_bytes(provenance) != provenance_bytes:
        raise ProviderCanaryError("archived provenance is not canonical")
    if (
        provenance.get("snapshot_id") != str(snapshot.snapshot_id)
        or provenance.get("provider") != config.source.value
        or provenance.get("raw", {}).get("digest")
        != str(snapshot.raw_object_ref.digest)
        or provenance.get("normalized", {}).get("digest")
        != str(snapshot.normalized_object_ref.digest)
    ):
        raise ProviderCanaryError("archived provenance bindings differ")
    propagation = _propagation_document(archived, entries, blobs, catalog)
    capability_names: list[str] = []
    if config.credential_capabilities is not None:
        capability_names = [
            config.credential_capabilities.identity_name,
            config.credential_capabilities.password_name,
        ]
    return {
        "mode": mode,
        "live_retrieval_performed": live,
        "provider": config.source.value,
        "endpoint_profile": config.endpoint_profile,
        "scope": config.scope,
        "request_spec": config.request_spec,
        "config_digest": str(config.digest),
        "bounds": {
            "maximum_provider_data_requests": 1,
            "timeout_s": config.bounds.timeout_s,
            "max_response_bytes": config.bounds.max_response_bytes,
            "minimum_request_interval_s": config.bounds.minimum_request_interval_s,
        },
        "credential_capability_names": capability_names,
        "credential_values_archived": False,
        "snapshot": {
            "snapshot_id": str(snapshot.snapshot_id),
            "retrieval_id": str(snapshot.retrieval_id),
            "retrieved_at_utc_ns": int(snapshot.retrieved_at_utc_ns),
            "raw": _object_document(snapshot.raw_object_ref),
            "normalized": _object_document(snapshot.normalized_object_ref),
            "provenance": _object_document(archived.provenance_object_ref),
            "parser_ref": _artifact_document(snapshot.parser_ref),
            "validation_policy_ref": _artifact_document(snapshot.validation.policy_ref),
            "satellite_count": snapshot.satellite_count,
            "norad_ids": [entry.norad_id for entry in entries],
            "element_epochs_utc_ns": [int(entry.epoch_utc_ns) for entry in entries],
            "norad_id_set_digest": str(snapshot.norad_id_set_digest),
            "element_epoch_min_utc_ns": int(snapshot.element_epoch_min_utc_ns),
            "element_epoch_max_utc_ns": int(snapshot.element_epoch_max_utc_ns),
            "attribution": snapshot.attribution,
        },
        "propagation": propagation,
        "verification": {
            "raw_object_hash_and_size": "verified",
            "normalized_object_hash_size_and_parse": "verified",
            "provenance_hash_size_and_bindings": "verified",
            "receipt_internal_digest": "verified-before-archive",
        },
    }


def _propagation_document(
    archived: Any,
    entries: Sequence[Any],
    blobs: FileSystemBlobStore,
    catalog: InMemoryEphemerisSnapshotCatalog,
) -> dict[str, Any]:
    if not entries:
        raise ProviderCanaryError("cataloged ephemeris has no propagation candidate")
    station_identity = {
        "station_id": "station_ephemeris_canary",
        "frame": "ITRF",
        "position_m": (6_378_135.0, 0.0, 0.0),
    }
    station = StationGeometrySnapshot(
        StationId("station_ephemeris_canary"),
        "ITRF",
        (6_378_135.0, 0.0, 0.0),
        canonical_digest(station_identity),
    )
    specification = sgp4_vallado_wgs72_specification()
    entry = entries[0]
    state = Sgp4OrbitPropagator(_CanaryEphemerisReader(catalog, blobs)).propagate(
        archived.snapshot_ref(),
        station,
        specification,
        entry.norad_id,
        entry.epoch_utc_ns,
    )
    if state.error_code is not None:
        raise ProviderCanaryError("pinned SGP4 propagation returned an error")
    snapshot_ref = archived.snapshot_ref()
    return {
        "status": "verified",
        "input_path": "archived-normalized-tle->catalog-exact-ref->pinned-sgp4",
        "snapshot_ref": {
            "snapshot_id": str(snapshot_ref.snapshot_id),
            "source": snapshot_ref.source.value,
            "raw_digest": str(snapshot_ref.raw_digest),
            "normalized_digest": str(snapshot_ref.normalized_digest),
        },
        "norad_id": state.norad_id,
        "utc_ns": int(state.utc_ns),
        "station_digest": str(station.digest),
        "propagation_specification": {
            "propagator_ref": _artifact_document(specification.propagator_ref),
            "gravity_model_ref": _artifact_document(specification.gravity_model_ref),
            "time_scale_ref": _artifact_document(specification.time_scale_ref),
            "earth_orientation_ref": _artifact_document(
                specification.earth_orientation_ref
            ),
            "error_policy_ref": _artifact_document(specification.error_policy_ref),
        },
        "state": {
            "range_rate_m_s": state.range_rate_m_s,
            "range_acceleration_m_s2": state.range_acceleration_m_s2,
            "elevation_deg": state.elevation_deg,
            "error_code": state.error_code,
        },
    }


def _receipt_bytes(body: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(
        {
            "schema": RECEIPT_SCHEMA,
            "receipt_digest": str(Digest.sha256(canonical_json_bytes(body))),
            "receipt": body,
        }
    )


def _retriever(
    config: ProviderCanaryConfig,
    transport: HttpTransport,
    archive: CasRawEphemerisArchive,
    clock: Callable[[], int],
    credentials: ProviderCredentials | None,
) -> HuggingFaceRetriever | SpaceTrackRetriever:
    if config.source is EphemerisSource.HUGGING_FACE:
        if credentials is not None:
            raise ProviderCanaryError("Hugging Face canary refuses credentials")
        return HuggingFaceRetriever(
            transport,
            archive,
            clock,
            max_response_bytes=config.bounds.max_response_bytes,
        )
    if credentials is None:
        raise ProviderCanaryError("Space-Track credential capability is unavailable")
    return SpaceTrackRetriever(
        transport,
        archive,
        clock,
        credentials,
        max_response_bytes=config.bounds.max_response_bytes,
    )


def _network_capabilities(
    config: ProviderCanaryConfig,
    credential_provider: NamedCredentialProvider,
    injected_transport: HttpTransport | None,
) -> tuple[HttpTransport, ProviderCredentials | None, Callable[[], None]]:
    if config.source is EphemerisSource.HUGGING_FACE:
        return (
            injected_transport
            or UrllibHttpTransport(
                allowed_hosts=("huggingface.co",),
                timeout_s=config.bounds.timeout_s,
            ),
            None,
            lambda: None,
        )
    capabilities = config.credential_capabilities
    if capabilities is None:
        raise ProviderCanaryError(
            "Space-Track credential capabilities are not configured"
        )
    try:
        identity = credential_provider.resolve(capabilities.identity_name)
        password = credential_provider.resolve(capabilities.password_name)
    except Exception as error:
        raise ProviderCanaryError("Space-Track credential resolution failed") from error
    credentials = ProviderCredentials(identity, password)
    if injected_transport is not None:
        return injected_transport, credentials, lambda: None
    transport = SpaceTrackSessionTransport(timeout_s=config.bounds.timeout_s)
    return transport, credentials, transport.close


@contextmanager
def _network_rate_permit(
    config: ProviderCanaryConfig, root: Path, attempted_utc_ns: UtcNs
) -> Iterator[None]:
    lock_path = root / f"{config.source.value}.rate.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProviderCanaryError(
                "another provider canary holds the rate gate"
            ) from error
        state_path = root / f"{config.source.value}.rate.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_bytes())
                previous = int(state["last_attempt_utc_ns"])
            except (
                OSError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                raise ProviderCanaryError("provider rate state is invalid") from error
            minimum_ns = config.bounds.minimum_request_interval_s * _NS_PER_SECOND
            if int(attempted_utc_ns) - previous < minimum_ns:
                raise ProviderCanaryError(
                    "provider minimum request interval has not elapsed"
                )
        temporary = root / f".{config.source.value}.rate.{os.getpid()}.tmp"
        payload = canonical_json_bytes(
            {
                "provider": config.source.value,
                "last_attempt_utc_ns": int(attempted_utc_ns),
            }
        )
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, state_path)
        finally:
            temporary.unlink(missing_ok=True)
        yield
    finally:
        os.close(descriptor)


def _parser_ref() -> ArtifactRef:
    return ArtifactRef(
        "tle-parser-v1",
        canonical_digest(
            {
                "parser": "leo-flow-strict-ascii-tle",
                "version": "1",
                "normalization": "norad-ascending-canonical-json",
            }
        ),
        SchemaRef("org.leo-flow.tle-parser"),
    )


def _validation_ref(config: ProviderCanaryConfig) -> ArtifactRef:
    return ArtifactRef(
        "provider-canary-tle-validation-v1",
        canonical_digest(config.validation),
        SchemaRef("org.leo-flow.tle-validation-policy"),
    )


def _fixture_tle() -> bytes:
    def checked(body: str) -> str:
        checksum = (
            sum(
                int(character) if character.isdigit() else 1 if character == "-" else 0
                for character in body
            )
            % 10
        )
        return body + str(checksum)

    line1 = checked(
        f"1 12345U 24001A   {_FIXTURE_EPOCH}  .00000000  00000-0  00000-0 0  999"
    )
    line2 = checked(
        "2 12345  53.0000 100.0000 0001000  10.0000 350.0000 15.00000000    1"
    )
    return f"0 STARLINK CANARY FIXTURE\n{line1}\n{line2}\n".encode("ascii")


def _safe_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise ProviderCanaryError("canary root must be a real directory")


def _object_document(ref: ObjectRef) -> dict[str, object]:
    return {
        "digest": str(ref.digest),
        "byte_count": ref.byte_count,
        "media_type": ref.media_type,
        "format_id": ref.format_id,
        "locator": ref.locator,
    }


def _artifact_document(ref: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": ref.artifact_id,
        "digest": str(ref.digest),
        "schema": None if ref.schema is None else ref.schema.schema_id,
    }


def _credential_capabilities(value: object) -> CredentialCapabilityNames | None:
    if value is None:
        return None
    item = _object(value, "credential_capabilities")
    _exact_keys(
        item,
        {"provider", "identity_name", "password_name"},
        "credential_capabilities",
    )
    return CredentialCapabilityNames(
        _string(item, "provider"),
        _string(item, "identity_name"),
        _string(item, "password_name"),
    )


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProviderCanaryError(f"{name} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ProviderCanaryError(f"{name} fields differ")


def _string(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result:
        raise ProviderCanaryError(f"{name} must be a non-empty string")
    return result


def _integer(value: Mapping[str, Any], name: str) -> int:
    result = value.get(name)
    if isinstance(result, bool) or not isinstance(result, int):
        raise ProviderCanaryError(f"{name} must be an integer")
    return result


def _number(value: Mapping[str, Any], name: str) -> float:
    result = value.get(name)
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ProviderCanaryError(f"{name} must be a number")
    return float(result)


def _outcome_document(outcome: CanaryOutcome) -> dict[str, object]:
    return {
        "event": "ephemeris_provider_canary",
        "status": "pass",
        "mode": outcome.mode,
        "live_retrieval_performed": outcome.live_retrieval_performed,
        "snapshot_id": outcome.snapshot_id,
        "receipt_ref": _object_document(outcome.receipt_ref),
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--root",
        type=Path,
        help="persistent canary CAS/rate root; required for live retrieval",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="explicitly allow one provider data request when config is approved",
    )
    args = parser.parse_args(argv)
    if args.allow_network and args.root is None:
        stderr.write(
            '{"event":"ephemeris_provider_canary","status":"failed",'
            '"detail":"persistent_root_required"}\n'
        )
        return 2
    owner: AbstractContextManager[str]
    owner = (
        tempfile.TemporaryDirectory(prefix="leo-ephemeris-canary-")
        if args.root is None
        else nullcontext(os.fspath(args.root))
    )
    try:
        config = load_canary_config(args.config)
        with owner as supplied:
            outcome = run_provider_canary(
                config,
                Path(supplied),
                allow_network=args.allow_network,
            )
    except Exception as error:  # noqa: BLE001 - sanitized process boundary
        stderr.write(
            json.dumps(
                {
                    "event": "ephemeris_provider_canary",
                    "status": "failed",
                    "detail": type(error).__name__,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return 1
    stdout.write(
        json.dumps(_outcome_document(outcome), sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
