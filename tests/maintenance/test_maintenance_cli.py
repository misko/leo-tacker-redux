from __future__ import annotations

from pathlib import Path

from leo_flow.maintenance.__main__ import main


def test_orphan_operational_failure_is_deterministic_and_sanitized(
    tmp_path: Path, capsys
) -> None:
    service_file = tmp_path / "pg_service.conf"
    service_file.write_text("[maintenance]\nhost=not-used\n")
    service_file.chmod(0o600)

    result = main(
        [
            "reconcile-orphans",
            "--blob-root",
            str(tmp_path / "missing-cas"),
            "--service",
            "maintenance",
            "--service-file",
            str(service_file),
        ]
    )

    captured = capsys.readouterr()
    assert result == 3
    assert captured.out == ""
    assert captured.err == '{"event":"maintenance_failed"}\n'
