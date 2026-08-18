from pathlib import Path


def test_every_optional_heavy_worker_shares_one_capture_aware_slot() -> None:
    units = (
        Path(
            "deploy/gauss-adaptive-response-v1/"
            "leo-gauss-adaptive-response.service.in"
        ),
        Path(
            "deploy/gauss-prompt-full-dwell-v1/"
            "leo-gauss-prompt-full-dwell.service.in"
        ),
        Path("deploy/gauss-full-dwell-v1/leo-gauss-full-dwell.service.in"),
        Path(
            "deploy/gauss-symbolwise-replay-v1/"
            "leo-gauss-symbolwise-replay.service.in"
        ),
    )
    common = (
        "--capture-guard-status %t/leo-flow-optional-heavy/guard.json",
        "--maximum-focused-backlog 0",
        "--host-cpu-cores 24",
        "--reserved-cpu-cores 8",
        "--estimated-claim-cpu-cores 3",
        "--minimum-memory-available-bytes 8589934592",
        "--maximum-io-pressure-avg10 5",
        "--maximum-optional-concurrency 1",
    )

    for path in units:
        unit = path.read_text(encoding="utf-8")
        for expected in common:
            assert expected in unit, f"{path} lacks {expected}"
