from __future__ import annotations

import hashlib
import hmac
import json

from leo_flow.adapters.radio_lifecycle_authenticated import (
    AuthenticatedRadioLifecycleObserverV0_1,
)
from leo_flow.contracts.core import RadioId, UtcNs, canonical_json_bytes
from leo_flow.contracts.radio_lifecycle import (
    RadioLifecycleObservationStatus,
    RadioLifecycleObserverUnavailableReason,
)

RADIO = RadioId("radio_auth_test")
KEY = b"k" * 32
BOOT = "41974bfd-7aa8-4d28-b1c8-57d21c3e05bb"


class _Transport:
    def __init__(self, *, key: bytes = KEY, mutate=None) -> None:
        self.key = key
        self.mutate = mutate

    def exchange(self, request: bytes, *, deadline_utc_ns: UtcNs) -> bytes:
        document = json.loads(request)
        assert int(deadline_utc_ns) == 9_000_000_000
        payload = {
            "protocol": "leo-radio-lifecycle-diagnostic-v0.1",
            "radio_id": document["radio_id"],
            "nonce": document["nonce"],
            "observed_utc_ns": 8_000_000_000,
            "boot_id": BOOT,
            "uptime_ns": 3_000_000_000,
            "boot_time_uncertainty_ns": 1_000_000,
            "iiod": {
                "pid": 41,
                "proc_start_ticks": 1234,
                "clock_ticks_per_second": 100,
            },
            "ad9361": {"initialization_epoch": 9, "reset_reason": "cold_boot"},
        }
        if self.mutate is not None:
            self.mutate(payload)
        encoded = canonical_json_bytes(payload)
        envelope = {
            "payload": payload,
            "mac_sha256": hmac.new(self.key, encoded, hashlib.sha256).hexdigest(),
        }
        return canonical_json_bytes(envelope)


def _observer(transport: _Transport):
    return AuthenticatedRadioLifecycleObserverV0_1(
        transport,
        {RADIO: KEY},
        utc_now_ns=lambda: 7_000_000_000,
        nonce_bytes=lambda count: b"n" * count,
    )


def test_verified_bounded_response_becomes_authenticated_observation() -> None:
    value = _observer(_Transport()).observe(RADIO, deadline_utc_ns=UtcNs(9_000_000_000))
    assert value.status is RadioLifecycleObservationStatus.AVAILABLE
    assert value.boot_id == BOOT
    assert value.iiod is not None and value.iiod.proc_start_ticks == 1234


def test_bad_mac_or_nonce_never_exposes_a_boot_identity() -> None:
    bad_mac = _observer(_Transport(key=b"x" * 32)).observe(
        RADIO, deadline_utc_ns=UtcNs(9_000_000_000)
    )
    assert bad_mac.status is RadioLifecycleObservationStatus.UNAVAILABLE
    assert bad_mac.boot_id is None
    assert (
        bad_mac.unavailable_reason
        is RadioLifecycleObserverUnavailableReason.AUTHENTICATION_FAILED
    )

    bad_nonce = _observer(
        _Transport(mutate=lambda payload: payload.__setitem__("nonce", "replayed"))
    ).observe(RADIO, deadline_utc_ns=UtcNs(9_000_000_000))
    assert bad_nonce.status is RadioLifecycleObservationStatus.UNAVAILABLE
    assert bad_nonce.boot_id is None


def test_oversized_response_and_unknown_radio_are_sanitized() -> None:
    class Oversized:
        def exchange(self, request: bytes, *, deadline_utc_ns: UtcNs) -> bytes:
            return b"x" * 2049

    oversized = _observer(Oversized()).observe(  # type: ignore[arg-type]
        RADIO, deadline_utc_ns=UtcNs(9_000_000_000)
    )
    assert (
        oversized.unavailable_reason
        is RadioLifecycleObserverUnavailableReason.INVALID_RESPONSE
    )
    unknown = _observer(_Transport()).observe(
        RadioId("radio_unknown"), deadline_utc_ns=UtcNs(9_000_000_000)
    )
    assert (
        unknown.unavailable_reason
        is RadioLifecycleObserverUnavailableReason.UNSUPPORTED
    )
