"""Ephemeris-specific, content-addressed archive adapters.

The generic blob store owns paths.  This adapter only assigns stable media and
format identities and makes repeated provider deliveries idempotent.
"""

from __future__ import annotations

import io

from leo_flow.contracts.core import Digest, UtcNs
from leo_flow.contracts.ephemeris import EphemerisSource
from leo_flow.contracts.storage import ObjectRef
from leo_flow.storage.ports import BlobWriter


class _CasArchive:
    def __init__(self, writer: BlobWriter) -> None:
        self._writer = writer

    def _put(
        self,
        data: bytes,
        *,
        media_type: str,
        format_id: str,
        idempotency_namespace: str,
    ) -> ObjectRef:
        digest = Digest.sha256(data)
        return self._writer.put(
            io.BytesIO(data),
            expected_digest=digest,
            expected_bytes=len(data),
            media_type=media_type,
            format_id=format_id,
            idempotency_key=f"{idempotency_namespace}:{digest.value}",
        )


class CasRawEphemerisArchive(_CasArchive):
    def put(
        self,
        data: bytes,
        *,
        source: EphemerisSource,
        retrieved_utc_ns: UtcNs,
    ) -> ObjectRef:
        del retrieved_utc_ns  # retrieval time belongs in provenance, not CAS identity
        return self._put(
            data,
            media_type="text/plain",
            format_id="tle-raw-v1",
            idempotency_namespace=f"ephemeris:{source.value}:raw",
        )


class CasNormalizedEphemerisArchive(_CasArchive):
    def put(self, data: bytes, *, source: EphemerisSource) -> ObjectRef:
        return self._put(
            data,
            media_type="application/json",
            format_id="tle-normalized-v1",
            idempotency_namespace=f"ephemeris:{source.value}:normalized",
        )


class CasEphemerisProvenanceArchive(_CasArchive):
    def put_manifest(self, data: bytes) -> ObjectRef:
        return self._put(
            data,
            media_type="application/json",
            format_id="ephemeris-provenance-v1",
            idempotency_namespace="ephemeris:provenance",
        )
