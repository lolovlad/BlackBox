"""Drop finished motion clips when the video directory exceeds its quota.

Incident files and anything outside ``motion/`` stay on disk. An open motion
episode is kept so a recording that is still being written is not cut off.
"""

from __future__ import annotations

import os
from pathlib import Path


def measure_video_storage(root: Path) -> dict[str, int]:
    used = 0
    motion = 0
    motion_root = root / "motion"
    for path in _files(root):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        used += size
        if _under(path, motion_root):
            motion += size
    return {"used_bytes": used, "motion_bytes": motion}


def purge_motion_over_quota(root: Path, quota_bytes: int, busy: set[tuple[str, str]] | None = None) -> list[Path]:
    """Delete the oldest motion files until ``root`` fits in ``quota_bytes``.

    ``busy`` holds ``(camera_id, episode_id)`` pairs that are still recording.
    A non-positive quota disables cleanup. Returns the paths that were removed.
    """
    if quota_bytes <= 0 or not root.is_dir():
        return []
    root = root.resolve()
    motion_root = root / "motion"
    active = busy or set()
    used = 0
    candidates: list[tuple[float, int, Path]] = []
    for path in _files(root):
        try:
            stat = path.stat()
        except OSError:
            continue
        used += stat.st_size
        if not _under(path, motion_root) or _busy(path, motion_root, active):
            continue
        candidates.append((stat.st_mtime, stat.st_size, path))
    if used <= quota_bytes:
        return []
    candidates.sort(key=lambda item: (item[0], str(item[2])))
    removed: list[Path] = []
    for _mtime, size, path in candidates:
        if used <= quota_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        used -= size
        removed.append(path)
        _prune_empty(path.parent, motion_root)
    return removed


def _files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            found.append(Path(dirpath) / name)
    return found


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _busy(path: Path, motion_root: Path, busy: set[tuple[str, str]]) -> bool:
    try:
        parts = path.resolve().relative_to(motion_root.resolve()).parts
    except (OSError, ValueError):
        return True
    if len(parts) < 2:
        return False
    return (parts[0], parts[1]) in busy


def _prune_empty(directory: Path, stop: Path) -> None:
    current = directory
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
