"""Фильтры / сортировка / пагинация данных и HTTP /data/tables."""

from __future__ import annotations

import json
import re
from datetime import datetime

import pytest
from werkzeug.datastructures import MultiDict

from src.database import Samples, TypeUser, User, db
from src.webui.modbus_service import pack_snapshot, parse_fields
from src.webui.repositories.data_repository import DataRepository
from src.webui.services.data_service import (
    TABLE_PAGE_SIZE,
    DataFilter,
    DataService,
    parse_data_filter,
)


def test_parse_data_filter_sort_desc_default() -> None:
    flt = parse_data_filter(MultiDict([("active_tab", "analog")]))
    assert flt.sort_desc is True


def test_parse_data_filter_sort_asc() -> None:
    flt = parse_data_filter(MultiDict([("active_tab", "analog"), ("sort", "asc")]))
    assert flt.sort_desc is False


def test_parse_data_filter_page_default_and_positive() -> None:
    flt = parse_data_filter(MultiDict([("active_tab", "analog")]))
    assert flt.page == 1
    flt2 = parse_data_filter(MultiDict([("active_tab", "analog"), ("page", "3")]))
    assert flt2.page == 3
    flt3 = parse_data_filter(MultiDict([("active_tab", "analog"), ("page", "0")]))
    assert flt3.page == 1


def test_same_calendar_day_date_to_becomes_end_of_day() -> None:
    """Один и тот же день 00:00 в «по» → верхняя граница 23:59:59.999999 (иначе выборка пустая)."""
    flt = parse_data_filter(
        MultiDict(
            [
                ("active_tab", "analog"),
                ("date_from", "07.04.2026 00:00"),
                ("date_to", "07.04.2026 00:00"),
            ]
        )
    )
    assert flt.date_from == datetime(2026, 4, 7, 0, 0, 0)
    assert flt.date_to == datetime(2026, 4, 7, 23, 59, 59, 999999)


def test_date_from_after_date_to_gets_swapped() -> None:
    flt = parse_data_filter(
        MultiDict(
            [
                ("active_tab", "analog"),
                ("date_from", "10.04.2026 12:00"),
                ("date_to", "01.04.2026 00:00"),
            ]
        )
    )
    assert flt.date_from <= flt.date_to


def test_parse_russian_date_only() -> None:
    flt = parse_data_filter(
        MultiDict([("active_tab", "analog"), ("date_from", "15.03.2026"), ("date_to", "16.03.2026")])
    )
    assert flt.date_from == datetime(2026, 3, 15, 0, 0, 0)
    assert flt.date_to == datetime(2026, 3, 16, 23, 59, 59, 999999)


def test_parse_iso_datetime_still_supported() -> None:
    flt = parse_data_filter(
        MultiDict([("active_tab", "analog"), ("date_from", "2026-05-01T08:30:00")])
    )
    assert flt.date_from == datetime(2026, 5, 1, 8, 30, 0)


def test_table_page_size_constant() -> None:
    assert TABLE_PAGE_SIZE == 1000


def _minimal_blob() -> bytes:
    return pack_snapshot({"hr": [0] * 90, "coils": [False] * 32})


