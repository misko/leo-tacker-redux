from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from leo_flow.contracts.core import Digest
from leo_flow.contracts.storage import ObjectRef
from leo_flow.maintenance.object_gc import collect_objects


def ref(label: str) -> ObjectRef:
    digest = Digest.sha256(label.encode())
    return ObjectRef(
        digest,
        len(label),
        "application/octet-stream",
        "test-v1",
        f"cas:sha256:{digest.value}",
    )


@dataclass
class Catalog:
    refs: tuple[ObjectRef, ...]
    claimed: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def candidates(self, *, as_of_utc_ns: int, limit: int):
        return self.refs[:limit]

    def claim(self, object_ref, **values):
        self.claimed.append(values["claim_token"])
        return object_ref

    def complete(self, object_ref, **values):
        self.completed.append(values["claim_token"])
        return True

    def fail(self, object_ref, **values):
        self.failures.append(values["detail"])
        return True


class Deleter:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.deleted: list[ObjectRef] = []

    def delete(self, object_ref: ObjectRef) -> None:
        self.deleted.append(object_ref)
        if self.fails:
            raise OSError("sensitive path must not escape")


def test_collection_uses_only_injected_deleter_and_completes_claim() -> None:
    object_ref = ref("orphan")
    catalog = Catalog((object_ref,))
    deleter = Deleter()

    result = collect_objects(catalog, deleter, now_utc_ns=1_000)

    assert deleter.deleted == [object_ref]
    assert result[0].outcome == "deleted"
    assert catalog.completed == catalog.claimed


def test_remote_failure_is_sanitized_and_durably_recorded() -> None:
    catalog = Catalog((ref("orphan"),))

    result = collect_objects(catalog, Deleter(fails=True), now_utc_ns=1_000)

    assert result[0].outcome == "delete_failed"
    assert catalog.failures == ["remote-delete:OSError"]


@pytest.mark.parametrize("limit,ttl", [(0, 1), (1, 0), (-1, 1)])
def test_invalid_collection_bounds_fail_closed(limit: int, ttl: int) -> None:
    with pytest.raises(ValueError):
        collect_objects(Catalog(()), Deleter(), limit=limit, claim_ttl_seconds=ttl)
