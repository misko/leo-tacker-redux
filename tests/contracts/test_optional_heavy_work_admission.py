from __future__ import annotations

import pytest

from leo_flow.contracts.optional_heavy_work_admission import (
    FocusedCaptureGuardV0_1,
    decode_focused_capture_guard_v0_1,
    encode_focused_capture_guard_v0_1,
)


def test_guard_codec_is_canonical_and_rejects_extra_fields() -> None:
    guard = FocusedCaptureGuardV0_1(10, 100, 20, 80, 2, 1, True)
    payload = encode_focused_capture_guard_v0_1(guard)
    assert decode_focused_capture_guard_v0_1(payload) == guard
    with pytest.raises(ValueError, match="fields"):
        decode_focused_capture_guard_v0_1(
            payload.replace(b'"schema":', b'"extra":0,"schema":')
        )


def test_guard_rejects_reversed_validity() -> None:
    with pytest.raises(ValueError, match="validity"):
        FocusedCaptureGuardV0_1(100, 99, 100, 101, 0, 0, False)
