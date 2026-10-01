from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class ExportError(RuntimeError):
    pass


def approved_file(raw: Any, roots: Iterable[Path]) -> Path | None:
    if not raw:
        return None
    try:
        path = Path(str(raw)).resolve(strict=True)
        approved = [root.resolve() for root in roots]
        if path.is_file() and any(path == root or root in path.parents for root in approved):
            return path
    except OSError:
        pass
    return None


def merge_camera_segments(
    paths: Iterable[Any],
    output: Path,
    *,
    roots: Iterable[Path],
    timeout_seconds: int = 600,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
) -> dict[str, Any]:
    """Merge ordered camera segments into one MP4, with a transcoding fallback."""
    accepted: list[Path] = []
    omitted: list[dict[str, str]] = []
    for raw in paths:
        path = approved_file(raw, roots)
        if path is None:
            omitted.append({"path": str(raw), "reason": "missing_or_outside_approved_storage"})
        elif path not in accepted:
            accepted.append(path)
    accepted.sort(key=lambda item: (_segment_time(item), item.name))
    if not accepted:
        return {"state": "empty", "segments": [], "omitted": omitted, "gaps": []}

    output.parent.mkdir(parents=True, exist_ok=True)
    gaps = _detect_gaps(accepted)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".ffconcat", delete=False) as concat:
        concat.write("ffconcat version 1.0\n")
        for path in accepted:
            concat.write(f"file '{str(path).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
        concat_path = Path(concat.name)
    common = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path)]
    trim: list[str] = []
    first_time = _segment_time(accepted[0]).replace(tzinfo=timezone.utc)
    normalized_start = _utc(range_start)
    normalized_end = _utc(range_end)
    offset = max(0.0, (normalized_start - first_time).total_seconds()) if normalized_start else 0.0
    if offset:
        trim.extend(["-ss", f"{offset:.3f}"])
    if normalized_end:
        clip_start = max(normalized_start or first_time, first_time)
        duration = (normalized_end - clip_start).total_seconds()
        if duration > 0:
            trim.extend(["-t", f"{duration:.3f}"])
    mode = "copy"
    try:
        first = subprocess.run(
            [*common, *trim, "-map", "0:v:0", "-an", "-c:v", "copy", "-movflags", "+faststart", str(output)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if first.returncode != 0 or not output.exists() or output.stat().st_size == 0:
            mode = "libx264"
            output.unlink(missing_ok=True)
            second = subprocess.run(
                [*common, *trim, "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart", str(output)],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            if second.returncode != 0 or not output.exists():
                raise ExportError((second.stderr or first.stderr or "ffmpeg failed")[-1000:])
    except (OSError, subprocess.TimeoutExpired) as exc:
        output.unlink(missing_ok=True)
        raise ExportError(f"Не удалось объединить видео: {exc}") from exc
    finally:
        concat_path.unlink(missing_ok=True)
    return {
        "state": "included",
        "path": str(output),
        "mode": mode,
        "segments": [str(path) for path in accepted],
        "omitted": omitted,
        "gaps": gaps,
        "trim": {"offset_seconds": offset, "range_end": normalized_end.isoformat() if normalized_end else None},
    }


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _segment_time(path: Path) -> datetime:
    try:
        return datetime.strptime(path.stem[:15], "%Y%m%d_%H%M%S")
    except ValueError:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            return datetime.min


def _detect_gaps(paths: list[Path]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for previous, current in zip(paths, paths[1:]):
        seconds = (_segment_time(current) - _segment_time(previous)).total_seconds()
        if seconds > 5:
            gaps.append({"after": str(previous), "before": str(current), "seconds": seconds})
    return gaps


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
