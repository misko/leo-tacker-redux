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
        "--host-cpu-cores 24",
        "--reserved-cpu-cores 8",
        "--minimum-memory-available-bytes 8589934592",
        "--maximum-optional-concurrency 1",
    )

    for path in units:
        unit = path.read_text(encoding="utf-8")
        for expected in common:
            assert expected in unit, f"{path} lacks {expected}"

    prompt = units[1].read_text(encoding="utf-8")
    assert "--maximum-focused-backlog 64" in prompt
    assert "--estimated-claim-cpu-cores 1" in prompt
    assert "--maximum-io-pressure-avg10 80" in prompt
    for path in (units[0], units[2], units[3]):
        unit = path.read_text(encoding="utf-8")
        assert "--maximum-focused-backlog 0" in unit
        assert "--estimated-claim-cpu-cores 3" in unit
        assert "--maximum-io-pressure-avg10 5" in unit