@pytest.fixture()
def memory_app(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "settings").mkdir(parents=True, exist_ok=True)
    (tmp_path / "settings" / "settings.json").write_text(
        '{"requests":[{"name":"hr","fc":3,"address":0,"count":90}],'
        '"fields":[{"name":"r0","type":"uint16","source":"hr","address":0}]}\n',
        encoding="utf-8",
    )
    runtime = {
        "modbus_port": "COM1",
        "modbus_slave": 1,
        "modbus_baudrate": 9600,
        "modbus_timeout": 0.35,
        "modbus_interval": 0.12,
        "modbus_address_offset": 1,
        "ram_batch_size": 60,
        "app_timezone": "UTC",
        "parser_settings_path": "settings/settings.json",
        "disable_modbus_collector": True,
    }
    (tmp_path / "settings" / "app_runtime.json").write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")
    env_lines = [
        f"BLACKBOX_DB_PATH={(tmp_path / 'webtest.db').as_posix()}",
        "DB_CLEANUP_INTERVAL_MINUTES=60",
        "DB_RETENTION_DAYS=30",
        "VIDEO_STORAGE_DIR=",
        "VIDEO_GC_INTERVAL_DAYS=10",
        "SECRET_KEY=test",
        "HOST=127.0.0.1",
        "PORT=5000",
        "FLASK_APP=src.web_app:app",
        "SESSION_COOKIE_SECURE=0",
        "SEED_ADMIN_USERNAME=admin",
        "SEED_ADMIN_PASSWORD=admin",
        "SEED_USER_USERNAME=user",
        "SEED_USER_PASSWORD=user",
    ]
    (tmp_path / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    from src.webui.app import create_app

    app = create_app()
    with app.app_context():
        db.drop_all()
        db.create_all()
        role = TypeUser(name="Admin", system_name="admin", description="")
        db.session.add(role)
        db.session.flush()
        db.session.add(
            User(
                username="testadmin",
                password="secret",
                type_user_id=role.id,
                is_deleted=False,
            )
        )
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        t1 = datetime(2026, 1, 2, 12, 0, 0)
        t2 = datetime(2026, 1, 3, 12, 0, 0)
        blob = _minimal_blob()
        for ts in (t0, t1, t2):
            db.session.add(Samples(created_at=ts, date=blob))
        db.session.commit()
    yield app


def test_repository_order_desc_and_count(memory_app) -> None:
    sf = memory_app.extensions["session_factory"]
    repo = DataRepository(sf)
    assert repo.count_analogs() == 3
    rows = repo.list_analogs(sort_desc=True, offset=0, limit=10)
    assert [r.created_at for r in rows] == sorted([r.created_at for r in rows], reverse=True)
    assert rows[0].created_at == datetime(2026, 1, 3, 12, 0, 0)


def test_repository_pagination_offset(memory_app) -> None:
    sf = memory_app.extensions["session_factory"]
    repo = DataRepository(sf)
    asc = repo.list_analogs(sort_desc=False, offset=0, limit=1)
    assert len(asc) == 1
    assert asc[0].created_at == datetime(2026, 1, 1, 12, 0, 0)
    second = repo.list_analogs(sort_desc=False, offset=1, limit=1)
    assert second[0].created_at == datetime(2026, 1, 2, 12, 0, 0)


def test_data_service_collect_tab_respects_sort_and_page(memory_app) -> None:
    sf = memory_app.extensions["session_factory"]
    repo = DataRepository(sf)
    svc = DataService(repo, alarms_enabled=False)
    flt = DataFilter(
        active_tab="analog",
        date_from=None,
        date_to=None,
        sort_desc=True,
        analog_columns=parse_data_filter(MultiDict([("active_tab", "analog")])).analog_columns,
        discrete_columns=[],
        page=1,
    )
    ctx = svc.collect_tab(flt)
    assert ctx["total_rows"] == 3
    assert ctx["total_pages"] == 1
    assert ctx["rows"][0]["time"].startswith("03.01.2026")


def _csrf_from_login_page(html: str) -> str:
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert m, "csrf_token not found in login page"
    return m.group(1)


def test_http_data_tables_requires_login(memory_app) -> None:
    client = memory_app.test_client()
    r = client.get("/data/tables?active_tab=analog")
    assert r.status_code in (302, 401)


def test_http_data_tables_returns_rows_for_admin(memory_app) -> None:
    client = memory_app.test_client()
    lg = client.get("/login")
    assert lg.status_code == 200
    token = _csrf_from_login_page(lg.get_data(as_text=True))
    post = client.post(
        "/login",
        data={"csrf_token": token, "username": "testadmin", "password": "secret"},
        follow_redirects=False,
    )
    assert post.status_code in (302, 200)
    r = client.get("/data/tables?active_tab=analog&sort=desc&page=1")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "03.01.2026" in body
    assert "data-table-result" in body
    assert "Всего записей: 3" in body


def test_parse_fields_int16_signed_temperature_raw() -> None:
    cfg = {
        "requests": [{"name": "hr", "fc": 3, "address": 1, "count": 2}],
        "fields": [
            {"name": "PT100_2", "source": "hr", "address": 0, "type": "int16"},
            {"name": "warm", "source": "hr", "address": 1, "type": "int16"},
        ],
    }
    out = parse_fields(cfg, {"hr": [0xFFF6, 850]})
    assert out["PT100_2"] == -10
    assert out["warm"] == 850
