from __future__ import annotations

import json
from pathlib import Path

from src.webui.modbus_service import (
    ANALOG_CSV_COLUMNS,
    DISCRETE_CSV_COLUMNS,
    analog_discrete_for_csv,
    create_export_csv,
    decode_to_processed,
)
from src.webui.repositories.data_repository import DataRepository


class DataService:
    def __init__(self, repository: DataRepository, alarms_enabled: bool) -> None:
        self._repo = repository
        self._alarms_enabled = alarms_enabled

    def collect_tables(self, limit: int = 100):
        analog_rows = self._repo.list_analogs(limit)
        discrete_rows = self._repo.list_discretes(limit)
        alarm_rows = self._repo.list_alarms(limit) if self._alarms_enabled else []

        analog_table = []
        for item in analog_rows:
            processed = decode_to_processed(item.date)
            analog, _ = analog_discrete_for_csv(processed)
            analog_table.append({"created_at": item.created_at.strftime("%Y-%m-%d %H:%M:%S"), "values": [analog.get(c, "") for c in ANALOG_CSV_COLUMNS]})

        discrete_table = []
        for item in discrete_rows:
            processed = decode_to_processed(item.date)
            _, discrete = analog_discrete_for_csv(processed)
            discrete_table.append({"created_at": item.created_at.strftime("%Y-%m-%d %H:%M:%S"), "values": [1 if bool(discrete.get(c, False)) else 0 for c in DISCRETE_CSV_COLUMNS]})

        alarms_table = []
        for item in alarm_rows:
            try:
                payload = json.loads(item.date.decode("utf-8"))
            except Exception:
                payload = {}
            alarms_table.append(
                {
                    "created_at": item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "name": item.name,
                    "description": item.description or "",
                    "payload": json.dumps(payload, ensure_ascii=False),
                }
            )
        return analog_table, discrete_table, alarms_table

    def build_export(self, session_factory, static_csv_dir: Path) -> Path:
        return create_export_csv(session_factory, static_csv_dir)
