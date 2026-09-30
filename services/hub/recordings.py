"""List and open video files that were saved on the archive disk.

The rolling pre-roll stays in memory and is not part of this library.
Incident clips live under ``incidents/``, motion clips under ``motion/``,
and a manual episode is a file in the camera folder.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VIDEO_SUFFIXES = {".mkv", ".mp4", ".mov", ".webm", ".avi"}


class RecordingError(ValueError):
    """The requested archive path is missing or not inside the video directory."""


def normalize_relative(value: str) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if text.startswith("/") or ":" in text:
        raise RecordingError("bad path")
    parts = [part for part in text.split("/") if part != ""]
    if any(part in {".", ".."} or part.startswith(".") for part in parts):
        raise RecordingError("bad path")
    return "/".join(parts)


def place_of(relative: str) -> str:
    head = relative.split("/", 1)[0] if relative else ""
    if head == "motion":
        return "motion"
    if head == "incidents":
        return "incident"
    if head:
        return "manual"
    return "root"


def incident_folder_label(started_at: str, alert_name: str) -> str:
    stamp = str(started_at or "").replace("T", " ")[:16]
    name = str(alert_name or "").strip()
    if stamp and name:
        return f"{stamp} · {name}"
    return stamp or name


def display_name(
    parent_parts: list[str],
    name: str,
    kind: str,
    cameras: dict[str, str],
    incidents: dict[str, str],
) -> str:
    if kind != "dir":
        return name
    if not parent_parts and name == "incidents":
        return "Инциденты"
    if not parent_parts and name == "motion":
        return "Движение"
    if parent_parts == ["incidents"] and name in incidents:
        return incidents[name]
    if name in cameras and (not parent_parts or parent_parts[0] in {"motion", "incidents"}):
        return cameras[name]
    return name


def annotate_entries(
    entries: list[dict[str, Any]],
    relative: str,
    cameras: dict[str, str],
    incidents: dict[str, str],
) -> None:
    parent = [part for part in relative.split("/") if part] if relative else []
    for entry in entries:
        entry["label"] = display_name(parent, str(entry["name"]), str(entry["kind"]), cameras, incidents)


def crumbs(relative: str, cameras: dict[str, str], incidents: dict[str, str]) -> list[dict[str, str]]:
    items = [{"label": "Записи", "path": ""}]
    parts = [part for part in relative.split("/") if part] if relative else []
    acc: list[str] = []
    for part in parts:
        label = display_name(acc, part, "dir", cameras, incidents)
        acc.append(part)
        items.append({"label": label, "path": "/".join(acc)})
    return items


def media_type_for(path: Path) -> str:
    return {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
    }.get(path.suffix.lower(), "application/octet-stream")


def resolve_recording(root: Path, relative: str) -> Path:
    rel = normalize_relative(relative)
    base = root.resolve()
    target = base.joinpath(*rel.split("/")).resolve() if rel else base
    if target != base and base not in target.parents:
        raise RecordingError("outside")
    return target


def list_recordings(root: Path, relative: str) -> dict[str, Any]:
    rel = normalize_relative(relative)
    current = resolve_recording(root, rel)
    if not current.exists():
        if rel:
            raise RecordingError("missing")
        entries: list[dict[str, Any]] = []
    elif not current.is_dir():
        raise RecordingError("missing")
    else:
        entries = _children(root.resolve(), current, rel)
    if not rel:
        parent = None
    elif "/" not in rel:
        parent = ""
    else:
        parent = rel.rsplit("/", 1)[0]
    return {"path": rel, "parent": parent, "place": place_of(rel), "entries": entries}


def recording_file(root: Path, relative: str) -> Path:
    rel = normalize_relative(relative)
    if not rel:
        raise RecordingError("missing")
    path = resolve_recording(root, rel)
    if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in VIDEO_SUFFIXES:
        raise RecordingError("missing")
    return path


def _children(base: Path, current: Path, relative: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        children = list(current.iterdir())
    except OSError as exc:
        raise RecordingError("missing") from exc
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            resolved = child.resolve()
            stat = child.stat()
        except OSError:
            continue
        if resolved != base and base not in resolved.parents:
            continue
        child_rel = f"{relative}/{child.name}" if relative else child.name
        modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        if child.is_dir():
            entries.append(
                {
                    "name": child.name,
                    "label": child.name,
                    "path": child_rel,
                    "kind": "dir",
                    "size": None,
                    "modified": modified,
                    "ext": "",
                }
            )
        elif child.is_file() and child.suffix.lower() in VIDEO_SUFFIXES:
            entries.append(
                {
                    "name": child.name,
                    "label": child.name,
                    "path": child_rel,
                    "kind": "file",
                    "size": int(stat.st_size),
                    "modified": modified,
                    "ext": child.suffix.lower().lstrip("."),
                }
            )
    entries.sort(key=lambda item: (item["kind"] != "dir", str(item["name"]).lower()))
    return entries
