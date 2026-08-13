"""Verify every catalog object against its immutable blob-store bytes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from leo_flow.contracts.storage import ObjectMetadata, ObjectRef


class ObjectInventory(Protocol):
    def objects(self) -> Iterable[ObjectRef]: ...


class BlobVerifier(Protocol):
    def head(self, ref: ObjectRef) -> ObjectMetadata: ...


@dataclass(frozen=True)
class ObjectAuditFailure:
    object_digest: str
    reason: str


@dataclass(frozen=True)
class ObjectAuditReport:
    object_count: int
    verified_count: int
    failures: tuple[ObjectAuditFailure, ...]

    @property
    def passed(self) -> bool:
        return self.object_count == self.verified_count and not self.failures


def audit_objects(
    inventory: ObjectInventory, verifier: BlobVerifier
) -> ObjectAuditReport:
    """Read and hash each registered object; continue to report all failures."""

    count = 0
    verified = 0
    failures: list[ObjectAuditFailure] = []
    seen: set[str] = set()
    for ref in inventory.objects():
        identity = str(ref.digest)
        if identity in seen:
            failures.append(ObjectAuditFailure(identity, "duplicate-catalog-identity"))
            continue
        seen.add(identity)
        count += 1
        try:
            metadata = verifier.head(ref)
            if metadata.ref != ref or not metadata.verified:
                raise ValueError("blob verifier did not confirm exact reference")
        except Exception as error:  # noqa: BLE001 - audit continues across objects
            failures.append(ObjectAuditFailure(identity, type(error).__name__))
        else:
            verified += 1
    return ObjectAuditReport(count, verified, tuple(failures))
