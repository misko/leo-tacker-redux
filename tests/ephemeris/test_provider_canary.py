from __future__ import annotations

import json
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest

from leo_flow.analysis.ephemeris.providers import HttpResponse, ProviderCredentials
from leo_flow.contracts.ephemeris import EphemerisSource
from leo_flow.deployments import ephemeris_provider_canary as canary
from leo_flow.storage.filesystem import FileSystemBlobStore

CONFIG = (
    Path(__file__).parents[2]
    / "deploy"
    / "ephemeris-provider-canary"
    / "huggingface-dry-run.example.json"
)
DEPLOY = CONFIG.parent


class Transport:
    def __init__(self, body: bytes, *, expects_credentials: bool = False) -> None:
        self.body = body
        self.expects_credentials = expects_credentials
        self.calls = 0

    def send(
        self,
        request: object,
        *,
        credentials: ProviderCredentials | None = None,
    ) -> HttpResponse:
        del request
        assert (credentials is not None) is self.expects_credentials
        self.calls += 1
        return HttpResponse(
            200,
            (
                ("Content-Type", "text/plain"),
                ("Content-Length", str(len(self.body))),
            ),
            (self.body,),
        )


class Credentials:
    def __init__(self) -> None:
        self.names: list[str] = []

    def resolve(self, name: str) -> str:
        self.names.append(name)
        return {
            "space-track-identity": "operator@example.invalid",
            "space-track-password": "never-archive-this-password",
        }[name]


class RefusingCredentials:
    def resolve(self, name: str) -> str:
        raise AssertionError(f"dry-run resolved {name}")


def _receipt(root: Path, outcome: canary.CanaryOutcome) -> dict[str, object]:
    blobs = FileSystemBlobStore(root / "cas")
    with blobs.open(outcome.receipt_ref) as stream:
        return dict(canary.verify_canary_receipt(stream.read()))


def test_example_is_offline_and_repeated_runs_are_content_idempotent(
    tmp_path: Path,
) -> None:
    config = canary.load_canary_config(CONFIG)
    assert config.source is EphemerisSource.HUGGING_FACE
    assert config.network_approved is False
    assert config.credential_capabilities is None

    first = canary.run_provider_canary(config, tmp_path)
    second = canary.run_provider_canary(config, tmp_path)
    third = canary.run_provider_canary(config, tmp_path / "independent-run")

    assert first == second
    assert first.receipt_ref == third.receipt_ref
    assert str(first.receipt_ref.digest) == (
        "sha256:779f7255d2f5cfe383c7865ec734b86b1cb24d157e94d2abc9e1ad9a5f568917"
    )
    assert first.mode == "fixture"
    assert first.live_retrieval_performed is False
    receipt = _receipt(tmp_path, first)
    assert receipt["verification"] == {
        "normalized_object_hash_size_and_parse": "verified",
        "provenance_hash_size_and_bindings": "verified",
        "raw_object_hash_and_size": "verified",
        "receipt_internal_digest": "verified-before-archive",
    }
    propagation = receipt["propagation"]
    assert isinstance(propagation, dict)
    assert propagation["status"] == "verified"
    assert propagation["input_path"] == (
        "archived-normalized-tle->catalog-exact-ref->pinned-sgp4"
    )
    assert propagation["norad_id"] == 12345
    assert propagation["state"]["error_code"] is None
    assert len(tuple((tmp_path / "cas" / "sha256").glob("*/*"))) == 4


def test_installed_timer_runs_hardened_fixture_path_until_explicit_override() -> None:
    service = (DEPLOY / "leo-ephemeris-provider-canary.service").read_text()
    timer = (DEPLOY / "leo-ephemeris-provider-canary.timer").read_text()
    network_override = (DEPLOY / "allow-network.conf.example").read_text()

    assert "--allow-network" not in service
    assert "RestrictAddressFamilies=AF_UNIX\n" in service
    assert "OnCalendar=*-*-* 00/6:00:00" in timer
    assert "Persistent=true" in timer
    assert "--allow-network" in network_override
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in network_override


