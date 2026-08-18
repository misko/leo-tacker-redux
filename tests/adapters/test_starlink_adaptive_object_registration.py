from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from leo_flow.adapters.starlink_adaptive_qam_postgres import (
    _register_live_object as register_qam_object,
)
from leo_flow.adapters.starlink_adaptive_response_postgres import (
    _register_live_object as register_response_object,
)
from leo_flow.analysis.recording.starlink_adaptive_qam_persistence import (
    StarlinkAdaptiveQamConflictError,
)
from leo_flow.analysis.recording.starlink_adaptive_response_persistence import (
    StarlinkAdaptiveResponseConflictError,
)
from leo_flow.contracts.core import Digest
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
    (register_response_object, register_qam_object),
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


@pytest.mark.parametrize(
    ("register", "error"),
    (
        (register_response_object, StarlinkAdaptiveResponseConflictError),
        (register_qam_object, StarlinkAdaptiveQamConflictError),
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
