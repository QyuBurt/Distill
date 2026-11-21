"""'This would save about X': what a conversion batch would free, worked out before anything is started.

It asks the batch engine's own scan which titles a run would take (so the preview and the run agree on what is in the batch),
then samples each title with estimate.py. Estimates, not promises: a few slices of each file, scaled by how the target format
usually compares. Archives are counted but not estimated (what is inside is not known until it is unpacked). No Qt.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .batch import BatchRunner
from .estimate import estimate
from .models import Config, fmt_size

# the pages that make files smaller, and the format name estimate.py knows them by
TARGETS = {"chd": "CHD", "rvz": "RVZ", "cso": "CSO", "zar": "ZAR"}  # (the Switch page has its own runner, so no preview there)
FOLDER_SAMPLE = 8  # a game folder is sampled through its largest files, then scaled to the whole folder


@dataclass(frozen=True, slots=True)
class Preview:
    mode: str
    titles: int      # titles that were estimated
    archives: int    # archives found: counted, not estimated
    before: int
    after: int
    stopped: bool = False

    @property
    def saved(self) -> int:
        return max(0, self.before - self.after)

    @property
    def percent(self) -> int:
        return round(self.saved * 100 / self.before) if self.before else 0

    def headline(self) -> str:
        """One line for a toast or a label."""
        if not self.titles and not self.archives:
            return "Nothing to convert was found in that folder."
        if not self.titles:
            return f"Only archives found ({self.archives}); what they hold is not known until they are unpacked."
        text = (f"About {fmt_size(self.saved)} saved ({self.percent}%): {self.titles} "
                f"title{'s' if self.titles != 1 else ''}, {fmt_size(self.before)} \u2192 about {fmt_size(self.after)}")
        if self.archives:
            text += f" \u00b7 {self.archives} archive{'s' if self.archives != 1 else ''} not counted"
        return text + (" \u00b7 stopped early" if self.stopped else "")


def _folder_estimate(folder: Path, target: str) -> tuple[int, int]:
    """(before, after) for a game folder: the largest few files are sampled, and their ratio is applied to the whole folder."""
    files: list[tuple[int, Path]] = []
    for root, _dirs, names in os.walk(folder):
        for name in names:
            path = Path(root, name)
            try:
                files.append((path.stat().st_size, path))
            except OSError:
                continue
    total = sum(size for size, _ in files)
    if not total:
        return 0, 0
    top = sorted(files, key=lambda f: f[0], reverse=True)[:FOLDER_SAMPLE]
    sample = estimate([p for _, p in top], target, {p: s for s, p in top})
    ratio = sample.after / sample.before if sample.before else 1.0
    return total, int(total * ratio)


def preview(cfg: Config, progress: Callable[[int, int], None] | None = None, stop: threading.Event | None = None) -> Preview | None:
    """What converting everything the batch would take would save, or None for a page that does not compress (PS3, PKG, Xbox)."""
    target = TARGETS.get(cfg.mode)
    if target is None:
        return None
    runner = BatchRunner(cfg, lambda _event: None, threading.Event())  # the engine's own scan, with nobody listening
    sources = runner._scan()
    before = after = titles = archives = 0
    stopped = False
    for i, src in enumerate(sources):
        if stop is not None and stop.is_set():
            stopped = True
            break
        if progress is not None:
            progress(i, len(sources))
        if src.is_archive:
            archives += 1
            continue
        if src.path.is_dir():
            b, a = _folder_estimate(src.path, target)
        else:
            est = estimate(sorted(src.originals(), key=str), target)
            b, a = est.before, est.after
        if b:
            before, after, titles = before + b, after + a, titles + 1
    return Preview(cfg.mode, titles, archives, before, after, stopped)
