from datetime import datetime
from pathlib import Path

from blackbox.hourly_param_csv import HourlySplitCsvWriter


def test_hourly_writer_creates_header_and_line_numbers(tmp_path: Path) -> None:
    analog_cols = ["A", "B"]
    discrete_cols = ["D1"]
    w = HourlySplitCsvWriter(tmp_path, "unit", analog_cols, discrete_cols)
    t0 = datetime(2026, 4, 6, 14, 30, 0)
    w.write_sample(t0, {"A": 1, "B": 2}, {"D1": True})
    w.write_sample(t0, {"A": 3}, {"D1": False})
    w.close()

    analog_file = tmp_path / "analogs" / "unit_2026-04-06_14.csv"
    discrete_file = tmp_path / "discretes" / "unit_2026-04-06_14.csv"
    assert analog_file.exists()
    assert discrete_file.exists()

    lines = analog_file.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "line_no,date,time,A,B"
    assert lines[1].startswith("1,2026-04-06,14:30:00")
    assert lines[2].startswith("2,2026-04-06,14:30:00")

    dlines = discrete_file.read_text(encoding="utf-8").strip().splitlines()
    assert dlines[1].endswith(",1")
    assert dlines[2].endswith(",0")
