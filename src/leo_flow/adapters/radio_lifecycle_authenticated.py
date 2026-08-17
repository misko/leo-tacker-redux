"""Authenticated, bounded radio lifecycle diagnostic adapter.

The wire transport is injected.  This module owns request nonces, canonical
payload authentication, replay rejection, strict bounds, and sanitization; it
does not own SSH, paths, or capture orchestration.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from typing import Protocol

from leo_flow.contracts.core import (
    V0_1,
    RadioId,
    SchemaRef,
    UtcNs,
    canonical_json_bytes,
)
from leo_flow.contracts.radio_lifecycle import (
    Ad9361LifecycleIdentityV0_1,
    IiodProcessIdentityV0_1,
    RadioLifecycleObservationSource,
    RadioLifecycleObservationStatus,
    RadioLifecycleObservationV0_1,
    RadioLifecycleObserverUnavailableReason,
    RadioLifecycleTrust,
)

_MAX_RESPONSE_BYTES = 2_048


class AuthenticatedLifecycleTransportV0_1(Protocol):
    def exchange(self, request: bytes, *, deadline_utc_ns: UtcNs) -> bytes: ...


class AuthenticatedRadioLifecycleObserverV0_1:
    """Verify a firmware diagnostic response before exposing any identity."""

    def __init__(
        self,
        transport: AuthenticatedLifecycleTransportV0_1,
        device_keys: Mapping[RadioId, bytes],
        *,
        utc_now_ns: Callable[[], int],
        nonce_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._transport = transport
        self._keys = dict(device_keys)
        if not self._keys or any(len(key) < 32 for key in self._keys.values()):
            raise ValueError(
                "each enrolled lifecycle device key must be at least 32 bytes"
            )
        self._utc_now_ns = utc_now_ns
        self._nonce_bytes = nonce_bytes
        self._used_nonces: set[str] = set()

    def observe(
        self, radio_id: RadioId, *, deadline_utc_ns: UtcNs
    ) -> RadioLifecycleObservationV0_1:
        observed = UtcNs(max(0, self._utc_now_ns()))
        if observed > deadline_utc_ns:
            return self._unavailable(
                radio_id,
                observed,
                RadioLifecycleObserverUnavailableReason.DEADLINE_EXCEEDED,
            )
        key = self._keys.get(radio_id)
        if key is None:
            return self._unavailable(
                radio_id, observed, RadioLifecycleObserverUnavailableReason.UNSUPPORTED
            )
        nonce = self._nonce_bytes(24).hex()
        if nonce in self._used_nonces:
            return self._unavailable(
                radio_id,
                observed,
                RadioLifecycleObserverUnavailableReason.OBSERVER_ERROR,
            )
        self._used_nonces.add(nonce)
        request = canonical_json_bytes(
            {
                "protocol": "leo-radio-lifecycle-diagnostic-v0.1",
                "radio_id": str(radio_id),
                "nonce": nonce,
                "deadline_utc_ns": int(deadline_utc_ns),
            }
        )
        try:
            response = self._transport.exchange(
                request, deadline_utc_ns=deadline_utc_ns
            )
        except TimeoutError:
            return self._unavailable(
                radio_id,
                UtcNs(max(0, self._utc_now_ns())),
                RadioLifecycleObserverUnavailableReason.DEADLINE_EXCEEDED,
            )
        except Exception:  # noqa: BLE001 - sanitized adapter boundary
            return self._unavailable(
                radio_id,
                UtcNs(max(0, self._utc_now_ns())),
                RadioLifecycleObserverUnavailableReason.OBSERVER_ERROR,
            )
        return self._verify(response, radio_id=radio_id, nonce=nonce, key=key)

    def _verify(
        self, response: bytes, *, radio_id: RadioId, nonce: str, key: bytes
    ) -> RadioLifecycleObservationV0_1:
        observed = UtcNs(max(0, self._utc_now_ns()))
        if len(response) > _MAX_RESPONSE_BYTES:
            return self._unavailable(
                radio_id,
                observed,
                RadioLifecycleObserverUnavailableReason.INVALID_RESPONSE,
            )
        try:
            envelope = json.loads(response)
            if not isinstance(envelope, dict) or set(envelope) != {
                "payload",
                "mac_sha256",
            }:
                raise ValueError
            payload = envelope["payload"]
            mac = envelope["mac_sha256"]
            if not isinstance(payload, dict) or not isinstance(mac, str):
                raise TypeError
            encoded = canonical_json_bytes(payload)
            expected = hmac.new(key, encoded, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, mac):
                return self._unavailable(
                    radio_id,
                    observed,
                    RadioLifecycleObserverUnavailableReason.AUTHENTICATION_FAILED,
                )
            required = {
                "protocol",
                "radio_id",
                "nonce",
                "observed_utc_ns",
                "boot_id",
                "uptime_ns",
                "boot_time_uncertainty_ns",
                "iiod",
            }
            if set(payload) - (required | {"ad9361"}) or not required <= set(payload):
                raise ValueError
            if (
                payload["protocol"] != "leo-radio-lifecycle-diagnostic-v0.1"
                or payload["radio_id"] != str(radio_id)
                or payload["nonce"] != nonce
            ):
                raise ValueError
            response_utc = _int(payload["observed_utc_ns"])
            uptime = _int(payload["uptime_ns"])
            uncertainty = _int(payload["boot_time_uncertainty_ns"])
            iiod = _mapping(payload["iiod"])
            ad = payload.get("ad9361")
            observation = RadioLifecycleObservationV0_1(
                SchemaRef(RadioLifecycleObservationV0_1.SCHEMA_ID, V0_1),
                radio_id,
                UtcNs(response_utc),
                RadioLifecycleObservationStatus.AVAILABLE,
                RadioLifecycleObservationSource.AUTHENTICATED_DIAGNOSTIC_V1,
                RadioLifecycleTrust.RADIO_AUTHENTICATED,
                str(payload["boot_id"]),
                uptime,
                UtcNs(response_utc - uptime),
                uncertainty,
                IiodProcessIdentityV0_1(
                    _int(iiod["pid"]),
                    _int(iiod["proc_start_ticks"]),
                    _int(iiod["clock_ticks_per_second"]),
                ),
                None
                if ad is None
                else Ad9361LifecycleIdentityV0_1(
                    _int(_mapping(ad)["initialization_epoch"]),
                    _optional_string(_mapping(ad).get("reset_reason")),
                ),
            )
            return observation
        except (KeyError, TypeError, ValueError, OverflowError):
            return self._unavailable(
                radio_id,
                observed,
                RadioLifecycleObserverUnavailableReason.INVALID_RESPONSE,
            )

    @staticmethod
    def _unavailable(
        radio_id: RadioId,
        observed: UtcNs,
        reason: RadioLifecycleObserverUnavailableReason,
    ) -> RadioLifecycleObservationV0_1:
        return RadioLifecycleObservationV0_1(
            SchemaRef(RadioLifecycleObservationV0_1.SCHEMA_ID, V0_1),
            radio_id,
            observed,
            RadioLifecycleObservationStatus.UNAVAILABLE,
            RadioLifecycleObservationSource.AUTHENTICATED_DIAGNOSTIC_V1,
            RadioLifecycleTrust.RADIO_AUTHENTICATED,
            None,
            None,
            None,
            None,
            None,
            None,
            reason,
        )


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError
    return value
