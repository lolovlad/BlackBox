"""
Почасовые CSV: отдельные файлы для analogs и discretes.
Имя файла: {prefix}_{YYYY-MM-DD}_{HH}.csv
Строка: номер_строки, дата, время, значения колонок.
"""
import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


class HourlySplitCsvWriter:
    def __init__(
        self,
        base_dir: Path,
        file_prefix: str,
        analog_columns: List[str],
        discrete_columns: List[str],
    ) -> None:
        self.base_dir = Path(base_dir)
        self.file_prefix = file_prefix
        self.analog_columns = list(analog_columns)
        self.discrete_columns = list(discrete_columns)
        self._hour_key: Optional[str] = None
        self._line_no: Dict[str, int] = {"analogs": 0, "discretes": 0}
        self._files: Dict[str, Any] = {}
        self._writers: Dict[str, csv.writer] = {}

    def _close_files(self) -> None:
        for f in self._files.values():
            if f is not None and not f.closed:
                f.close()
        self._files.clear()
        self._writers.clear()

    def close(self) -> None:
        self._close_files()
        self._hour_key = None

    def _ensure_hour(self, dt: datetime) -> None:
        hour_key = dt.strftime("%Y-%m-%d_%H")
        if hour_key == self._hour_key and self._files.get("analogs") and self._files.get("discretes"):
            return
        self._close_files()
        self._hour_key = hour_key
        self._line_no = {"analogs": 0, "discretes": 0}
        date_part = dt.strftime("%Y-%m-%d")
        hour_part = dt.strftime("%H")
        for category, columns in (
            ("analogs", self.analog_columns),
            ("discretes", self.discrete_columns),
        ):
            subdir = self.base_dir / category
            subdir.mkdir(parents=True, exist_ok=True)
            filename = f"{self.file_prefix}_{date_part}_{hour_part}.csv"
            path = subdir / filename
            file_exists = path.exists()
            f = open(path, "a", newline="", encoding="utf-8")
            writer = csv.writer(f, delimiter=",")
            if not file_exists:
                writer.writerow(["line_no", "date", "time", *columns])
            self._files[category] = f
            self._writers[category] = writer

    def write_sample(
        self,
        dt: datetime,
        analog_values: Mapping[str, Any],
        discrete_values: Mapping[str, Any],
    ) -> None:
        self._ensure_hour(dt)
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M:%S.%f")[:12]

        self._line_no["analogs"] += 1
        a_row: List[Any] = [self._line_no["analogs"], date_str, time_str]
        for key in self.analog_columns:
            a_row.append(analog_values.get(key, ""))
        self._writers["analogs"].writerow(a_row)

        self._line_no["discretes"] += 1
        d_row: List[Any] = [self._line_no["discretes"], date_str, time_str]
        for key in self.discrete_columns:
            v = discrete_values.get(key, False)
            if isinstance(v, bool):
                d_row.append(1 if v else 0)
            else:
                d_row.append(v)
        self._writers["discretes"].writerow(d_row)

        for f in self._files.values():
            if f:
                f.flush()
