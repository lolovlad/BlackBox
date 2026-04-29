from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from src.webui.modbus_service import RuntimeConfig


@dataclass
class ReaderStatus:
    running: bool
    pid: int | None
    last_heartbeat_at: float | None
    last_heartbeat_age_sec: float | None


class ReaderSupervisor:
    """Управляет отдельным subprocess для Modbus-ридера."""

    def __init__(self, *, runtime: RuntimeConfig, alarms_enabled: bool, instance_dir: Path, project_root: Path) -> None:
        self._config = runtime
        self._alarms_enabled = alarms_enabled
        self._instance_dir = instance_dir
        self._project_root = project_root
        self._control_dir = instance_dir / "reader-control"
        self._control_dir.mkdir(parents=True, exist_ok=True)
        self._heartbeat_path = self._control_dir / "heartbeat.json"
        self._stop_path = self._control_dir / "stop.flag"
        self._log_path = self._control_dir / "reader.log"
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._log_file = None

    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            if self._stop_path.exists():
                self._stop_path.unlink(missing_ok=True)
            env = os.environ.copy()
            env.update(
                {
                    "READER_HEARTBEAT_PATH": str(self._heartbeat_path),
                    "READER_STOP_PATH": str(self._stop_path),
                    "READER_STATIC_CSV_DIR": str(self._config.static_csv_dir),
                    "READER_ALARMS_ENABLED": "1" if self._alarms_enabled else "0",
                    # Чтобы лог в файле был “живым” (не буферизовался).
                    "PYTHONUNBUFFERED": "1",
                }
            )
            # Пишем stdout/stderr сабпроцесса в файл, иначе падения при старте “тихие”.
            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_file = open(self._log_path, "a", encoding="utf-8", buffering=1)
            except OSError:
                self._log_file = None
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "src.webui.reader_subprocess"],
                cwd=str(self._project_root),
                env=env,
                stdout=self._log_file or subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            # Если процесс завершился сразу — это обычно ImportError/ошибка окружения.
            time.sleep(0.05)
            if self._proc.poll() is not None and self._log_file is not None:
                try:
                    self._log_file.write(f"\n[supervisor] reader exited early with code={self._proc.returncode}\n")
                    self._log_file.flush()
                except Exception:
                    pass

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
            if self._log_file is not None:
                try:
                    self._log_file.close()
                except Exception:
                    pass
                self._log_file = None

    def restart(self, new_config: RuntimeConfig | None = None) -> None:
        if new_config is not None:
            self._config = new_config
        self.stop()
        self.start()

    def status(self) -> ReaderStatus:
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
        return ReaderStatus(
            running=running,
            pid=pid,
            last_heartbeat_at=last_heartbeat_at,
            last_heartbeat_age_sec=age,
        )

