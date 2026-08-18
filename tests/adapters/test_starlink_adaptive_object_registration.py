from __future__ import annotations

from collections.abc import Callable
from typing import Any, Self

import pytest

from leo_flow.adapters.starlink_adaptive_qam_postgres import (
    _cataloged as cataloged_qam,
)
from leo_flow.adapters.starlink_adaptive_qam_postgres import (
    _register_live_object as register_qam_object,
)
from leo_flow.adapters.starlink_adaptive_response_postgres import (
    PostgresAdaptiveResponseWorkRepositoryV0_1,
)
from leo_flow.adapters.starlink_adaptive_response_postgres import (
    _cataloged as cataloged_response,
)
from leo_flow.adapters.starlink_adaptive_response_postgres import (
    _register_live_object as register_response_object,
)
from leo_flow.adapters.starlink_pilot_refinement_postgres import (
    _cataloged as cataloged_refinement,
)
from leo_flow.adapters.starlink_pilot_refinement_postgres import (
    _register_live_object as register_refinement_object,
)
from leo_flow.analysis.recording.starlink_adaptive_qam_persistence import (
    StarlinkAdaptiveQamConflictError,
)
from leo_flow.analysis.recording.starlink_adaptive_response_persistence import (
    StarlinkAdaptiveResponseConflictError,
)
from leo_flow.analysis.recording.starlink_pilot_refinement_persistence import (
    StarlinkPilotRefinementConflictError,
)
from leo_flow.contracts.core import Digest, RecordingId
from leo_flow.contracts.storage import ObjectRef


class _Cursor:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, values: tuple[object, ...]) -> _Cursor:
        self.calls.append((query, values))
        return self

    def fetchone(self) -> dict[str, object] | None:
        return self.row

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self, **_kwargs: object) -> _Cursor:
        return self._cursor

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _object() -> ObjectRef:
    return ObjectRef(
        Digest.sha256(b"adaptive-product"),
        321,
        "application/json",
        "adaptive-product-v0.1",
        "cas:sha256:adaptive-product",
    )


@pytest.mark.parametrize(
    "register",
    (register_response_object, register_qam_object, register_refinement_object),
)
def test_adaptive_catalog_registers_and_verifies_its_cas_object(
    register: Callable[[Any, ObjectRef], None],
) -> None:
    ref = _object()
    cursor = _Cursor(
        {
            "byte_count": ref.byte_count,
            "media_type": ref.media_type,
            "format_id": ref.format_id,
            "locator": ref.locator,
        }
    )

    register(cursor, ref)

    assert len(cursor.calls) == 2
    assert "register_live_object_blob" in cursor.calls[0][0]
    assert cursor.calls[0][1] == (
        ref.digest.algorithm.value,
        ref.digest.value,
        ref.byte_count,
        ref.media_type,
        ref.format_id,
        ref.locator,
    )
    assert "lifecycle_state='live'" in cursor.calls[1][0]


def test_adaptive_reanalysis_uses_exact_recording_and_prior_result_cas() -> None:
    cursor = _Cursor({"changed": True})
    repository = PostgresAdaptiveResponseWorkRepositoryV0_1(
        lambda: _Connection(cursor)  # type: ignore[arg-type]
    )

    assert repository.requeue_completed(
        RecordingId("rec_reanalysis"),
        "slar_" + "1" * 32,
        "analysis-plan-cadence-v2",
    )
    assert cursor.calls == [
        (
            "SELECT public.requeue_starlink_adaptive_response_work_v0_1(%s,%s,%s) AS changed",
            (
                "rec_reanalysis",
                "slar_" + "1" * 32,
                "analysis-plan-cadence-v2",
            ),
        )
    ]


@pytest.mark.parametrize(
    ("register", "error"),
    (
        (register_response_object, StarlinkAdaptiveResponseConflictError),
        (register_qam_object, StarlinkAdaptiveQamConflictError),
        (register_refinement_object, StarlinkPilotRefinementConflictError),
    ),
)
def test_adaptive_catalog_rejects_conflicting_object_metadata(
    register: Callable[[Any, ObjectRef], None],
    error: type[Exception],
) -> None:
    cursor = _Cursor(
        {
            "byte_count": 999,
            "media_type": "application/json",
            "format_id": "adaptive-product-v0.1",
            "locator": "cas:sha256:adaptive-product",
        }
    )

    with pytest.raises(error):
        register(cursor, _object())


def _catalog_row() -> dict[str, object]:
    return {
        "analysis_id": "slar_" + "1" * 32,
        "recording_id": "rec_adaptive_catalog",
        "input_recording_digest_value": Digest.sha256(b"recording").value,
        "timeline_analysis_id": "fdtl_" + "2" * 32,
        "timeline_bundle_digest_value": Digest.sha256(b"timeline").value,
        "source_suite_analysis_id": "slsuite_" + "3" * 32,
        "source_suite_bundle_digest_value": Digest.sha256(b"suite").value,
        "source_adaptive_response_analysis_id": "slar_" + "4" * 32,
        "source_adaptive_response_bundle_digest_value": Digest.sha256(
            b"adaptive"
        ).value,
        "request_digest_value": Digest.sha256(b"request").value,
        "stream_count": 1,
        "window_count": 1,
        "point_count": 8,
        "bundle_digest_algorithm": "sha256",
        "bundle_digest_value": Digest.sha256(b"bundle").value,
        "bundle_byte_count": 123,
        "bundle_media_type": "application/json",
        "bundle_format_id": "adaptive-product-v0.1",
        "bundle_locator": "cas:sha256:bundle",
    }


def test_response_catalog_restores_fixed_source_schema_identities() -> None:
    value = cataloged_response(_catalog_row()).projection

    assert value.timeline_ref.schema is not None
    assert (
        value.timeline_ref.schema.schema_id == "org.leo-flow.full-dwell-timeline-bundle"
    )
    assert value.source_suite_ref.schema is not None
    assert (
        value.source_suite_ref.schema.schema_id
        == "org.leo-flow.starlink-detector-suite-recording-bundle"
    )


def test_qam_catalog_restores_fixed_source_schema_identities() -> None:
    row = _catalog_row()
    row["analysis_id"] = "slaqam4_" + "5" * 32
    row["point_count"] = 2400

    value = cataloged_qam(row).projection

    assert value.source_adaptive_response_ref.schema is not None
    assert (
        value.source_adaptive_response_ref.schema.schema_id
        == "org.leo-flow.starlink-adaptive-response-bundle"
    )
    assert value.source_suite_ref.schema is not None
    assert (
        value.source_suite_ref.schema.schema_id
        == "org.leo-flow.starlink-detector-suite-recording-bundle"
    )


def test_refinement_catalog_restores_fixed_source_schema_identities() -> None:
    row = _catalog_row()
    row["analysis_id"] = "slpr_" + "6" * 32
    row["recording_identity_digest_value"] = row.pop("input_recording_digest_value")
    row["source_prescreen_analysis_id"] = "slps_" + "7" * 32
    row["source_prescreen_bundle_digest_value"] = Digest.sha256(b"prescreen").value
    row["seed_count"] = 1

    value = cataloged_refinement(row).projection

    assert value.source_prescreen_ref.schema is not None
    assert (
        value.source_prescreen_ref.schema.schema_id
        == "org.leo-flow.starlink-pilot-prescreen-bundle"
    )
    assert value.source_suite_ref.schema is not None
    assert (
        value.source_suite_ref.schema.schema_id
        == "org.leo-flow.starlink-detector-suite-recording-bundle"
    )
