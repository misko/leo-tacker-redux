"""Candidate-only QAM separation metric shared by analysis presentations."""

from __future__ import annotations

import math


def qam_goodness_v0_2(hard_symbol_accuracy: float, rms_evm: float) -> float:
    """Rank separated known-symbol QAM high and chance/noise low on [0, 1]."""

    if (
        not math.isfinite(hard_symbol_accuracy)
        or not math.isfinite(rms_evm)
        or not 0.0 <= hard_symbol_accuracy <= 1.0
        or rms_evm < 0.0
    ):
        raise ValueError("QAM goodness inputs are invalid")
    chance_corrected = max(0.0, min(1.0, (hard_symbol_accuracy - 0.25) / 0.75))
    compactness = 1.0 / (1.0 + (rms_evm / 2.0) ** 2)
    return math.sqrt(chance_corrected * compactness)
