from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.database import Alarms, EventLog, Samples, TypeUser, User, Video, db
from src.webui.background_tasks import MaintenanceScheduler


def _write_app_runtime(base_dir: Path) -> None:
    cfg = {
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
    (base_dir / "settings" / "app_runtime.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def _write_env_file(base_dir: Path, db_path: Path, video_dir: Path | None = None) -> None:
    lines = [
        f"BLACKBOX_DB_PATH={db_path.as_posix()}",
        "DB_CLEANUP_INTERVAL_MINUTES=999999",
        "DB_RETENTION_DAYS=30",
        f"VIDEO_STORAGE_DIR={(video_dir.as_posix() if video_dir is not None else '')}",
        "VIDEO_GC_INTERVAL_DAYS=10",
        "SECRET_KEY=test-secret",
        "HOST=127.0.0.1",
        "PORT=5001",
        "FLASK_APP=src.web_app:app",
        "SESSION_COOKIE_SECURE=0",
        "SEED_ADMIN_USERNAME=admin",
        "SEED_ADMIN_PASSWORD=admin",
        "SEED_USER_USERNAME=user",
        "SEED_USER_PASSWORD=user",
        f"FLASK_INSTANCE_PATH={(base_dir / 'instance').as_posix()}",
    ]
    (base_dir / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture()
def app_with_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "settings").mkdir(parents=True, exist_ok=True)
    (tmp_path / "settings" / "settings.json").write_text('{"requests":[],"fields":[]}\n', encoding="utf-8")
    _write_app_runtime(tmp_path)
    db_path = tmp_path / "app.db"
    video_dir = tmp_path / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    _write_env_file(tmp_path, db_path, video_dir)

    from src.webui.app import create_app

    app = create_app()
    with app.app_context():
        db.drop_all()
        db.create_all()
        role = TypeUser(name="Admin", system_name="admin", description="")
        db.session.add(role)
        db.session.flush()
        db.session.add(User(username="admin", password="admin", type_user_id=role.id, is_deleted=False))
        db.session.commit()
    yield app, tmp_path


def test_create_app_fails_without_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    from src.webui.app import create_app

    with pytest.raises(RuntimeError, match="\\.env"):
        create_app()


def test_api_video_add_uses_alarms_state_intervals(app_with_env) -> None:
    app, _ = app_with_env
    with app.app_context():
        start = datetime(2026, 4, 9, 14, 0, 0)
        end = datetime(2026, 4, 9, 14, 30, 0)
        db.session.add(
            Alarms(
                created_at=start,
                date=b"{}",
                name="Low oil pressure",
                state="active",
                description="active",
            )
        )
        db.session.add(
            Alarms(
                created_at=end,
                date=b"{}",
                name="Low oil pressure",
                state="inactive",
                description="inactive",
            )
        )
        db.session.commit()

    client = app.test_client()
    ok_resp = client.post("/api/video/add", json={"path": "D:/cam/camera_(2026-04-09_14-10-00).mp4"})
    assert ok_resp.status_code == 200
    ok_data = ok_resp.get_json()
    assert ok_data["ok"] is True
    assert ok_data["alarm_name"] == "Low oil pressure"

    miss_resp = client.post("/api/video/add", json={"path": "D:/cam/camera_(2026-04-09_15-10-00).mp4"})
    assert miss_resp.status_code == 404

    with app.app_context():
        rows = Video.query.all()
        assert len(rows) == 1
        assert rows[0].alarm is not None
        assert rows[0].alarm.name == "Low oil pressure"


def test_api_video_add_fails_when_nearest_alarm_is_inactive(app_with_env) -> None:
    app, _ = app_with_env
    with app.app_context():
        db.session.add(
            Alarms(
                created_at=datetime(2026, 4, 9, 10, 0, 0),
                date=b"{}",
                name="A1",
                state="active",
                description="active",
            )
        )
        db.session.add(
            Alarms(
                created_at=datetime(2026, 4, 9, 10, 5, 0),
                date=b"{}",
                name="A1",
                state="inactive",
                description="inactive",
            )
        )
        db.session.commit()

    client = app.test_client()
    resp = client.post("/api/video/add", json={"path": "D:/cam/camera_(2026-04-09_10-06-00).mp4"})
    assert resp.status_code == 404

    with app.app_context():
        assert Video.query.count() == 0


def test_api_video_add_accepts_compact_filename_from_cam_folder(app_with_env) -> None:
    app, _ = app_with_env
    with app.app_context():
        db.session.add(
            Alarms(
                created_at=datetime(2026, 4, 9, 14, 50, 0),
                date=b"{}",
                name="Cam1 motion",
                state="active",
                description="active",
            )
        )
        db.session.commit()

    client = app.test_client()
    resp = client.post("/api/video/add", json={"path": "/mnt/nvme/motion/cam1/20260409_145117.mkv"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["captured_at"] == "2026-04-09T14:51:17"

    with app.app_context():
        row = Video.query.one()
        assert row.file_name == "20260409_145117.mkv"


def test_api_video_add_accepts_plain_text_body(app_with_env) -> None:
    app, _ = app_with_env
    with app.app_context():
        db.session.add(
            Alarms(
                created_at=datetime(2026, 4, 9, 14, 50, 0),
                date=b"{}",
                name="Cam2 motion",
                state="active",
                description="active",
            )
        )
        db.session.commit()

    client = app.test_client()
    resp = client.post("/api/video/add", data="/mnt/nvme/motion/cam2/20260409_145117.mkv", content_type="text/plain")
    assert resp.status_code == 200


def test_maintenance_cleanup_db_and_video_files(app_with_env) -> None:
    app, tmp_path = app_with_env
    env_path = tmp_path / ".env"
    session_factory = app.extensions["session_factory"]
    scheduler = MaintenanceScheduler(session_factory=session_factory, env_path=env_path)

    now = datetime.now()
    old_ts = now - timedelta(days=40)
    with app.app_context():
        db.session.add(Samples(created_at=old_ts, date=b"old"))
        db.session.add(Samples(created_at=now, date=b"new"))
        db.session.add(Alarms(created_at=old_ts, date=b"{}", name="A1", state="active", description="old"))
        db.session.add(Alarms(created_at=now, date=b"{}", name="A1", state="active", description="new"))
        db.session.add(EventLog(created_at=old_ts, level="info", code="old", message="old", payload_json="{}"))
        db.session.add(EventLog(created_at=now, level="info", code="new", message="new", payload_json="{}"))
        db.session.commit()

    scheduler._run_db_cleanup({"DB_RETENTION_DAYS": "30"})

    with app.app_context():
        assert Samples.query.count() == 1
        assert Alarms.query.count() == 1
        assert EventLog.query.count() == 1

    video_dir = tmp_path / "videos"
    keep_file = video_dir / "keep.mp4"
    stale_file = video_dir / "stale.mp4"
    keep_file.write_bytes(b"x")
    stale_file.write_bytes(b"x")
    with app.app_context():
        db.session.add(
            Video(
                captured_at=now,
                file_name=keep_file.name,
                file_path=str(keep_file),
                alarm_id=None,
            )
        )
        db.session.commit()

    scheduler._run_video_cleanup({"VIDEO_STORAGE_DIR": str(video_dir)})
    assert keep_file.exists()
    assert not stale_file.exists()
