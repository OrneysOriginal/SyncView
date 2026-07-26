from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass(slots=True)
class ClockSample:
    t0: float
    t1_remote: float
    t2: float

    @property
    def rtt(self) -> float:
        return self.t2 - self.t0

    @property
    def offset(self) -> float:
        return self.t1_remote - ((self.t0 + self.t2) / 2)


def summarize(samples: list[ClockSample]) -> tuple[float, float]:
    if not samples:
        return 0.0, 0.0
    return median(s.rtt for s in samples), median(s.offset for s in samples)


def execution_delay_seconds(ping_seconds: float) -> float:
    return max(0.3, 3 * ping_seconds)
