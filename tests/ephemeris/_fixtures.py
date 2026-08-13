from __future__ import annotations

import io
from contextlib import nullcontext

from leo_flow.contracts.core import Digest
from leo_flow.contracts.ephemeris import EphemerisSource
from leo_flow.contracts.storage import ObjectRef


def checked(body: str) -> str:
    assert len(body) == 68
    checksum = (
        sum(int(char) if char.isdigit() else 1 if char == "-" else 0 for char in body)
        % 10
    )
    return body + str(checksum)


def tle(
    norad: int = 12345, epoch: str = "24200.50000000", name: str = "STARLINK TEST"
) -> bytes:
    line1 = checked(
        f"1 {norad:05d}U 24001A   {epoch}  .00000000  00000-0  00000-0 0  999"
    )
    line2 = checked(
        f"2 {norad:05d}  53.0000 100.0000 0001000  10.0000 350.0000 15.00000000    1"
    )
    return f"0 {name}\n{line1}\n{line2}\n".encode()


class MemoryArchive:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.sources: list[EphemerisSource] = []

    def put(
        self, data: bytes, *, source: EphemerisSource, **_kwargs: object
    ) -> ObjectRef:
        digest = Digest.sha256(data)
        self.objects[digest.value] = data
        self.sources.append(source)
        media_type = "application/json" if data.startswith(b"{") else "text/plain"
        return ObjectRef(
            digest,
            len(data),
            media_type,
            "ephemeris-fixture-v1",
            f"memory:{digest.value}",
        )

    def open(self, ref: ObjectRef):
        return nullcontext(io.BytesIO(self.objects[ref.digest.value]))
