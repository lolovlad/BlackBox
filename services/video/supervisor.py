"""Keep one ffmpeg process per enabled camera."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from services.video.settings import CameraSettings, VideoConfig, build_ffmpeg_argv


def _signature(camera: CameraSettings) -> str:
    return camera.model_dump_json()


class Supervisor:
    def __init__(self, data_root: Path, spawn: Callable[[list[str]], Any] | None = None) -> None:
        self.data_root = data_root
        self.spawn = spawn or _popen
        self.slots: dict[str, dict[str, Any]] = {}

    def reconcile(self, config: VideoConfig) -> list[dict[str, str]]:
        wanted = {camera.id: camera for camera in config.cameras if camera.enabled and camera.url}
        for camera_id in list(self.slots):
            slot = self.slots[camera_id]
            camera = wanted.get(camera_id)
            running = _running(slot["proc"])
            if camera is None or slot["signature"] != _signature(camera) or not running:
                _stop(slot["proc"])
                del self.slots[camera_id]
        statuses: list[dict[str, str]] = []
        for camera in config.cameras:
            if camera.id not in wanted:
                statuses.append({"id": camera.id, "state": "stopped", "message": ""})
                continue
            slot = self.slots.get(camera.id)
            if slot is None or not _running(slot["proc"]):
                if slot is not None:
                    _stop(slot["proc"])
                    self.slots.pop(camera.id, None)
                try:
                    self.slots[camera.id] = self._start(camera)
                except FileNotFoundError:
                    statuses.append({"id": camera.id, "state": "error", "message": "ffmpeg не найден"})
                    continue
                except OSError as exc:
                    statuses.append({"id": camera.id, "state": "error", "message": str(exc)})
                    continue
                slot = self.slots[camera.id]
            message = _tail(slot)
            if _running(slot["proc"]):
                statuses.append({"id": camera.id, "state": "recording", "message": message})
            else:
                statuses.append({"id": camera.id, "state": "error", "message": message or "ffmpeg остановился"})
        return statuses

    def _start(self, camera: CameraSettings) -> dict[str, Any]:
        directory = self.data_root / "video" / camera.id
        directory.mkdir(parents=True, exist_ok=True)
        proc = self.spawn(build_ffmpeg_argv(camera, directory))
        lines: list[str] = []
        stderr = getattr(proc, "stderr", None)
        if stderr is not None:
            threading.Thread(target=_capture, args=(stderr, lines), daemon=True).start()
        return {"proc": proc, "signature": _signature(camera), "lines": lines}

    def stop_all(self) -> None:
        for slot in self.slots.values():
            _stop(slot["proc"])
        self.slots.clear()


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
            del lines[:-20]
    except Exception:
        return
