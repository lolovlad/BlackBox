"""List and open video files that were saved on the archive disk.

The rolling pre-roll stays in memory and is not part of this library.
Incident clips live under ``incidents/``, motion clips under ``motion/``,
and a manual episode is a file in the camera folder.
"""

from __future__ import annotations

import os
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


def list_recordings(root: Path, relative: str, *, page: int = 1, page_size: int = 0) -> dict[str, Any]:
    rel = normalize_relative(relative)
    current = resolve_recording(root, rel)
    if not current.exists():
        if rel:
            raise RecordingError("missing")
        folder_entries, file_entries = [], []
    elif not current.is_dir():
        raise RecordingError("missing")
    else:
        folder_entries, file_entries = _scan_names(current)
    if page_size and page_size > 0:
        size = max(1, int(page_size))
        total = len(file_entries)
        pages = max(1, (total + size - 1) // size) if total else 1
        current_page = min(max(1, int(page)), pages)
        start = (current_page - 1) * size
        file_entries = file_entries[start : start + size]
    else:
        total = len(file_entries)
        current_page = 1
        pages = 1
        size = total or 0
    base = root.resolve()
    folders = [entry for item in folder_entries if (entry := _entry_from_dir(base, item, rel, "dir"))]
    files = [entry for item in file_entries if (entry := _entry_from_dir(base, item, rel, "file"))]
    if not rel:
        parent = None
    elif "/" not in rel:
        parent = ""
    else:
        parent = rel.rsplit("/", 1)[0]
    return {
        "path": rel,
        "parent": parent,
        "place": place_of(rel),
        "entries": [*folders, *files],
        "page": current_page,
        "total_pages": pages,
        "total_rows": total,
        "page_size": size,
    }


def recording_file(root: Path, relative: str) -> Path:
    rel = normalize_relative(relative)
    if not rel:
        raise RecordingError("missing")
    path = resolve_recording(root, rel)
    if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in VIDEO_SUFFIXES:
        raise RecordingError("missing")
    return path


def _scan_names(current: Path) -> tuple[list[os.DirEntry], list[os.DirEntry]]:
    folders: list[os.DirEntry] = []
    files: list[os.DirEntry] = []
    try:
        children = os.scandir(current)
    except OSError as exc:
        raise RecordingError("missing") from exc
    with children:
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                is_dir = child.is_dir(follow_symlinks=False)
                is_file = child.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                folders.append(child)
            elif is_file and Path(child.name).suffix.lower() in VIDEO_SUFFIXES:
                files.append(child)
    folders.sort(key=lambda item: item.name.lower())
    files.sort(key=lambda item: item.name.lower())
    return folders, files


def _entry_from_dir(base: Path, child: os.DirEntry, relative: str, kind: str) -> dict[str, Any] | None:
    try:
        resolved = Path(child.path).resolve()
        stat = child.stat(follow_symlinks=False)
    except OSError:
        return None
    if resolved != base and base not in resolved.parents:
        return None
    child_rel = f"{relative}/{child.name}" if relative else child.name
    return {
        "name": child.name,
        "label": child.name,
        "path": child_rel,
        "kind": kind,
        "size": None if kind == "dir" else int(stat.st_size),
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "ext": "" if kind == "dir" else Path(child.name).suffix.lower().lstrip("."),
    }


def _children(base: Path, current: Path, relative: str) -> list[dict[str, Any]]:
    folders, files = _scan_names(current)
    entries = [entry for item in folders if (entry := _entry_from_dir(base, item, relative, "dir"))]
    entries.extend(entry for item in files if (entry := _entry_from_dir(base, item, relative, "file")))
    return entries
