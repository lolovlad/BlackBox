from __future__ import annotations

import json
import os
import time
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.webui.gpio_service import GpioCollector, build_gpio_backend


def _write_heartbeat(path: Path, *, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": pid, "ts": time.time()}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    heartbeat_path = Path(os.getenv("GPIO_READER_HEARTBEAT_PATH", "instance/gpio-control/heartbeat.json"))
    stop_path = Path(os.getenv("GPIO_READER_STOP_PATH", "instance/gpio-control/stop.flag"))
    settings_path = Path(os.getenv("GPIO_SETTINGS_PATH", "settings/gpio_inputs.json"))

    db_path = os.getenv("BLACKBOX_DB_PATH", "instance/blackbox.db")
    db_file = Path(db_path).resolve()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    sf = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    backend = build_gpio_backend()
    collector = GpioCollector(sf, gpio_settings_path=settings_path.resolve(), backend=backend)

    try:
        while True:
            _write_heartbeat(heartbeat_path, pid=os.getpid())
            if stop_path.exists():
                break
            collector.poll_once()  # single step to keep heartbeat loop responsive
            time.sleep(collector.poll_interval_sec)
    finally:
        collector.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

