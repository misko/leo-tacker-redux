from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from leo_flow.maintenance.capacity import (
    CapacityConfiguration,
    CapacityConfigurationError,
    CapacityRoot,
    CapacityThresholds,
    check_capacity,
    exit_code,
    load_configuration,
    main,
)


@dataclass(frozen=True)
class FakeStat:
    st_dev: int
    st_ino: int


@dataclass(frozen=True)
class FakeStatVfs:
    f_frsize: int
    f_blocks: int
    f_bavail: int


def thresholds() -> CapacityThresholds:
    return CapacityThresholds(
        warn_free_bytes=300,
        critical_free_bytes=100,
        warn_free_fraction=0.3,
        critical_free_fraction=0.1,
        warn_seconds_to_full=60,
        critical_seconds_to_full=20,
    )


def test_check_is_deterministic_and_uses_bytes_fraction_and_time() -> None:
    configuration = CapacityConfiguration(
        thresholds(),
        (
            CapacityRoot("cas", Path("/cas"), 2),
            CapacityRoot("spool", Path("/spool"), 3),
        ),
    )
    stats = {Path("/cas"): FakeStat(7, 70), Path("/spool"): FakeStat(7, 71)}

    report = check_capacity(
        configuration,
        now_utc_ns=lambda: 123,
        stat=stats.__getitem__,
        statvfs=lambda _path: FakeStatVfs(10, 100, 25),
        resolve=lambda path: path,
    )

    assert report["checked_at_utc_ns"] == 123
    assert report["overall_status"] == "warn"
    roots = report["roots"]
    assert isinstance(roots, list)
    assert [root["name"] for root in roots] == ["cas", "spool"]
    assert roots[0]["filesystem_free_bytes"] == 250
    assert roots[0]["filesystem_free_fraction"] == 0.25
    assert roots[0]["estimated_filesystem_bytes_per_second"] == 5.0
    assert roots[0]["seconds_to_full"] == 50.0
    assert roots[0]["reasons"] == [
        "free-bytes-warn",
        "free-fraction-warn",
        "time-to-full-warn",
    ]
    permuted = check_capacity(
        CapacityConfiguration(thresholds(), tuple(reversed(configuration.roots))),
        now_utc_ns=lambda: 123,
        stat=stats.__getitem__,
        statvfs=lambda _path: FakeStatVfs(10, 100, 25),
        resolve=lambda path: path,
    )
    assert json.dumps(report, sort_keys=True, separators=(",", ":")) == json.dumps(
        permuted, sort_keys=True, separators=(",", ":")
    )
    assert exit_code(report, "warn") == 2
    assert exit_code(report, "critical") == 0


def test_exact_inode_alias_does_not_double_count_rate() -> None:
    configuration = CapacityConfiguration(
        thresholds(),
        (
            CapacityRoot("alias", Path("/alias"), 9),
            CapacityRoot("spool", Path("/spool"), 4),
        ),
    )
    report = check_capacity(
        configuration,
        now_utc_ns=lambda: 1,
        stat=lambda _path: FakeStat(1, 2),
        statvfs=lambda _path: FakeStatVfs(10, 100, 50),
        resolve=lambda path: path,
    )
    roots = report["roots"]
    assert isinstance(roots, list)
    assert roots[0]["duplicate_of"] is None
    assert roots[1]["duplicate_of"] == "alias"
    assert roots[0]["estimated_filesystem_bytes_per_second"] == 9.0


def test_inaccessible_root_fails_closed_without_directory_scan() -> None:
    configuration = CapacityConfiguration(
        thresholds(), (CapacityRoot("missing", Path("/missing")),)
    )

    def fail(_path: Path) -> Path:
        raise FileNotFoundError

    report = check_capacity(
        configuration,
        now_utc_ns=lambda: 2,
        resolve=fail,
        stat=lambda _path: pytest.fail("stat should not run"),
        statvfs=lambda _path: pytest.fail("statvfs should not run"),
    )
    assert report["overall_status"] == "critical"
    assert report["roots"] == [
        {
            "configured_path": "/missing",
            "name": "missing",
            "reason": "inaccessible:FileNotFoundError",
            "status": "critical",
        }
    ]
    assert exit_code(report, "critical") == 3


def test_one_capacity_snapshot_is_used_for_shared_device() -> None:
    configuration = CapacityConfiguration(
        thresholds(),
        (CapacityRoot("a", Path("/a")), CapacityRoot("b", Path("/b"))),
    )
    calls: list[Path] = []

    def observe(path: Path) -> FakeStatVfs:
        calls.append(path)
        return FakeStatVfs(10, 100, 50)

    report = check_capacity(
        configuration,
        now_utc_ns=lambda: 3,
        stat=lambda path: FakeStat(1, 1 if path == Path("/a") else 2),
        statvfs=observe,
        resolve=lambda path: path,
    )
    assert len(calls) == 1
    assert report["roots"][0]["filesystem_free_bytes"] == 500
    assert report["roots"][1]["filesystem_free_bytes"] == 500


def test_closed_configuration_loads_and_rejects_unknown_fields(tmp_path: Path) -> None:
    document = {
        "schema_version": 1,
        "fail_on": "warn",
        "thresholds": {
            "warn_free_bytes": 300,
            "critical_free_bytes": 100,
            "warn_free_fraction": 0.3,
            "critical_free_fraction": 0.1,
        },
        "roots": [{"name": "spool", "path": "/var/spool/leo"}],
    }
    path = tmp_path / "capacity.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_configuration(path).roots[0].name == "spool"

    document["ambient_discovery"] = True
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CapacityConfigurationError, match="unknown"):
        load_configuration(path)


def test_threshold_ordering() -> None:
    with pytest.raises(CapacityConfigurationError, match="critical bytes"):
        CapacityThresholds(10, 11, 0.2, 0.1)


@pytest.mark.parametrize(
    (
        "status",
        "warn_bytes",
        "critical_bytes",
        "warn_fraction",
        "critical_fraction",
        "code",
    ),
    [
        ("healthy", 0, 0, 0.0, 0.0, 0),
        ("warn", 10**30, 0, 1.0, 0.0, 2),
        ("critical", 10**30, 10**30, 1.0, 1.0, 3),
    ],
)
def test_cli_emits_one_document_and_status_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status: str,
    warn_bytes: int,
    critical_bytes: int,
    warn_fraction: float,
    critical_fraction: float,
    code: int,
) -> None:
    config = tmp_path / f"{status}.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fail_on": "warn",
                "thresholds": {
                    "warn_free_bytes": warn_bytes,
                    "critical_free_bytes": critical_bytes,
                    "warn_free_fraction": warn_fraction,
                    "critical_free_fraction": critical_fraction,
                },
                "roots": [{"name": "test-root", "path": str(tmp_path)}],
            }
        ),
        encoding="utf-8",
    )
    assert main(["--config", str(config)]) == code
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["overall_status"] == status
    assert captured.err == ""


def test_cli_unreadable_config_is_one_bounded_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(tmp_path / "missing.json")]) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"event":"storage_capacity_configuration_failed"}\n'
