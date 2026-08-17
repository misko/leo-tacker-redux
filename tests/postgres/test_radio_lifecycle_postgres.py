from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from leo_flow.adapters.radio_lifecycle_postgres import (
    PostgresRadioLifecycleRepositoryV0_1,
)
from leo_flow.capture.radio_lifecycle import build_attempt_lifecycle_fact
from leo_flow.contracts.core import (
    V0_1,
    CaptureAttemptId,
    CaptureBatchId,
    RadioId,
    SchemaRef,
    UtcNs,
)
from leo_flow.contracts.radio_lifecycle import (
    CaptureAttemptLifecycleFactV0_1,
    IiodProcessIdentityV0_1,
    RadioLifecycleObservationSource,
    RadioLifecycleObservationStatus,
    RadioLifecycleObservationV0_1,
    RadioLifecycleReason,
    RadioLifecycleTrust,
    RadioTransportOutcome,
)

RADIO = RadioId("radio_pg_lifecycle")


def _observation(observed: int, boot: str, uptime: int):
    return RadioLifecycleObservationV0_1(
        SchemaRef(RadioLifecycleObservationV0_1.SCHEMA_ID, V0_1),
        RADIO,
        UtcNs(observed),
        RadioLifecycleObservationStatus.AVAILABLE,
        RadioLifecycleObservationSource.AUTHENTICATED_DIAGNOSTIC_V1,
        RadioLifecycleTrust.RADIO_AUTHENTICATED,
        boot,
        uptime,
        UtcNs(observed - uptime),
        1,
        IiodProcessIdentityV0_1(7, 11, 100),
    )


def _fact(boot: str = "d6f89d3a-6856-441f-83db-96c71728e15b"):
    return build_attempt_lifecycle_fact(
        schema=SchemaRef(CaptureAttemptLifecycleFactV0_1.SCHEMA_ID, V0_1),
        batch_id=CaptureBatchId("cbatch_pg_lifecycle"),
        attempt_id=CaptureAttemptId("cattempt_pg_lifecycle"),
        radio_id=RADIO,
        preflight=_observation(10, "41974bfd-7aa8-4d28-b1c8-57d21c3e05bb", 9),
        terminal=_observation(20, boot, 1 if boot.startswith("d6") else 19),
        transport_outcome=RadioTransportOutcome.DISCONNECTED,
    )


def _repository(postgres_dsn: str, role: str):
    def connect():
        connection = psycopg.connect(postgres_dsn, row_factory=dict_row)
        connection.execute(f"SET ROLE {role}")
        return connection

    return PostgresRadioLifecycleRepositoryV0_1(connect)


@pytest.mark.integration
def test_attempt_fact_exact_replay_history_and_dashboard_read(
    postgres_dsn: str,
) -> None:
    capture = _repository(postgres_dsn, "leo_capture")
    dashboard = _repository(postgres_dsn, "leo_dashboard")
    fact = _fact()
    assert capture.record_attempt(fact) == fact
    assert capture.record_attempt(fact) == fact
    assert capture.latest_terminal(RADIO) == (fact.attempt_id, fact.terminal)
    view = dashboard.capture_attempt_radio_lifecycle(fact.attempt_id)
    assert view.reason is RadioLifecycleReason.RADIO_REBOOTED
    assert view.evidence_codes == ("boot_id_changed",)


@pytest.mark.integration
def test_conflicting_replay_is_rejected_and_tables_are_private(
    postgres_dsn: str,
) -> None:
    capture = _repository(postgres_dsn, "leo_capture")
    capture.record_attempt(_fact())
    with pytest.raises(psycopg.errors.UniqueViolation):
        capture.record_attempt(_fact("41974bfd-7aa8-4d28-b1c8-57d21c3e05bb"))
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute(
            "SELECT has_table_privilege('leo_capture',"
            "'capture_attempt_radio_lifecycle_fact','SELECT'),"
            "has_table_privilege('leo_dashboard',"
            "'capture_attempt_radio_lifecycle_fact','SELECT')"
        ).fetchone() == (False, False)
