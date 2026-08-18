"""Shared composition helper for optional heavy station workers."""

from __future__ import annotations

import time
from pathlib import Path

from leo_flow.adapters.optional_heavy_work_admission import (
    LocalCaptureAwareHeavyWorkAdmissionV0_1,
)
from leo_flow.contracts.optional_heavy_work_admission import (
    OptionalHeavyWorkAdmissionPortV0_1,
)


def build_optional_heavy_work_admission(
    guard_path: Path | None,
    *,
    maximum_focused_backlog: int,
    host_cpu_cores: int,
    reserved_cpu_cores: int,
    estimated_claim_cpu_cores: int,
    minimum_memory_available_bytes: int,
    maximum_io_pressure_avg10: float,
    maximum_optional_concurrency: int,
) -> OptionalHeavyWorkAdmissionPortV0_1 | None:
    if guard_path is None:
        return None
    return LocalCaptureAwareHeavyWorkAdmissionV0_1(
        guard_path,
        clock_ns=time.time_ns,
        maximum_focused_backlog=maximum_focused_backlog,
        host_cpu_cores=host_cpu_cores,
        reserved_cpu_cores=reserved_cpu_cores,
        estimated_claim_cpu_cores=estimated_claim_cpu_cores,
        minimum_memory_available_bytes=minimum_memory_available_bytes,
        maximum_io_pressure_avg10=maximum_io_pressure_avg10,
        maximum_optional_concurrency=maximum_optional_concurrency,
    )
