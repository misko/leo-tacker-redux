from __future__ import annotations

import io

from leo_station.adaptive_response_operator import main


class _Cycle:
    def __init__(self, values: list[bool]) -> None:
        self._values = values

    def run_once(self) -> bool:
        return self._values.pop(0)


def test_adaptive_operator_runs_one_bounded_cycle() -> None:
    stdout = io.StringIO()
    captured = {}

    def build(_credentials, **kwargs):
        captured.update(kwargs)
        return _Cycle([True])

    assert (
        main(
            [
                "--credential-directory",
                "/credentials",
                "--worker-id",
                "worker-a",
                "--lease-ttl-seconds",
                "3600",
                "--once",
            ],
            stdout=stdout,
            service_builder=build,
        )
        == 0
    )
    assert captured == {"worker_id": "worker-a", "lease_ttl_s": 3600.0}
    assert stdout.getvalue() == (
        '{"event": "adaptive_response_cycle_complete", "processed": true}\n'
    )


def test_adaptive_operator_sanitizes_failures() -> None:
    stderr = io.StringIO()

    def fail(_credentials, **_kwargs):
        raise RuntimeError("private failure")

    assert (
        main(
            ["--credential-directory", "/credentials", "--once"],
            stderr=stderr,
            service_builder=fail,
        )
        == 4
    )
    assert stderr.getvalue() == '{"event":"adaptive_response_cycle_failed"}\n'
