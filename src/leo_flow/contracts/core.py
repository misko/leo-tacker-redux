"""Contract v0.1 identity, schema, hashing, and provenance primitives."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any, ClassVar, NewType, Self

from ._validation import require_nonnegative, require_token, require_utc_ns

UtcNs = NewType("UtcNs", int)


@dataclass(frozen=True, order=True)
class SchemaVersion:
    major: int
    minor: int

    def __post_init__(self) -> None:
        require_nonnegative(self.major, "major")
        require_nonnegative(self.minor, "minor")

    @classmethod
    def parse(cls, value: str) -> SchemaVersion:
        match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)
        if match is None:
            raise ValueError("schema version must be '<major>.<minor>'")
        return cls(int(match.group(1)), int(match.group(2)))

    def can_read(self, produced: SchemaVersion) -> bool:
        """A reader handles its own major up through its declared minor."""
        return self.major == produced.major and produced.minor <= self.minor

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


V0_1 = SchemaVersion(0, 1)


class ContractId(str):
    """Immutable opaque ID whose namespace is carried by its concrete type."""

    prefix: ClassVar[str] = "id"

    def __new__(cls, value: str) -> Self:
        require_token(value, cls.__name__)
        if not value.startswith(f"{cls.prefix}_"):
            raise ValueError(f"{cls.__name__} must start with '{cls.prefix}_'")
        return str.__new__(cls, value)


class PlanId(ContractId):
    prefix = "plan"


class ActivityId(ContractId):
    prefix = "act"


class SegmentId(ContractId):
    prefix = "seg"


class RecordingId(ContractId):
    prefix = "rec"


class CaptureBatchId(ContractId):
    prefix = "cbatch"


class CaptureAttemptId(ContractId):
    prefix = "cattempt"


class StationId(ContractId):
    prefix = "station"


class RadioId(ContractId):
    prefix = "radio"


class ReceiverChainId(ContractId):
    prefix = "rx"


class ReceiverPairId(ContractId):
    prefix = "rxpair"


class HardwareSnapshotId(ContractId):
    prefix = "hw"


class AnalysisRunId(ContractId):
    prefix = "arun"


class FeatureId(ContractId):
    prefix = "feature"


class FeatureSetId(ContractId):
    prefix = "fset"


class DatasetSnapshotId(ContractId):
    prefix = "dataset"


class DetectorEvaluationId(ContractId):
    prefix = "eval"


class EvaluationRunId(ContractId):
    prefix = "erun"


class ModelRunId(ContractId):
    prefix = "mrun"


class ModelSnapshotId(ContractId):
    prefix = "model"


class EphemerisSnapshotId(ContractId):
    prefix = "eph"


class EphemerisRetrievalId(ContractId):
    prefix = "ephret"


class JobId(ContractId):
    prefix = "job"


class DigestAlgorithm(str, Enum):
    SHA256 = "sha256"


@dataclass(frozen=True)
class Digest:
    algorithm: DigestAlgorithm
    value: str

    def __post_init__(self) -> None:
        expected = 64 if self.algorithm is DigestAlgorithm.SHA256 else 0
        if (
            len(self.value) != expected
            or re.fullmatch(r"[0-9a-f]+", self.value) is None
        ):
            raise ValueError(f"invalid {self.algorithm.value} digest")

    @classmethod
    def sha256(cls, data: bytes) -> Digest:
        return cls(DigestAlgorithm.SHA256, hashlib.sha256(data).hexdigest())

    def __str__(self) -> str:
        return f"{self.algorithm.value}:{self.value}"


@dataclass(frozen=True)
class SchemaRef:
    schema_id: str
    version: SchemaVersion = V0_1

    def __post_init__(self) -> None:
        require_token(self.schema_id, "schema_id")


@dataclass(frozen=True)
class ArtifactRef:
    """Reference to immutable configuration, algorithm, or dependency content."""

    artifact_id: str
    digest: Digest
    schema: SchemaRef | None = None

    def __post_init__(self) -> None:
        require_token(self.artifact_id, "artifact_id")


@dataclass(frozen=True)
class Provenance:
    producer_name: str
    producer_version: str
    git_commit: str
    environment_digest: Digest
    normalized_config_digest: Digest
    input_digests: tuple[Digest, ...]
    dependency_digests: tuple[Digest, ...]
    started_utc_ns: UtcNs
    completed_utc_ns: UtcNs
    host_class: str

    def __post_init__(self) -> None:
        require_token(self.producer_name, "producer_name")
        require_token(self.producer_version, "producer_version")
        require_token(self.git_commit, "git_commit")
        require_token(self.host_class, "host_class")
        require_utc_ns(self.started_utc_ns, "started_utc_ns")
        require_utc_ns(self.completed_utc_ns, "completed_utc_ns")
        if self.completed_utc_ns < self.started_utc_ns:
            raise ValueError("provenance completion precedes start")
        if not self.input_digests:
            raise ValueError("provenance must close over at least one input digest")


def _primitive(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field.name: _primitive(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return _primitive(value.value)
    if isinstance(value, Digest):
        return {"algorithm": value.algorithm.value, "value": value.value}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {key: _primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_primitive(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON rejects NaN and Infinity")
        return 0 if value == 0.0 else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical JSON type: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode the v0.1 RFC-8785 domain used by contracts.

    Contracts admit only string keys, finite numbers, and JSON value types.  This
    intentionally excludes platform objects and normalizes negative zero.
    """
    return _encode_canonical(_primitive(value)).encode("utf-8")


def _encode_canonical(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        # UTF-8 encoding rejects lone surrogates, which I-JSON cannot carry.
        value.encode("utf-8")
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _ecmascript_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode_canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: key.encode("utf-16be"))
        return (
            "{"
            + ",".join(
                f"{_encode_canonical(key)}:{_encode_canonical(value[key])}"
                for key in keys
            )
            + "}"
        )
    raise TypeError(f"unsupported canonical JSON type: {type(value).__name__}")


def _ecmascript_number(value: float) -> str:
    """Render Python's shortest round-trip digits with ECMAScript thresholds."""
    if not math.isfinite(value):
        raise ValueError("canonical JSON rejects NaN and Infinity")
    if value == 0.0:
        return "0"
    absolute = abs(value)
    shortest = repr(value).lower()
    if 1e-6 <= absolute < 1e21:
        if "e" not in shortest:
            return shortest.removesuffix(".0")
        mantissa, exponent_text = shortest.split("e")
        exponent = int(exponent_text)
        sign = ""
        if mantissa.startswith("-"):
            sign, mantissa = "-", mantissa[1:]
        whole, _, fraction = mantissa.partition(".")
        digits = whole + fraction
        decimal_position = len(whole) + exponent
        if decimal_position <= 0:
            return sign + "0." + "0" * (-decimal_position) + digits
        if decimal_position >= len(digits):
            return sign + digits + "0" * (decimal_position - len(digits))
        return sign + digits[:decimal_position] + "." + digits[decimal_position:]
    mantissa, separator, exponent_text = shortest.partition("e")
    if not separator:
        # Values outside fixed notation normally have an exponent in repr, but
        # retain a deterministic fallback for alternate Python implementations.
        return mantissa.removesuffix(".0")
    exponent = int(exponent_text)
    return f"{mantissa.removesuffix('.0')}e{'+' if exponent >= 0 else ''}{exponent}"


def canonical_digest(value: Any) -> Digest:
    return Digest.sha256(canonical_json_bytes(value))
