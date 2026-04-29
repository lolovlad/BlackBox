import time
from pathlib import Path

from blackbox.config import DataLoggerConfig, AlarmCondition
from blackbox.data_logger import DataLogger


def _cfg(tmp_path: Path) -> DataLoggerConfig:
    return DataLoggerConfig(
        data_directory=str(tmp_path / "data"),
        alarm_directory=str(tmp_path / "alarms"),
        backup_directory=str(tmp_path / "backup"),
        log_directory=str(tmp_path / "logs"),
        analog_poll_interval=0.05,
        fsync_on_write=False,  # иначе осфскрн на каждом цикле может сильно тормозить/зависать
    )


def test_datalogger_start_stop_and_logging(tmp_path):
    cfg = _cfg(tmp_path)
    logger = DataLogger(cfg)

    logger.start()
    assert logger.is_running() is True

    # Немного подождем, чтобы прошли циклы опроса
    time.sleep(0.2)
    logger.stop()
    assert logger.is_running() is False

    # Проверим, что создался каталог логов
    log_dir = Path(cfg.log_directory)
    assert log_dir.exists()
    assert any(log_dir.iterdir()), "Файлы логов не созданы"


def test_datalogger_triggers_alarm_and_creates_alarm_file(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.alarm_pre_time = 1
    cfg.alarm_post_time = 1
    # Важно: аварийный файл создаётся только после "post_time" (AlarmWriter.finish_alarm),
    # поэтому в тесте нужно подождать достаточно времени, иначе запись может не успеть.
    wait_after_set_discrete_sec = 1.6

    dl = DataLogger(cfg)

    # Простое условие аварии — дискретный вход 0 True
    cond = AlarmCondition(
        name="TEST_ALARM",
        discrete_inputs=[0],
        discrete_condition=lambda d: d.get(0, False) is True,
    )
    dl.add_alarm_condition(cond)

    dl.start()
    # Зададим дискретный вход, чтобы спровоцировать аварию
    dl.set_discrete_value(0, True)

    time.sleep(wait_after_set_discrete_sec)
    dl.stop()

    alarm_dir = Path(cfg.alarm_directory)
    files = list(alarm_dir.glob("alarm_TEST_ALARM_*.csv")) + list(
        alarm_dir.glob("alarm_TEST_ALARM_*.json")
    )
    assert files, "Аварийный файл не создан"

