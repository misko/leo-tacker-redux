from __future__ import annotations

import pytest

from leo_flow.services import (
    AnalysisServiceConfig,
    CaptureServiceConfig,
    ConfigurationError,
    DashboardServiceConfig,
    parse_service_config,
)


def runtime() -> dict[str, object]:
    return {
        "instance_id": "station-a-capture",
        "poll_interval_s": 0.25,
        "shutdown_timeout_s": 3,
        "secret_refs": [{"provider": "environment", "name": "POSTGRES_DSN"}],
    }


def test_each_process_has_a_strict_versioned_configuration() -> None:
    capture = parse_service_config(
        {
            "schema_version": 1,
            "process": "capture",
            "runtime": runtime(),
            "adapters": {
                "plan_source_ref": "plans.production",
                "radio_ref": "radio.pluto-v5",
                "preflight_ref": "preflight.v5",
                "recording_writer_ref": "sigmf.local",
                "spool_ref": "sqlite.local",
                "recording_publisher_ref": "cas.postgres",
            },
        }
    )
    assert isinstance(capture, CaptureServiceConfig)
    assert capture.runtime.secret_refs[0].name == "POSTGRES_DSN"

    analysis = parse_service_config(
        {
            "schema_version": 1,
            "process": "analysis",
            "runtime": runtime(),
            "adapters": {
                "job_repository_ref": "jobs.postgres",
                "recording_reader_ref": "recordings.cas",
                "feature_publisher_ref": "features.postgres",
                "model_publisher_ref": "models.postgres",
            },
        }
    )
    assert isinstance(analysis, AnalysisServiceConfig)

    dashboard = parse_service_config(
        {
            "schema_version": 1,
            "process": "dashboard",
            "runtime": runtime(),
            "adapters": {
                "query_projection_ref": "dashboard.postgres-ro",
                "server_ref": "http.stdlib",
                "bind_host": "127.0.0.1",
                "bind_port": 8080,
            },
        }
    )
    assert isinstance(dashboard, DashboardServiceConfig)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda value: value.update(schema_version=2), "schema_version"),
        (lambda value: value.update(password="plaintext"), "unknown=.*password"),
        (lambda value: value["runtime"].update(extra=True), "unknown=.*extra"),
        (
            lambda value: value["runtime"]["secret_refs"][0].update(value="secret"),
            "unknown=.*value",
        ),
        (
            lambda value: value["adapters"].update(legacy_path="/nfs/control"),
            "unknown=.*legacy_path",
        ),
    ],
)
def test_unknown_keys_versions_and_inline_secrets_fail_closed(
    mutation, match: str
) -> None:
    value = {
        "schema_version": 1,
        "process": "dashboard",
        "runtime": runtime(),
        "adapters": {
            "query_projection_ref": "dashboard.postgres-ro",
            "server_ref": "http.stdlib",
            "bind_host": "127.0.0.1",
            "bind_port": 8080,
        },
    }
    mutation(value)
    with pytest.raises(ConfigurationError, match=match):
        parse_service_config(value)


def test_invalid_intervals_and_bind_address_are_rejected() -> None:
    value = {
        "schema_version": 1,
        "process": "dashboard",
        "runtime": {**runtime(), "poll_interval_s": 0},
        "adapters": {
            "query_projection_ref": "dashboard.postgres-ro",
            "server_ref": "http.stdlib",
            "bind_host": "127.0.0.1",
            "bind_port": 8080,
        },
    }
    with pytest.raises(ConfigurationError, match="positive"):
        parse_service_config(value)
    value["runtime"] = runtime()
    value["adapters"]["bind_port"] = 0
    with pytest.raises(ConfigurationError, match="bind"):
        parse_service_config(value)
