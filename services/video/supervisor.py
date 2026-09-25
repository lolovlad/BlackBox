"""Run preview and episode ffmpeg processes. An episode ends when its process exits."""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.video.settings import (
    CameraSettings,
    VideoConfig,
    build_episode_argv,
    build_preview_argv,
    episode_name,
)


def _preview_signature(camera: CameraSettings) -> str:
    payload = {
        "url": camera.url,
        "rtsp_transport": camera.rtsp_transport,
        "codec": camera.codec,
        "width": camera.width,
        "height": camera.height,
    }
    return json.dumps(payload, sort_keys=True)


class Supervisor:
    def __init__(self, data_root: Path, spawn: Callable[[list[str]], Any] | None = None) -> None:
        self.data_root = data_root
        self.spawn = spawn or _popen
        self.output_root = data_root / "video"
        self.episodes: dict[str, dict[str, Any]] = {}
        self.previews: dict[str, dict[str, Any]] = {}
        self.preview_errors: dict[str, str] = {}
        self.closed: dict[str, dict[str, str]] = {}
        self.pending_logs: list[dict[str, str]] = []

    def tick(
        self,
        config: VideoConfig,
        episodes: list[dict[str, Any]],
        previews: list[CameraSettings],
        output_root: Path | None = None,
    ) -> dict[str, list[dict[str, str]]]:
        if output_root is not None:
            self.output_root = Path(output_root)
        events: list[dict[str, str]] = []
        self._reap_episodes(events)
        self._sync_episodes(config, episodes, events)
        recording = {str(slot["camera_id"]) for slot in self.episodes.values() if _running(slot["proc"])}
        self._sync_previews(previews, recording)
        self._drain_slots()
        return {"statuses": self._statuses(config, previews, recording), "episodes": events, "logs": self._take_logs()}

    def stop_all(self) -> None:
        for slot in list(self.episodes.values()) + list(self.previews.values()):
            _stop(slot["proc"])
        self.episodes.clear()
        self.previews.clear()

    def _reap_episodes(self, events: list[dict[str, str]]) -> None:
        for episode_id in list(self.episodes):
            slot = self.episodes[episode_id]
            if _running(slot["proc"]):
                continue
            code = slot["proc"].poll()
            self._drain_slot(slot)
            message = _tail(slot)
            if code == 0:
                state = "finished"
                self._note(str(slot["camera_id"]), f"Эпизод {episode_id} завершён, код {code}", level="info", source="hub")
            else:
                state = "error"
                message = message or f"ffmpeg завершился с кодом {code}"
                self._note(str(slot["camera_id"]), f"Эпизод {episode_id} завершился с кодом {code}", level="error", source="hub")
            event = {
                "id": episode_id,
                "state": state,
                "path": str(slot["path"]),
                "message": message,
                "started_at": str(slot["started_at"]),
                "ended_at": _now(),
            }
            self._close(episode_id, event)
            events.append(event)
            del self.episodes[episode_id]

    def _sync_episodes(self, config: VideoConfig, episodes: list[dict[str, Any]], events: list[dict[str, str]]) -> None:
        cameras = {camera.id: camera for camera in config.cameras}
        emitted = {event["id"] for event in events}
        root = self.output_root
        for item in episodes:
            episode_id = str(item.get("id") or "")
            state = str(item.get("state") or "")
            if not episode_id or state not in {"queued", "recording"}:
                continue
            if episode_id in self.closed:
                if episode_id not in emitted:
                    events.append(dict(self.closed[episode_id]))
                continue
            if episode_id in self.episodes:
                if state == "queued":
                    slot = self.episodes[episode_id]
                    events.append(_recording_event(episode_id, slot))
                continue
            if state == "recording":
                event = {
                    "id": episode_id,
                    "state": "error",
                    "path": str(item.get("path") or ""),
                    "message": "запись прервана",
                    "started_at": str(item.get("started_at") or ""),
                    "ended_at": _now(),
                }
                self._close(episode_id, event)
                events.append(event)
                continue
            camera = cameras.get(str(item.get("camera_id") or ""))
            if camera is None or not camera.enabled or not camera.url:
                event = {
                    "id": episode_id,
                    "state": "error",
                    "path": "",
                    "message": "камера недоступна",
                    "started_at": "",
                    "ended_at": _now(),
                }
                self._close(episode_id, event)
                events.append(event)
                continue
            self._stop_preview(camera.id)
            started_at = _now()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            directory = root / camera.id
            path = directory / episode_name(camera, episode_id, stamp)
            argv = build_episode_argv(camera, path)
            self._note(camera.id, f"Эпизод {episode_id} запущен: {_redact_argv(argv)}", level="info", source="hub")
            try:
                directory.mkdir(parents=True, exist_ok=True)
                proc = self.spawn(argv)
            except OSError as exc:
                event = {
                    "id": episode_id,
                    "state": "error",
                    "path": "",
                    "message": "ffmpeg не найден" if isinstance(exc, FileNotFoundError) else str(exc),
                    "started_at": "",
                    "ended_at": _now(),
                }
                self._note(camera.id, event["message"], level="error", source="hub")
                self._close(episode_id, event)
                events.append(event)
                continue
            slot = _slot(proc, path, camera.id, started_at)
            slot["log_source"] = "ffmpeg"
            self.episodes[episode_id] = slot
            events.append(_recording_event(episode_id, slot))

    def _sync_previews(self, previews: list[CameraSettings], recording: set[str]) -> None:
        wanted = {camera.id: camera for camera in previews if camera.url and camera.id not in recording}
        for camera_id in list(self.previews):
            slot = self.previews[camera_id]
            camera = wanted.get(camera_id)
            signature = _preview_signature(camera) if camera is not None else ""
            if camera is None or slot["signature"] != signature or not _running(slot["proc"]):
                self._drain_slot(slot)
                if camera is not None and not _running(slot["proc"]):
                    self.preview_errors[camera_id] = _tail(slot) or "просмотр остановился"
                elif camera is None:
                    self._note(camera_id, "Просмотр остановлен", level="info", source="hub")
                _stop(slot["proc"])
                del self.previews[camera_id]
        for camera_id, camera in wanted.items():
            if camera_id in self.previews:
                continue
            jpeg = self.data_root / "video" / ".preview" / f"{camera_id}.jpg"
            argv = build_preview_argv(camera, jpeg)
            self._note(camera_id, f"Просмотр запущен: {_redact_argv(argv)}", level="info", source="hub")
            try:
                jpeg.parent.mkdir(parents=True, exist_ok=True)
                proc = self.spawn(argv)
            except OSError as exc:
                self.preview_errors[camera_id] = "ffmpeg не найден" if isinstance(exc, FileNotFoundError) else str(exc)
                self._note(camera_id, self.preview_errors[camera_id], level="error", source="hub")
                continue
            self.preview_errors.pop(camera_id, None)
            self.previews[camera_id] = {
                "proc": proc,
                "signature": _preview_signature(camera),
                "lines": _watch(proc),
                "sent": 0,
                "camera_id": camera_id,
                "log_source": "ffmpeg",
            }

    def _statuses(self, config: VideoConfig, previews: list[CameraSettings], recording: set[str]) -> list[dict[str, str]]:
        wanted = {camera.id for camera in previews if camera.url and camera.id not in recording}
        rows: list[dict[str, str]] = []
        for camera in config.cameras:
            if camera.id in recording:
                slot = next(item for item in self.episodes.values() if item["camera_id"] == camera.id and _running(item["proc"]))
                rows.append({"id": camera.id, "state": "recording", "message": _tail(slot)})
                continue
            preview = self.previews.get(camera.id)
            if preview is not None and _running(preview["proc"]):
                rows.append({"id": camera.id, "state": "preview", "message": _tail(preview)})
                continue
            if camera.id in wanted and camera.id in self.preview_errors:
                rows.append({"id": camera.id, "state": "error", "message": self.preview_errors[camera.id]})
                continue
            rows.append({"id": camera.id, "state": "stopped", "message": ""})
        return rows

    def _stop_preview(self, camera_id: str) -> None:
        slot = self.previews.pop(camera_id, None)
        if slot is None:
            return
        self._drain_slot(slot)
        self._note(camera_id, "Просмотр остановлен на время записи", level="info", source="hub")
        _stop(slot["proc"])

    def _drain_slots(self) -> None:
        for slot in list(self.episodes.values()) + list(self.previews.values()):
            self._drain_slot(slot)

    def _drain_slot(self, slot: dict[str, Any]) -> None:
        lines = slot.get("lines") or []
        snapshot = list(lines)
        sent = int(slot.get("sent") or 0)
        camera_id = str(slot.get("camera_id") or "")
        source = str(slot.get("log_source") or "ffmpeg")
        for line in snapshot[sent:]:
            self._note(camera_id, line, source=source)
        slot["sent"] = len(snapshot)
        if slot["sent"] > 400:
            drop = slot["sent"] - 80
            del lines[:drop]
            slot["sent"] -= drop

    def _note(self, camera_id: str, line: str, *, level: str | None = None, source: str = "ffmpeg") -> None:
        text = str(line or "").strip()
        if not camera_id or not text:
            return
        self.pending_logs.append(
            {
                "camera_id": camera_id,
                "level": level or _log_level(text),
                "source": source,
                "line": text[:2000],
            }
        )

    def _take_logs(self) -> list[dict[str, str]]:
        items = self.pending_logs
        self.pending_logs = []
        return items

    def _close(self, episode_id: str, event: dict[str, str]) -> None:
        self.closed[episode_id] = event
        if len(self.closed) > 200:
            for key in list(self.closed)[:-100]:
                self.closed.pop(key, None)


