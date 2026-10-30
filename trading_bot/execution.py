"""Deterministic paper costs. No exchange or account access."""
import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PaperCosts:
    entry_fee_bps: float = 10.0
    exit_fee_bps: float = 10.0
    slippage_bps: float = 0.0

    def __post_init__(self):
        for value in asdict(self).values():
            if not math.isfinite(value) or not 0 <= value < 10000:
                raise ValueError("Paper costs must be finite and in [0, 10000) bps")

    def fill(self, quote, direction, opening):
        if not math.isfinite(quote) or quote <= 0:
            raise ValueError("Invalid quote")
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("Invalid direction")
        buying = (direction == "LONG") == opening
        return quote * (1 + (1 if buying else -1) * self.slippage_bps / 10000)

    def as_dict(self):
        return asdict(self)
