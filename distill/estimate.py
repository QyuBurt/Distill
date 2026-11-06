"""'This would save X GB': a quick estimate of what converting files would free, before it starts.

A few slices of each file are compressed with zlib to see how well the data squeezes, then scaled by how the target
format typically compares with zlib. Estimates, not promises. No Qt in here."""
from __future__ import annotations

import os
import zlib
from dataclasses import dataclass
from pathlib import Path

SAMPLE_BYTES = 1 << 20
SAMPLES = 6
# How well each target format usually does against plain zlib level 3 on the same data (lower = smaller output).
_FACTOR = {"CHD": 0.85, "RVZ": 0.80, "CSO": 1.05, "ZSO": 0.95, "NSZ": 0.85, "XCZ": 0.85, "ZAR": 0.90}
_FALLBACK = {"CHD": 0.60, "RVZ": 0.55, "CSO": 0.75, "ZSO": 0.72, "NSZ": 0.70, "XCZ": 0.70, "ZAR": 0.75}  # unreadable file


def sample_ratio(path: Path, size: int) -> float | None:
    """Compressed/raw of a few evenly spread slices of the file; None if it can't be read."""
    if size <= 0:
        return None
    raw = packed = 0
    try:
        with path.open("rb") as f:
            for i in range(SAMPLES):
                f.seek(min(max(size - SAMPLE_BYTES, 0), i * size // SAMPLES))
                chunk = f.read(SAMPLE_BYTES)
                if not chunk:
                    continue
                raw += len(chunk)
                packed += len(zlib.compress(chunk, 3))
    except OSError:
        return None
    return packed / raw if raw else None


@dataclass(slots=True)
class Estimate:
    before: int
    after: int

    @property
    def saved(self) -> int:
        return max(0, self.before - self.after)


def estimate(paths: list[Path], target: str, sizes: dict[Path, int] | None = None) -> Estimate:
    """Estimated total size before and after converting `paths` to `target` ('CHD', 'RVZ', 'CSO', 'NSZ' ...)."""
    target = target.upper()
    before = after = 0
    for p in paths:
        try:
            size = (sizes or {}).get(p) or os.path.getsize(p)
        except OSError:
            continue
        r = sample_ratio(p, size)
        ratio = _FALLBACK.get(target, 0.7) if r is None else min(1.0, r * _FACTOR.get(target, 0.9))
        before += size
        after += int(size * ratio)
    return Estimate(before, after)
