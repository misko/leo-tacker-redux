"""Narrow admission port separating capture from prior analysis delivery."""

from __future__ import annotations

from typing import Protocol


class CaptureAdmissionGate(Protocol):
    def ready(self) -> bool:
        """Return true only when a new capture may begin."""
        ...
