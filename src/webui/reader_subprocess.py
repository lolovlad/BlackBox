from __future__ import annotations

import json
import os
import time
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from src.webui.modbus_service import ModbusCollector, RuntimeConfig, configure_settings_path, reload_settings_cache


def _build_runtime_config() -> RuntimeConfig:
    static_csv_dir = Path(os.getenv("READER_STATIC_CSV_DIR", "src/static/csv"))
    static_csv_dir.mkdir(parents=True, exist_ok=True)
    return RuntimeConfig(
        db_path=os.getenv("BLACKBOX_DB_PATH", "instance/blackbox.db"),
        modbus_port=os.getenv("MODBUS_PORT", "/dev/ttyAMA0"),
        modbus_slave=int(os.getenv("MODBUS_SLAVE", "1")),
        modbus_baudrate=int(os.getenv("MODBUS_BAUDRATE", "9600")),
        modbus_timeout=float(os.getenv("MODBUS_TIMEOUT", "0.35")),
        modbus_interval=float(os.getenv("MODBUS_INTERVAL", "0.12")),
        address_offset=int(os.getenv("MODBUS_ADDRESS_OFFSET", "1")),
        ram_batch_size=int(os.getenv("RAM_BATCH_SIZE", "60")),
        static_csv_dir=static_csv_dir,
    )


def _write_heartbeat(path: Path, *, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": pid, "ts": time.time()}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    heartbeat_path = Path(os.getenv("READER_HEARTBEAT_PATH", "instance/reader-control/heartbeat.json"))
    stop_path = Path(os.getenv("READER_STOP_PATH", "instance/reader-control/stop.flag"))
    configure_settings_path(os.getenv("PARSER_SETTINGS_PATH", "settings/settings.json"))
    cfg_settings = reload_settings_cache()
    try:
        reqs = cfg_settings.get("requests", []) if isinstance(cfg_settings, dict) else []
        req_summary = ", ".join(
            f"{r.get('name')}[fc={r.get('fc')},addr={r.get('address')},count={r.get('count')}]"
            for r in reqs
            if isinstance(r, dict)
        )
        print(f"Reader snapshot format=BBX1 requests={req_summary}")
    except Exception:
        print("Reader snapshot format=BBX1")
    cfg = _build_runtime_config()
    db_file = Path(cfg.db_path).resolve()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    sf = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    alarms_enabled = os.getenv("READER_ALARMS_ENABLED", "1") == "1"
    try:
        alarms_enabled = alarms_enabled and inspect(engine).has_table("alarms")
    except Exception:
        alarms_enabled = False

    collector = ModbusCollector(sf, cfg, alarms_enabled=alarms_enabled)
    collector.start()
    try:
        while True:
            _write_heartbeat(heartbeat_path, pid=os.getpid())
            if stop_path.exists():
                break
            time.sleep(1.0)
    finally:
        collector.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