def _recording_event(episode_id: str, slot: dict[str, Any]) -> dict[str, str]:
    return {
        "id": episode_id,
        "state": "recording",
        "path": str(slot["path"]),
        "message": "",
        "started_at": str(slot["started_at"]),
        "ended_at": "",
    }


def _slot(proc: Any, path: Path, camera_id: str, started_at: str) -> dict[str, Any]:
    return {"proc": proc, "path": path, "camera_id": camera_id, "started_at": started_at, "lines": _watch(proc), "sent": 0}


def _watch(proc: Any) -> list[str]:
    lines: list[str] = []
    stderr = getattr(proc, "stderr", None)
    if stderr is not None:
        threading.Thread(target=_capture, args=(stderr, lines), daemon=True).start()
    return lines


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _popen(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def _running(proc: Any) -> bool:
    poll = getattr(proc, "poll", None)
    if not callable(poll):
        return False
    return poll() is None


def _stop(proc: Any) -> None:
    if not _running(proc):
        return
    terminate = getattr(proc, "terminate", None)
    if callable(terminate):
        terminate()
    wait = getattr(proc, "wait", None)
    if callable(wait):
        try:
            wait(timeout=2)
        except Exception:
            kill = getattr(proc, "kill", None)
            if callable(kill):
                kill()


def _redact_argv(argv: list[str]) -> str:
    return " ".join(_redact_url(part) if part.startswith(("rtsp://", "rtsps://")) else part for part in argv)


def _redact_url(url: str) -> str:
    scheme, mark, rest = url.partition("://")
    if "@" in rest:
        rest = "***@" + rest.split("@", 1)[1]
    return f"{scheme}{mark}{rest}"


def _log_level(line: str) -> str:
    lowered = line.lower()
    if any(word in lowered for word in ("error", "failed", "nothing was written", "errno", "invalid", "denied", "refused")):
        return "error"
    return "info"


def _tail(slot: dict[str, Any]) -> str:
    lines = slot.get("lines") or []
    if lines:
        return str(lines[-1])[:500]
    return ""


def _capture(stream: Any, lines: list[str]) -> None:
    try:
        for line in stream:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            text = text.strip()
            if not text:
                continue
            lines.append(text)
    except Exception:
        return
