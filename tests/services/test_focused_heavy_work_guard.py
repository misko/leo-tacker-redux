from __future__ import annotations

from leo_flow.deployments.gauss_focused_continuous_operator import _publish_guard


class _UnavailablePublisher:
    def publish(self, _snapshot: object) -> None:
        raise OSError("optional status unavailable")


class _Journal:
    def incomplete(self) -> tuple[()]:
        return ()


def test_optional_guard_publication_failure_never_blocks_capture() -> None:
    _publish_guard(  # type: ignore[arg-type]
        _UnavailablePublisher(),
        _Journal(),  # type: ignore[arg-type]
        {},
        active=True,
        guard_from_utc_ns=10,
        guard_until_utc_ns=20,
    )
