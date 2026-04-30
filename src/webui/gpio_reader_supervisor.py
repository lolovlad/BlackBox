from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GpioReaderStatus:
    running: bool
    pid: int | None
    last_heartbeat_at: float | None
    last_heartbeat_age_sec: float | None


class GpioReaderSupervisor:
    """Управляет отдельным subprocess для GPIO-ридера (Raspberry)."""

    def __init__(self, *, instance_dir: Path, project_root: Path, gpio_settings_path: Path) -> None:
        self._instance_dir = instance_dir
        self._project_root = project_root
        self._gpio_settings_path = gpio_settings_path
        self._control_dir = instance_dir / "gpio-control"
        self._control_dir.mkdir(parents=True, exist_ok=True)
        self._heartbeat_path = self._control_dir / "heartbeat.json"
        self._stop_path = self._control_dir / "stop.flag"
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            if self._stop_path.exists():
                self._stop_path.unlink(missing_ok=True)
            env = os.environ.copy()
            env.update(
                {
                    "GPIO_READER_HEARTBEAT_PATH": str(self._heartbeat_path),
                    "GPIO_READER_STOP_PATH": str(self._stop_path),
                    "GPIO_SETTINGS_PATH": str(self._gpio_settings_path),
                }
            )
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "src.webui.gpio_reader_subprocess"],
                cwd=str(self._project_root),
                env=env,
            )

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            self._stop_path.write_text("stop\n", encoding="utf-8")
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.2)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._proc = None

    def restart(self, *, gpio_settings_path: Path | None = None) -> None:
        if gpio_settings_path is not None:
            self._gpio_settings_path = gpio_settings_path
        self.stop()
        self.start()

    def status(self) -> GpioReaderStatus:
        with self._lock:
            proc = self._proc
            running = proc is not None and proc.poll() is None
            pid = proc.pid if running else None
        last_heartbeat_at: float | None = None
        if self._heartbeat_path.exists():
            try:
                payload = json.loads(self._heartbeat_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    raw = payload.get("ts")
                    if isinstance(raw, (int, float)):
                        last_heartbeat_at = float(raw)
            except Exception:
                last_heartbeat_at = None
        age = None if last_heartbeat_at is None else max(0.0, time.time() - last_heartbeat_at)
        return GpioReaderStatus(
            running=running,
            pid=pid,
            last_heartbeat_at=last_heartbeat_at,
            last_heartbeat_age_sec=age,
        )

