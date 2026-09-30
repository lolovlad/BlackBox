"""Frame-difference motion, the same idea as Motion's noise_level / threshold / event_gap.

A frame counts as motion when enough pixels differ from the previous frame by more
than the noise level. Recording stays open through quiet frames until gap_sec passes,
so a short pause does not split one event into two files. This is not person detection.
"""

from __future__ import annotations

ANALYSIS_WIDTH = 320
ANALYSIS_HEIGHT = 180
ANALYSIS_FPS = 2


class MotionTracker:
    def __init__(
        self,
        *,
        noise: int = 32,
        threshold_pct: int = 2,
        min_frames: int = 2,
        gap_sec: float = 10,
    ) -> None:
        self.noise = noise
        self.threshold_pct = threshold_pct
        self.min_frames = min_frames
        self.gap_sec = gap_sec
        self.prev: bytes | None = None
        self.streak = 0
        self.active = False
        self.quiet_since: float | None = None

    def configure(self, *, noise: int, threshold_pct: int, min_frames: int, gap_sec: float) -> None:
        self.noise = int(noise)
        self.threshold_pct = int(threshold_pct)
        self.min_frames = max(1, int(min_frames))
        self.gap_sec = max(0.0, float(gap_sec))

    def push(self, frame: bytes, now: float) -> str | None:
        """Return 'start', 'stop', or None. `now` is a monotonic or wall timestamp in seconds."""
        if not frame:
            return None
        previous = self.prev
        if previous is not None and len(previous) != len(frame):
            self.prev = frame
            self.streak = 0
            return None
        self.prev = frame
        if previous is None:
            return None
        changed = _changed_pixels(previous, frame, self.noise)
        hot = changed * 100 >= self.threshold_pct * len(frame)
        if hot:
            self.quiet_since = None
            self.streak += 1
            if not self.active and self.streak >= self.min_frames:
                self.active = True
                return "start"
            return None
        self.streak = 0
        if not self.active:
            return None
        if self.quiet_since is None:
            self.quiet_since = now
        if now - self.quiet_since >= self.gap_sec:
            self.active = False
            self.quiet_since = None
            return "stop"
        return None


def _changed_pixels(previous: bytes, frame: bytes, noise: int) -> int:
    count = 0
    for left, right in zip(previous, frame, strict=True):
        if abs(left - right) > noise:
            count += 1
    return count
