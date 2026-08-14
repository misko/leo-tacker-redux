"""Privileged, explicitly injected garbage collection for cataloged blobs."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.storage import ObjectRef


class MaintenanceBlobDeleter(Protocol):
    """Delete exactly one immutable object; never exposed to runtime services."""

    def delete(self, ref: ObjectRef) -> None: ...


@dataclass(frozen=True)
class GarbageCollectionCandidate:
    ref: ObjectRef


@dataclass(frozen=True)
class GarbageCollectionResult:
    object_digest: str
    outcome: str


class GarbageCollectionCatalog(Protocol):
    def candidates(self, *, as_of_utc_ns: int, limit: int) -> Iterable[ObjectRef]: ...

    def claim(
        self,
        ref: ObjectRef,
        *,
        claim_token: str,
        claimed_at_utc_ns: int,
        claim_expires_at_utc_ns: int,
    ) -> ObjectRef | None: ...

    def complete(
        self, ref: ObjectRef, *, claim_token: str, completed_at_utc_ns: int
    ) -> bool: ...

    def fail(
        self,
        ref: ObjectRef,
        *,
        claim_token: str,
        failed_at_utc_ns: int,
        detail: str,
    ) -> bool: ...


class GarbageCollectionError(RuntimeError):
    """A claimed deletion could not be durably fenced or recorded."""


def collect_objects(
    catalog: GarbageCollectionCatalog,
    deleter: MaintenanceBlobDeleter,
    *,
    limit: int = 100,
    claim_ttl_seconds: int = 300,
    now_utc_ns: int | None = None,
) -> tuple[GarbageCollectionResult, ...]:
    """Claim and delete eligible objects one at a time with durable outcomes."""

    if limit <= 0 or claim_ttl_seconds <= 0:
        raise ValueError("limit and claim_ttl_seconds must be positive")
    now = time.time_ns() if now_utc_ns is None else now_utc_ns
    if now < 0:
        raise ValueError("now_utc_ns must be non-negative")
    expires = now + claim_ttl_seconds * 1_000_000_000
    results: list[GarbageCollectionResult] = []
    for proposed in catalog.candidates(as_of_utc_ns=now, limit=limit):
        token = uuid.uuid4().hex
        claimed = catalog.claim(
            proposed,
            claim_token=token,
            claimed_at_utc_ns=now,
            claim_expires_at_utc_ns=expires,
        )
        if claimed is None:
            results.append(GarbageCollectionResult(str(proposed.digest), "skipped"))
            continue
        try:
            deleter.delete(claimed)
        except Exception as error:
            detail = f"remote-delete:{type(error).__name__}"
            if not catalog.fail(
                claimed,
                claim_token=token,
                failed_at_utc_ns=now,
                detail=detail,
            ):
                raise GarbageCollectionError(
                    "GC failure lost its claim fence"
                ) from error
            results.append(
                GarbageCollectionResult(str(claimed.digest), "delete_failed")
            )
            continue
        if not catalog.complete(claimed, claim_token=token, completed_at_utc_ns=now):
            raise GarbageCollectionError(
                "deleted bytes but lost the catalog claim fence"
            )
        results.append(GarbageCollectionResult(str(claimed.digest), "deleted"))
    return tuple(results)
