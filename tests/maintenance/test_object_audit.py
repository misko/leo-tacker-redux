from __future__ import annotations

from leo_flow.contracts.core import Digest
from leo_flow.contracts.storage import ObjectMetadata, ObjectRef
from leo_flow.maintenance import audit_objects


def ref(name: str) -> ObjectRef:
    return ObjectRef(
        Digest.sha256(name.encode()),
        len(name),
        "application/octet-stream",
        "audit-fixture-v1",
        f"opaque://{name}",
    )


class Inventory:
    def __init__(self, values) -> None:
        self.values = values

    def objects(self):
        return iter(self.values)


class Verifier:
    def __init__(self, broken=()) -> None:
        self.broken = set(broken)
        self.calls = []

    def head(self, value):
        self.calls.append(value)
        if value in self.broken:
            raise OSError("sensitive storage detail")
        return ObjectMetadata(value, True)


def test_audit_hashes_every_exact_catalog_reference() -> None:
    values = (ref("one"), ref("two"))
    verifier = Verifier()

    report = audit_objects(Inventory(values), verifier)

    assert report.passed
    assert (report.object_count, report.verified_count) == (2, 2)
    assert verifier.calls == list(values)


def test_audit_continues_and_sanitizes_failure_details() -> None:
    values = (ref("one"), ref("two"), ref("three"))
    report = audit_objects(Inventory(values), Verifier((values[1],)))

    assert not report.passed
    assert report.verified_count == 2
    assert report.failures[0].object_digest == str(values[1].digest)
    assert report.failures[0].reason == "OSError"
    assert "sensitive" not in repr(report)


def test_duplicate_catalog_identity_is_audit_failure() -> None:
    value = ref("one")
    report = audit_objects(Inventory((value, value)), Verifier())

    assert not report.passed
    assert report.object_count == 1
    assert report.verified_count == 1
    assert report.failures[0].reason == "duplicate-catalog-identity"
