"""Run preview and episode ffmpeg processes. An episode ends when its process exits."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.video.devices import h264_encoder_present
from services.video.settings import (
    CameraSettings,
    VideoConfig,
    build_episode_argv,
    build_buffer_argv,
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
    def __init__(
        self,
        data_root: Path,
        spawn: Callable[[list[str]], Any] | None = None,
        hardware_h264: Callable[[], bool] | None = None,
    ) -> None:
        self.data_root = data_root
        self.spawn = spawn or _popen
        self._hardware_h264 = hardware_h264 or h264_encoder_present
        self._hw_h264: bool | None = None
        self.output_root = data_root / "video"
        self.episodes: dict[str, dict[str, Any]] = {}
        self.incident_episodes: dict[str, dict[str, Any]] = {}
        self.buffers: dict[str, dict[str, Any]] = {}
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
        manual_jobs = [item for item in episodes if not item.get("incident_id")]
        self._sync_episodes(config, manual_jobs, events)
        manual_recording = {str(slot["camera_id"]) for slot in self.episodes.values() if _running(slot["proc"])}
        self._sync_previews(previews, manual_recording | {str(item.get("camera_id")) for item in episodes if item.get("incident_id")})
        self._sync_buffers(config, set())
        self._sync_incident_episodes(config, episodes, events)
        recording = manual_recording | {
            str(slot["camera_id"])
            for slot in self.incident_episodes.values()
            if not slot.get("finished")
        }
        self._drain_slots()
        self._collect_buffer_logs()
        return {"statuses": self._statuses(config, previews, recording), "episodes": events, "logs": self._take_logs()}

    def stop_all(self) -> None:
        for slot in list(self.episodes.values()) + list(self.previews.values()) + list(self.buffers.values()):
            _stop(slot["proc"])
        self.episodes.clear()
        self.incident_episodes.clear()
        self.buffers.clear()
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
            if not episode_id or state not in {"queued", "recording", "stopping"}:
                continue
            if item.get("incident_id"):
                continue
            if episode_id in self.closed:
                if episode_id not in emitted:
                    events.append(dict(self.closed[episode_id]))
                continue
            due = _due(item.get("stop_at"))
            if episode_id in self.episodes:
                slot = self.episodes[episode_id]
                if state == "stopping" or due:
                    _stop(slot["proc"])
                elif state == "queued":
                    events.append(_recording_event(episode_id, slot))
                continue
            if state == "stopping" or due:
                event = {
                    "id": episode_id,
                    "state": "error",
                    "path": str(item.get("path") or ""),
                    "message": "инцидент завершился до запуска записи",
                    "started_at": str(item.get("started_at") or ""),
                    "ended_at": _now(),
                }
                self._close(episode_id, event)
                events.append(event)
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
            encode_as = self._encoder(camera)
            # Incident episodes are stopped by the Hub at stop_at. A long
            # upper bound prevents a lost stop command from creating a
            # permanently running ffmpeg process.
            duration = item.get("duration_sec")
            argv = build_episode_argv(encode_as, path, duration_sec=duration)
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

    def _sync_buffers(self, config: VideoConfig, suspended: set[str]) -> None:
        wanted = {
            camera.id: camera
            for camera in config.cameras
            if camera.enabled and camera.url and camera.id not in suspended
        }
        segment_sec = config.incident_segment_sec
        for camera_id in list(self.buffers):
            slot = self.buffers[camera_id]
            camera = wanted.get(camera_id)
            signature = f"{_preview_signature(camera)}|segment:{segment_sec}" if camera is not None else ""
            if camera is None or signature != slot["signature"] or not _running(slot["proc"]):
                self._drain_slot(slot)
                _stop(slot["proc"])
                self.buffers.pop(camera_id, None)
        for camera_id, camera in wanted.items():
            if camera_id in self.buffers:
                continue
            directory = self.output_root / ".buffer" / camera_id
            pattern = directory / "%Y%m%d_%H%M%S.mkv"
            argv = build_buffer_argv(camera, pattern, segment_sec)
            signature = f"{_preview_signature(camera)}|segment:{segment_sec}"
            try:
                directory.mkdir(parents=True, exist_ok=True)
                proc = self.spawn(argv)
            except OSError as exc:
                self.preview_errors[camera_id] = "ffmpeg не найден" if isinstance(exc, FileNotFoundError) else str(exc)
                self._note(camera_id, f"Не удалось запустить кольцевой буфер: {self.preview_errors[camera_id]}", level="error", source="hub")
                continue
            self.buffers[camera_id] = {
                "proc": proc,
                "signature": signature,
                "camera_id": camera_id,
                "directory": directory,
                "lines": _watch(proc),
                "sent": 0,
                "segment_sec": segment_sec,
                "log_source": "ffmpeg-buffer",
            }
            keep = max(10, config.incident_pre_sec + segment_sec * 2 + 5)
            self._note(
                camera_id,
                f"Кольцевой буфер запущен: последние {keep} с, кусками по {segment_sec} с. Старые куски удаляются.",
                level="info",
                source="hub",
            )

    def _sync_incident_episodes(
        self,
        config: VideoConfig,
        episodes: list[dict[str, Any]],
        events: list[dict[str, str]],
    ) -> None:
        cameras = {camera.id: camera for camera in config.cameras}
        now = datetime.now(timezone.utc).timestamp()
        segment_sec = max(1, config.incident_segment_sec)
        active_ids: set[str] = set()
        for item in episodes:
            episode_id = str(item.get("id") or "")
            if not episode_id or not item.get("incident_id") or item.get("state") not in {"queued", "recording"}:
                continue
            if episode_id in self.closed:
                events.append(dict(self.closed[episode_id]))
                continue
            camera_id = str(item.get("camera_id") or "")
            camera = cameras.get(camera_id)
            if camera is None or not camera.enabled or not camera.url:
                if episode_id not in self.closed:
                    event = {
                        "id": episode_id,
                        "state": "error",
                        "path": "",
                        "paths": [],
                        "message": "камера недоступна",
                        "started_at": str(item.get("started_at") or ""),
                        "ended_at": _now(),
                    }
                    self._close(episode_id, event)
                    events.append(event)
                continue
            active_ids.add(episode_id)
            slot = self.incident_episodes.get(episode_id)
            incident_id = str(item.get("incident_id") or "")
            output_dir = self.output_root / "incidents" / incident_id / camera_id
            output_dir.mkdir(parents=True, exist_ok=True)
            known = set(str(path) for path in item.get("paths") or [])
            if slot is None:
                slot = {
                    "camera_id": camera_id,
                    "incident_id": incident_id,
                    "path": output_dir,
                    "paths": known,
                    "capture_from": str(item.get("capture_from") or item.get("started_at") or _now()),
                    "started_at": str(item.get("started_at") or _now()),
                    "stop_at": item.get("stop_at"),
                    "finished": False,
                }
                self.incident_episodes[episode_id] = slot
            else:
                slot["stop_at"] = item.get("stop_at")
                slot["paths"].update(known)

            buffer = self.buffers.get(camera_id)
            source_dir = Path(buffer["directory"]) if buffer is not None else None
            before_count = len(slot["paths"])
            if source_dir is not None:
                capture_from = _timestamp(slot["capture_from"])
                # Ignore the still-open segment. Its modification time moves
                # until ffmpeg closes it at the next keyframe boundary.
                for source in sorted(source_dir.glob("*.mkv")):
                    try:
                        modified = source.stat().st_mtime
                    except OSError:
                        continue
                    if modified > now - segment_sec - 0.25:
                        continue
                    if capture_from is not None and modified < capture_from - segment_sec:
                        continue
                    target = output_dir / source.name
                    if str(target) in slot["paths"]:
                        continue
                    try:
                        shutil.copy2(source, target)
                    except OSError:
                        continue
                    slot["paths"].add(str(target))

            stop_at = _timestamp(slot.get("stop_at"))
            due = stop_at is not None and now >= stop_at + segment_sec + 0.5
            changed = len(slot["paths"]) != before_count
            if item.get("state") == "queued" or changed:
                paths = sorted(slot["paths"])
                events.append(
                    {
                        "id": episode_id,
                        "state": "recording",
                        "path": paths[0] if paths else str(output_dir),
                        "paths": paths,
                        "message": "",
                        "started_at": slot["started_at"],
                        "ended_at": "",
                    }
                )
            if due:
                paths = sorted(slot["paths"])
                if paths:
                    event = {
                        "id": episode_id,
                        "state": "finished",
                        "path": paths[0],
                        "paths": paths,
                        "message": "",
                        "started_at": slot["started_at"],
                        "ended_at": _now(),
                    }
                else:
                    event = {
                        "id": episode_id,
                        "state": "error",
                        "path": "",
                        "paths": [],
                        "message": "нет готовых видеосегментов",
                        "started_at": slot["started_at"],
                        "ended_at": _now(),
                    }
                self._close(episode_id, event)
                self.incident_episodes.pop(episode_id, None)
                events.append(event)

        retention = max(10, config.incident_pre_sec + segment_sec * 2 + 5)
        for buffer in self.buffers.values():
            directory = Path(buffer["directory"])
            for segment in directory.glob("*.mkv"):
                try:
                    if segment.stat().st_mtime < now - retention:
                        segment.unlink(missing_ok=True)
                except OSError:
                    continue

    def _collect_buffer_logs(self) -> None:
        for slot in self.buffers.values():
            self._drain_slot(slot)

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
                slot = next((item for item in self.episodes.values() if item["camera_id"] == camera.id and _running(item["proc"])), None)
                rows.append({"id": camera.id, "state": "recording", "message": _tail(slot) if slot is not None else "Идёт запись инцидента"})
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

    def _encoder(self, camera: CameraSettings) -> CameraSettings:
        if camera.codec != "h264_v4l2m2m" or self._hardware_ready():
            return camera
        self._note(
            camera.id,
            "Аппаратный кодер H.264 не найден, эпизод пишется через libx264",
            level="error",
            source="hub",
        )
        return camera.model_copy(update={"codec": "libx264"})

    def _hardware_ready(self) -> bool:
        if self._hw_h264 is None:
            try:
                self._hw_h264 = bool(self._hardware_h264())
            except OSError:
                self._hw_h264 = False
        return self._hw_h264

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
            if source == "ffmpeg-buffer" and _routine_buffer_line(line):
                continue
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


def _due(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment <= datetime.now(timezone.utc)


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


def _routine_buffer_line(line: str) -> bool:
    text = str(line or "").strip()
    if text.startswith("frame="):
        return True
    return "[segment " in text and "Opening " in text and text.endswith("for writing")


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
