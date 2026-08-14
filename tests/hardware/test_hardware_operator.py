from __future__ import annotations

import json
from pathlib import Path

import pytest

from leo_flow.contracts.core import canonical_json_bytes
from leo_flow.contracts.hardware import HardwareMetadataSnapshotRef
from leo_flow.hardware.__main__ import main
from leo_flow.hardware.codec import encode_hardware_snapshot
from leo_flow.hardware.operator import (
    HardwareOperatorError,
    HardwareRepository,
    create_bundle,
    hardware_publication_key,
    load_operator_config,
    publish_bundle,
    validate_bundle,
)

EXAMPLE = (
    Path(__file__).parents[2]
    / "deploy"
    / "hardware-metadata"
    / "operator-config.example.json"
)


class _Repository(HardwareRepository):
    def __init__(self) -> None:
        self.snapshot = None
        self.key: str | None = None
        self.calls = 0

    def publish(self, snapshot, *, idempotency_key):
        self.calls += 1
        if self.snapshot is not None and (
            self.snapshot != snapshot or self.key != idempotency_key
        ):
            raise AssertionError("non-idempotent publication")
        self.snapshot = snapshot
        self.key = idempotency_key
        payload = encode_hardware_snapshot(snapshot)
        _, identity = _validate_bytes(payload, snapshot.snapshot_id)
        return identity

    def get(self, ref):
        if self.snapshot is None:
            raise AssertionError("snapshot was not published")
        return self.snapshot


def _validate_bytes(payload, snapshot_id):
    from leo_flow.contracts.core import Digest

    ref = HardwareMetadataSnapshotRef(snapshot_id, Digest.sha256(payload))
    return payload, ref


def test_example_creates_exact_deterministic_idempotent_bundle(tmp_path: Path) -> None:
    config = load_operator_config(EXAMPLE)
    bundle = tmp_path / "hardware.json"

    first = create_bundle(config, bundle)
    second = create_bundle(config, bundle)
    snapshot, validated = validate_bundle(bundle, expected=config.snapshot)

    assert first == second == validated
    assert bundle.read_bytes() == encode_hardware_snapshot(snapshot)
    assert first.ref.snapshot_id == config.snapshot.snapshot_id
    assert hardware_publication_key(first.ref).endswith(str(first.ref.digest))


def test_create_refuses_to_overwrite_different_bytes(tmp_path: Path) -> None:
    config = load_operator_config(EXAMPLE)
    bundle = tmp_path / "hardware.json"
    bundle.write_bytes(b"different")

    with pytest.raises(HardwareOperatorError, match="different bytes"):
        create_bundle(config, bundle)


def test_config_rejects_unknown_fields_duplicate_keys_and_raw_dsn(
    tmp_path: Path,
) -> None:
    document = json.loads(EXAMPLE.read_bytes())
    document["secret"] = "postgresql://user:password@database/catalog"
    invalid = tmp_path / "unknown.json"
    invalid.write_bytes(canonical_json_bytes(document))
    with pytest.raises(HardwareOperatorError, match="fields"):
        load_operator_config(invalid)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema":"x","schema":"secret"}')
    with pytest.raises(HardwareOperatorError, match="duplicate"):
        load_operator_config(duplicate)

    document = json.loads(EXAMPLE.read_bytes())
    document["publication"]["database_dsn"] = {
        "provider": "systemd-credential",
        "name": "credential",
        "value": "postgresql://secret",
    }
    raw = tmp_path / "raw.json"
    raw.write_bytes(canonical_json_bytes(document))
    with pytest.raises(HardwareOperatorError, match="fields"):
        load_operator_config(raw)


def test_validate_rejects_noncanonical_or_symlinked_bundle(tmp_path: Path) -> None:
    config = load_operator_config(EXAMPLE)
    bundle = tmp_path / "hardware.json"
    bundle.write_bytes(b" " + encode_hardware_snapshot(config.snapshot))
    with pytest.raises(HardwareOperatorError, match="invalid"):
        validate_bundle(bundle)

    target = tmp_path / "target.json"
    target.write_bytes(encode_hardware_snapshot(config.snapshot))
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(HardwareOperatorError, match="cannot be read"):
        validate_bundle(link)


def test_dry_run_performs_no_repository_or_cas_io(tmp_path: Path) -> None:
    document = json.loads(EXAMPLE.read_bytes())
    document["publication"]["cas_root"] = str(tmp_path / "must-not-exist")
    config_path = tmp_path / "operator.json"
    config_path.write_bytes(canonical_json_bytes(document))
    config = load_operator_config(config_path)
    bundle = tmp_path / "hardware.json"
    identity = create_bundle(config, bundle)

    assert publish_bundle(config, bundle, dry_run=True) == identity
    assert not config.publication.cas_root.exists()


def test_publish_uses_content_bound_key_and_exact_readback(tmp_path: Path) -> None:
    config = load_operator_config(EXAMPLE)
    bundle = tmp_path / "hardware.json"
    identity = create_bundle(config, bundle)
    repository = _Repository()

    assert (
        publish_bundle(config, bundle, dry_run=False, repository=repository) == identity
    )
    assert (
        publish_bundle(config, bundle, dry_run=False, repository=repository) == identity
    )
    assert repository.calls == 2
    assert repository.key == hardware_publication_key(identity.ref)


def test_cli_reports_exact_identity_and_sanitizes_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = tmp_path / "hardware.json"
    assert main(["create", "--config", str(EXAMPLE), "--output", str(bundle)]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["event"] == "hardware_bundle_created"
    assert created["snapshot_id"] == "hw_example_do_not_publish"
    assert created["digest_algorithm"] == "sha256"
    assert len(created["digest_value"]) == 64

    broken = tmp_path / "postgresql-password-secret.json"
    broken.write_text("not json")
    assert main(["validate", "--bundle", str(broken)]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"event":"hardware_operator_failed"}\n'
    assert "secret" not in captured.err
