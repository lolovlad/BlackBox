from datetime import datetime, timedelta
import json
from pathlib import Path

from blackbox.config import DataLoggerConfig, DataFormat
from blackbox.data_writer import DataWriter, AlarmWriter


def _base_config(tmp_path: Path) -> DataLoggerConfig:
    return DataLoggerConfig(
        data_directory=str(tmp_path / "data"),
        alarm_directory=str(tmp_path / "alarms"),
        backup_directory=str(tmp_path / "backup"),
        log_directory=str(tmp_path / "logs"),
        fsync_on_write=False,  # ускорим тесты
    )


def test_data_writer_creates_daily_csv(tmp_path):
    cfg = _base_config(tmp_path)
    cfg.data_format = DataFormat.CSV
    writer = DataWriter(cfg)

    ts = datetime.now()
    writer.write_data(ts, {0: True}, {0: 1.23, 3: 220.0})
    writer.close()

    date_dir = Path(cfg.data_directory) / ts.strftime("%Y-%m-%d")
    files = list(date_dir.glob("data.csv"))
    assert files, "Файл с данными не был создан"

    content = files[0].read_text(encoding="utf-8").strip().splitlines()
    # Первая строка — заголовок, вторая — данные
    assert len(content) >= 2


def test_data_writer_backup_when_main_unavailable(tmp_path):
    cfg = _base_config(tmp_path)
    cfg.data_format = DataFormat.JSON
    writer = DataWriter(cfg)

    # Имитируем недоступность основного файла
    ts = datetime.now()
    # При write_data() вызывается _check_date_change(): если _current_date != today,
    # DataWriter откроет основной файл и ветка backup не сработает.
    writer._current_date = ts.strftime("%Y-%m-%d")
    writer._current_file = None
    writer.write_data(ts, {0: True}, {0: 1.0})

    backup_file = Path(cfg.backup_directory) / "backup_data.jsonl"
    assert backup_file.exists(), "Резервный файл не был создан"
    data = backup_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(data) == 1
    obj = json.loads(data[0])
    assert obj["discrete"]["0"] is True


def test_alarm_writer_saves_alarm_csv(tmp_path):
    cfg = _base_config(tmp_path)
    cfg.data_format = DataFormat.CSV
    cfg.alarm_pre_time = 5
    cfg.alarm_post_time = 5

    writer = AlarmWriter(cfg)

    base_time = datetime.now()
    # Данные до события
    for i in range(3):
        writer.add_data_point(base_time - timedelta(seconds=6 - i), {0: False}, {0: 1.0})

    # Начало события
    start_time = base_time
    writer.start_alarm("TEST_ALARM", start_time)

    # Данные после события
    for i in range(3):
        writer.add_data_point(base_time + timedelta(seconds=i), {0: True}, {0: 2.0})

    writer.finish_alarm("TEST_ALARM")

    alarm_files = list(Path(cfg.alarm_directory).glob("alarm_TEST_ALARM_*.csv"))
    assert alarm_files, "Файл аварийного события не создан"