def test_network_requires_both_reviewed_config_and_command_opt_in(
    tmp_path: Path,
) -> None:
    config = canary.load_canary_config(CONFIG)
    transport = Transport(canary._fixture_tle())

    with pytest.raises(canary.ProviderCanaryError, match="both reviewed"):
        canary.run_provider_canary(
            config,
            tmp_path,
            allow_network=True,
            transport=transport,
        )

    assert transport.calls == 0


def test_approved_injected_boundary_is_one_request_and_rate_limited(
    tmp_path: Path,
) -> None:
    config = replace(canary.load_canary_config(CONFIG), network_approved=True)
    transport = Transport(canary._fixture_tle())
    epoch = canary.parse_tle_catalog(canary._fixture_tle())[0].epoch_utc_ns
    values = iter((int(epoch), int(epoch), int(epoch), int(epoch) + 1))

    outcome = canary.run_provider_canary(
        config,
        tmp_path,
        allow_network=True,
        transport=transport,
        now_utc_ns=lambda: next(values),
    )

    assert outcome.mode == "network"
    assert outcome.live_retrieval_performed is False
    assert transport.calls == 1
    with pytest.raises(canary.ProviderCanaryError, match="interval"):
        canary.run_provider_canary(
            config,
            tmp_path,
            allow_network=True,
            transport=transport,
            now_utc_ns=lambda: int(epoch),
        )
    assert transport.calls == 1


def test_space_track_resolves_only_named_capabilities_and_archives_no_values(
    tmp_path: Path,
) -> None:
    config = replace(
        canary.load_canary_config(CONFIG),
        source=EphemerisSource.SPACE_TRACK,
        endpoint_profile="space-track-starlink-gp-v1",
        network_approved=True,
        credential_capabilities=canary.CredentialCapabilityNames(
            "systemd-credential",
            "space-track-identity",
            "space-track-password",
        ),
    )
    credentials = Credentials()
    transport = Transport(canary._fixture_tle(), expects_credentials=True)
    epoch = canary.parse_tle_catalog(canary._fixture_tle())[0].epoch_utc_ns
    values = iter((int(epoch), int(epoch), int(epoch), int(epoch) + 1))

    outcome = canary.run_provider_canary(
        config,
        tmp_path,
        allow_network=True,
        credential_provider=credentials,
        transport=transport,
        now_utc_ns=lambda: next(values),
    )
    receipt = _receipt(tmp_path, outcome)
    receipt_text = json.dumps(receipt)

    assert credentials.names == ["space-track-identity", "space-track-password"]
    assert receipt["credential_capability_names"] == [
        "space-track-identity",
        "space-track-password",
    ]
    assert receipt["credential_values_archived"] is False
    assert "operator@example.invalid" not in receipt_text
    assert "never-archive-this-password" not in receipt_text


def test_space_track_dry_run_does_not_resolve_configured_capabilities(
    tmp_path: Path,
) -> None:
    config = canary.load_canary_config(
        CONFIG.with_name("space-track-dry-run.example.json")
    )

    outcome = canary.run_provider_canary(
        config,
        tmp_path,
        credential_provider=RefusingCredentials(),
    )

    assert outcome.mode == "fixture"
    assert outcome.live_retrieval_performed is False


def test_cli_defaults_to_fixture_and_reports_sanitized_failure(
    tmp_path: Path,
) -> None:
    stdout = StringIO()
    stderr = StringIO()
    assert (
        canary.main(
            ["--config", str(CONFIG), "--root", str(tmp_path)],
            stdout=stdout,
            stderr=stderr,
        )
        == 0
    )
    output = json.loads(stdout.getvalue())
    assert output["status"] == "pass"
    assert output["mode"] == "fixture"
    assert output["live_retrieval_performed"] is False

    assert (
        canary.main(
            [
                "--config",
                str(CONFIG),
                "--root",
                str(tmp_path / "live"),
                "--allow-network",
            ],
            stdout=stdout,
            stderr=stderr,
        )
        == 1
    )
    failure = stderr.getvalue()
    assert json.loads(failure) == {
        "detail": "ProviderCanaryError",
        "event": "ephemeris_provider_canary",
        "status": "failed",
    }
    assert "Hugging Face" not in failure
