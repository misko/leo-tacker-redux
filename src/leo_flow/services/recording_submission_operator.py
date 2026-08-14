"""Strict operator command for submitting one published recording.

This module is deliberately infrastructure-neutral.  The deployment adapter
supplies a durable catalog and queue; capture never imports or invokes it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol, cast

from leo_flow.contracts.core import (
    ArtifactRef,
    Digest,
    DigestAlgorithm,
    RecordingId,
    SchemaRef,
    SchemaVersion,
)
from leo_flow.contracts.features import FeatureSetBundle
from leo_flow.contracts.storage import PublishedRecordingRef

from .recording_submission import (
    RecordingAnalysisJobEnqueuer,
    RecordingAnalysisSubmission,
    RecordingAnalysisSubmissionService,
    SubmittedRecordingAnalysis,
)

OPERATOR_CONFIG_SCHEMA = "org.leo-flow.recording-analysis-submission"
OPERATOR_CONFIG_VERSION = "0.1"
MAX_OPERATOR_CONFIG_BYTES = 64 * 1024


class RecordingSubmissionOperatorError(RuntimeError):
    """An operator command is invalid or cannot cross the durable boundary."""


class PublishedRecordingCatalog(Protocol):
    """Analysis-side read capability over atomic recording publications."""

    def get(self, recording_id: RecordingId) -> PublishedRecordingRef | None: ...


@dataclass(frozen=True)
class ExactRecordingAnalysisSelection:
    """Every immutable identity required to analyze one recording."""

    recording_id: RecordingId
    algorithm_ref: ArtifactRef
    config_ref: ArtifactRef
    dependency_refs: tuple[ArtifactRef, ...]
    requested_output_schema: SchemaRef

    def __post_init__(self) -> None:
        if self.requested_output_schema != SchemaRef(FeatureSetBundle.SCHEMA_ID):
            raise RecordingSubmissionOperatorError(
                "requested output must be the exact supported FeatureSet schema"
            )
        dependency_ids = [ref.artifact_id for ref in self.dependency_refs]
        if len(dependency_ids) != len(set(dependency_ids)):
            raise RecordingSubmissionOperatorError(
                "dependency artifact IDs must be unique"
            )


@dataclass(frozen=True)
class RecordingSubmissionOperatorConfig:
    """Strict public command plus a reference to, never the value of, the DSN."""

    selection: ExactRecordingAnalysisSelection
    dsn_credential_name: str

    def __post_init__(self) -> None:
        if (
            not self.dsn_credential_name
            or self.dsn_credential_name in {".", ".."}
            or "/" in self.dsn_credential_name
            or "\x00" in self.dsn_credential_name
        ):
            raise RecordingSubmissionOperatorError(
                "database DSN credential name is invalid"
            )


class RecordingAnalysisSubmissionOperator:
    """Resolve one publication and enqueue its fully pinned analysis command."""

    def __init__(
        self,
        *,
        recordings: PublishedRecordingCatalog,
        jobs: RecordingAnalysisJobEnqueuer,
    ) -> None:
        self._recordings = recordings
        self._submission = RecordingAnalysisSubmissionService(jobs)

    def submit(
        self, selection: ExactRecordingAnalysisSelection
    ) -> SubmittedRecordingAnalysis:
        recording = self._recordings.get(selection.recording_id)
        if recording is None:
            raise RecordingSubmissionOperatorError(
                "recording is not present in the published recording catalog"
            )
        if recording.recording_id != selection.recording_id:
            raise RecordingSubmissionOperatorError(
                "recording catalog substituted a different recording identity"
            )
        return self._submission.submit(
            RecordingAnalysisSubmission(
                recording=recording,
                algorithm_ref=selection.algorithm_ref,
                config_ref=selection.config_ref,
                dependency_refs=selection.dependency_refs,
                requested_output_schema=selection.requested_output_schema,
            )
        )


def load_recording_submission_config(path: Path) -> RecordingSubmissionOperatorConfig:
    """Read one bounded, strict JSON command without resolving its secret."""

    try:
        raw = path.read_bytes()
        if len(raw) > MAX_OPERATOR_CONFIG_BYTES:
            _bad("recording submission configuration exceeds the size limit")
        document = json.loads(raw, object_pairs_hook=_unique)
        root = _object(document, "configuration")
        _keys(
            root,
            {"schema", "version", "recording_id", "analysis", "database_dsn"},
            "configuration",
        )
        if (
            root["schema"] != OPERATOR_CONFIG_SCHEMA
            or root["version"] != OPERATOR_CONFIG_VERSION
        ):
            _bad("recording submission configuration schema is unsupported")

        analysis = _object(root["analysis"], "analysis")
        _keys(
            analysis,
            {
                "algorithm_ref",
                "config_ref",
                "dependency_refs",
                "requested_output_schema",
            },
            "analysis",
        )
        dependencies = analysis["dependency_refs"]
        if not isinstance(dependencies, list):
            _bad("analysis.dependency_refs must be an array")

        credential = _object(root["database_dsn"], "database_dsn")
        _keys(credential, {"provider", "name"}, "database_dsn")
        if credential["provider"] != "systemd-credential":
            _bad("database DSN provider must be systemd-credential")

        return RecordingSubmissionOperatorConfig(
            ExactRecordingAnalysisSelection(
                recording_id=RecordingId(_string(root["recording_id"], "recording_id")),
                algorithm_ref=_artifact(analysis["algorithm_ref"], "algorithm_ref"),
                config_ref=_artifact(analysis["config_ref"], "config_ref"),
                dependency_refs=tuple(
                    _artifact(value, f"dependency_refs[{index}]")
                    for index, value in enumerate(dependencies)
                ),
                requested_output_schema=_schema(
                    analysis["requested_output_schema"], "requested_output_schema"
                ),
            ),
            _string(credential["name"], "database_dsn.name"),
        )
    except RecordingSubmissionOperatorError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RecordingSubmissionOperatorError(
            "recording submission configuration is invalid"
        ) from error


def _artifact(value: object, name: str) -> ArtifactRef:
    item = _object(value, name)
    _keys(item, {"artifact_id", "digest", "schema"}, name)
    return ArtifactRef(
        _string(item["artifact_id"], f"{name}.artifact_id"),
        _digest(item["digest"], f"{name}.digest"),
        None if item["schema"] is None else _schema(item["schema"], f"{name}.schema"),
    )


def _digest(value: object, name: str) -> Digest:
    item = _object(value, name)
    _keys(item, {"algorithm", "value"}, name)
    return Digest(
        DigestAlgorithm(_string(item["algorithm"], f"{name}.algorithm")),
        _string(item["value"], f"{name}.value"),
    )


def _schema(value: object, name: str) -> SchemaRef:
    item = _object(value, name)
    _keys(item, {"schema_id", "version"}, name)
    return SchemaRef(
        _string(item["schema_id"], f"{name}.schema_id"),
        SchemaVersion.parse(_string(item["version"], f"{name}.version")),
    )


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _bad(f"duplicate configuration key: {key}")
        result[key] = value
    return result


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _bad(f"{name} must be an object")
    return cast(dict[str, object], value)


def _keys(value: dict[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        _bad(f"{name} fields differ from the schema")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        _bad(f"{name} must be a non-empty string")
    return value


def _bad(message: str) -> NoReturn:
    raise RecordingSubmissionOperatorError(message)
