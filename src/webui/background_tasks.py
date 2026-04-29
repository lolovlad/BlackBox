from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from src.database import Alarms, EventLog, Samples, Video
from src.webui.system_settings import read_env_file

logger = logging.getLogger(__name__)


def _as_int(raw: str | None, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(str(raw).strip())
    except Exception:
        return default
    return value if value >= minimum else default


class MaintenanceScheduler:
    def __init__(self, *, session_factory: sessionmaker, env_path: Path) -> None:
        self._session_factory = session_factory
        self._env_path = env_path
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_db_cleanup_at: float = 0.0
        self._last_video_cleanup_at: float = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="maintenance-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            env = read_env_file(self._env_path)
            now = time.monotonic()

            db_interval_min = _as_int(env.get("DB_CLEANUP_INTERVAL_MINUTES"), 60)
            video_interval_days = _as_int(env.get("VIDEO_GC_INTERVAL_DAYS"), 10)

            if (now - self._last_db_cleanup_at) >= db_interval_min * 60:
                self._run_db_cleanup(env)
                self._last_db_cleanup_at = now

            if (now - self._last_video_cleanup_at) >= video_interval_days * 24 * 60 * 60:
                self._run_video_cleanup(env)
                self._last_video_cleanup_at = now

            self._stop_event.wait(timeout=5.0)

    def _run_db_cleanup(self, env: dict[str, str]) -> None:
        retention_days = _as_int(env.get("DB_RETENTION_DAYS"), 30)
        threshold = datetime.now() - timedelta(days=retention_days)
        session = self._session_factory()
        try:
            samples_deleted = session.execute(delete(Samples).where(Samples.created_at < threshold)).rowcount or 0
            alarms_deleted = session.execute(delete(Alarms).where(Alarms.created_at < threshold)).rowcount or 0
            logs_deleted = session.execute(delete(EventLog).where(EventLog.created_at < threshold)).rowcount or 0
            session.commit()
            logger.info(
                "DB cleanup done: retention_days=%d samples=%d alarms=%d event_logs=%d",
                retention_days,
                samples_deleted,
                alarms_deleted,
                logs_deleted,
            )
        except Exception:
            session.rollback()
            logger.exception("DB cleanup failed")
        finally:
            session.close()

    def _run_video_cleanup(self, env: dict[str, str]) -> None:
        root_dir_raw = (env.get("VIDEO_STORAGE_DIR") or "").strip()
        if not root_dir_raw:
            logger.info("Video cleanup skipped: VIDEO_STORAGE_DIR is not set")
            return
        root_dir = Path(root_dir_raw).expanduser()
        if not root_dir.exists() or not root_dir.is_dir():
            logger.warning("Video cleanup skipped: invalid VIDEO_STORAGE_DIR=%s", root_dir)
            return

        scan_roots: list[Path] = [root_dir]
        # Частый кейс: путь задан как /.../motion/cam1, но камеры лежат в соседних camN.
        # Тогда очищаем по родителю, чтобы не пропускать устаревшие файлы в cam2/cam3/...
        if re.fullmatch(r"cam\d+", root_dir.name.lower()) and root_dir.parent.is_dir():
            scan_roots.append(root_dir.parent)
        scan_roots = list(dict.fromkeys(p.resolve() for p in scan_roots))

        session = self._session_factory()
        try:
            try:
                rows = session.execute(select(Video.file_path)).scalars().all()
                keep_paths = {Path(p).resolve() for p in rows}
            except Exception as exc:  # noqa: BLE001
                # Тесты могут стартовать раньше создания миграций/таблиц.
                if "no such table" in str(exc).lower() or "videos" in str(exc).lower():
                    logger.info("Video cleanup skipped: missing 'videos' table")
                    return
                raise
        finally:
            session.close()

        deleted = 0
        scanned_files = 0
        seen: set[Path] = set()
        for scan_root in scan_roots:
            for file in scan_root.rglob("*"):
                if self._stop_event.is_set():
                    break
                if not file.is_file():
                    continue
                try:
                    resolved = file.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    scanned_files += 1
                    if resolved not in keep_paths:
                        file.unlink(missing_ok=True)
                        deleted += 1
                except Exception:
                    logger.exception("Failed to remove stale video file: %s", file)
            if self._stop_event.is_set():
                break
        logger.info(
            "Video cleanup done: roots=%s scanned=%d deleted=%d",
            ", ".join(str(p) for p in scan_roots),
            scanned_files,
            deleted,
        )
