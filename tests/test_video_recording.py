from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import zipfile

import pytest
from pydantic import ValidationError

from services.video.devices import h264_encoder_present, publish_host_video_devices
from services.video.settings import (
    CameraSettings,
    VideoConfig,
    build_buffer_argv,
    build_episode_argv,
    build_preview_argv,
    camera_estimate,
    estimate_mib,
)
from services.video.supervisor import Supervisor
from services.hub.db import HubRepository
from tests.test_hub_vnext import _client


def _camera(**overrides) -> CameraSettings:
    payload = {
        "id": "cam1",
        "name": "Вход",
        "enabled": True,
        "url": "rtsp://10.0.0.8/stream",
        "resolution": "1920x1080",
        "bitrate_kbps": 2000,
        "audio": "none",
        "segment_sec": 60,
    }
    payload.update(overrides)
    return CameraSettings.model_validate(payload)


def test_fragment_size_matches_bitrate_formula():
    camera = _camera()
    expected = estimate_mib(2000, 0, 60)
    assert expected == 2000 * 60 / 8 / 1024
    estimate = camera_estimate(camera)
    assert estimate["fragment_mib"] == round(expected, 2)
    assert estimate["hour_mib"] == round(estimate_mib(2000, 0, 3600), 2)
    assert estimate["day_mib"] == round(estimate_mib(2000, 0, 86400), 2)


def test_ffmpeg_argv_copy_skips_scale_and_libx264_limits_bitrate(tmp_path: Path):
    copied = build_episode_argv(_camera(codec="copy", fps=25), tmp_path / "clip.mp4")
    assert "-vf" not in copied
    assert "scale" not in " ".join(copied)
    assert "segment" not in copied
    assert copied[copied.index("-c:v") + 1] == "copy"
    assert copied[copied.index("-t") + 1] == "60"
    assert copied[-1].endswith("clip.mp4")

    encoded = build_episode_argv(_camera(codec="libx264", fps=25), tmp_path / "clip.mp4")
    encoded_text = " ".join(encoded)
    assert "scale=1920:1080" in encoded_text
    assert "2000k" in encoded
    assert encoded[encoded.index("-c:v") + 1] == "libx264"
    assert encoded[encoded.index("-t") + 1] == "60"
    assert encoded[-1].endswith("clip.mp4")

    hardware = build_episode_argv(_camera(codec="h264_v4l2m2m", fps=25), tmp_path / "clip.mp4")
    hardware_text = " ".join(hardware)
    assert hardware[hardware.index("-c:v") + 1] == "h264_v4l2m2m"
    assert "format=yuv420p" in hardware_text
    assert "-maxrate" not in hardware
    assert "-preset" not in hardware

    buffer = build_buffer_argv(_camera(), tmp_path / "%Y%m%d_%H%M%S.mkv", 2)
    assert buffer[buffer.index("-f") + 1] == "segment"
    assert buffer[buffer.index("-segment_time") + 1] == "2"
    assert buffer[-1].endswith("%Y%m%d_%H%M%S.mkv")

    preview = build_preview_argv(_camera(), tmp_path / "live.jpg")
    assert "scale=1920:1080" in " ".join(preview)
    assert preview[1] == "-y"
    assert preview[preview.index("-f") + 1] == "image2"
    plain = build_preview_argv(_camera(codec="copy"), tmp_path / "live.jpg")
    assert "scale=" not in " ".join(plain)


def test_storage_picks_a_resource_and_a_folder():
    assert VideoConfig().storage_resource_id == "storage:data"
    assert VideoConfig(video_subdir="").video_subdir == "video"
    assert VideoConfig(storage_resource_id="storage:nvme0n1p1", video_subdir="clips").video_subdir == "clips"
    with pytest.raises(ValidationError):
        VideoConfig(storage_resource_id="nvme")
    with pytest.raises(ValidationError):
        VideoConfig(video_subdir="../etc")


class _Proc:
    def __init__(self, argv: list[str]):
        self.argv = argv
        self.alive = True
        self.code = 0
        self.stderr = None

    def poll(self):
        return None if self.alive else self.code

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        self.alive = False


def test_supervisor_records_one_episode_and_reports_when_it_ends(tmp_path: Path):
    started: list[_Proc] = []

    def spawn(argv):
        proc = _Proc(list(argv))
        started.append(proc)
        return proc

    supervisor = Supervisor(tmp_path, spawn=spawn)
    camera = _camera(url="rtsp://user:secret@10.0.0.8/stream")
    config = VideoConfig(cameras=[camera])
    assert supervisor.tick(config, [], [])["statuses"] == [{"id": "cam1", "state": "stopped", "message": ""}]
    assert len(started) == 1 and "segment" in " ".join(started[0].argv)
    assert supervisor.tick(VideoConfig(cameras=[_camera(enabled=False)]), [{"id": "ep0", "camera_id": "cam1", "state": "queued"}], [])["episodes"][0]["state"] == "error"
    assert len(started) == 1

    archive = tmp_path / "archive"
    job = {"id": "ep1", "camera_id": "cam1", "state": "queued"}
    first = supervisor.tick(config, [job], [camera], output_root=archive)
    assert len(started) == 3
    episode_proc = next(proc for proc in started if "-t" in proc.argv)
    assert "segment" not in episode_proc.argv
    assert str(archive) in episode_proc.argv[-1] or str(archive).replace("\\", "/") in episode_proc.argv[-1].replace("\\", "/")
    assert first["episodes"][0]["state"] == "recording"
    assert first["statuses"][0]["state"] == "recording"
    assert supervisor.previews == {}
    assert any("Эпизод ep1 запущен" in item["line"] and item["source"] == "hub" for item in first["logs"])
    assert "user:secret" not in " ".join(item["line"] for item in first["logs"])

    supervisor.episodes["ep1"]["lines"].append("Nothing was written into output file, because at least one of its streams received no packets.")
    logged = supervisor.tick(config, [{"id": "ep1", "camera_id": "cam1", "state": "recording"}], [])
    assert any(item["level"] == "error" and "Nothing was written" in item["line"] for item in logged["logs"])

    again = supervisor.tick(config, [{"id": "ep1", "camera_id": "cam1", "state": "recording"}], [])
    assert again["episodes"] == []
    assert len(started) == 3

    episode_proc.alive = False
    episode_proc.code = 0
    done = supervisor.tick(config, [{"id": "ep1", "camera_id": "cam1", "state": "recording"}], [])
    assert done["episodes"][0]["state"] == "finished"
    assert done["episodes"][0]["path"].endswith("ep1.mp4")
    assert any("Эпизод ep1 завершён" in item["line"] for item in done["logs"])

    lost = supervisor.tick(config, [{"id": "ep9", "camera_id": "cam1", "state": "recording"}], [])
    assert lost["episodes"][0]["state"] == "error"
    assert lost["episodes"][0]["message"] == "запись прервана"


def test_missing_pi_encoder_records_with_libx264(tmp_path: Path):
    started: list[_Proc] = []

    def spawn(argv):
        proc = _Proc(list(argv))
        started.append(proc)
        return proc

    supervisor = Supervisor(tmp_path, spawn=spawn, hardware_h264=lambda: False)
    camera = _camera(codec="h264_v4l2m2m")
    result = supervisor.tick(VideoConfig(cameras=[camera]), [{"id": "ep1", "camera_id": "cam1", "state": "queued"}], [])
    episode = next(proc for proc in started if "-c:v" in proc.argv)
    assert episode.argv[episode.argv.index("-c:v") + 1] == "libx264"
    assert "h264_v4l2m2m" not in episode.argv
    assert any("Аппаратный кодер H.264 не найден" in item["line"] and item["source"] == "hub" for item in result["logs"])

    ready = Supervisor(tmp_path, spawn=spawn, hardware_h264=lambda: True)
    again = ready.tick(VideoConfig(cameras=[camera]), [{"id": "ep2", "camera_id": "cam1", "state": "queued"}], [])
    hardware_episode = [proc for proc in started if "-c:v" in proc.argv][-1]
    assert hardware_episode.argv[hardware_episode.argv.index("-c:v") + 1] == "h264_v4l2m2m"
    assert "format=yuv420p" in " ".join(hardware_episode.argv)
    assert not any("не найден" in item["line"] for item in again["logs"])


def test_video_device_publish_ignores_ordinary_files(tmp_path: Path):
    host = tmp_path / "host"
    dest = tmp_path / "dev"
    host.mkdir()
    dest.mkdir()
    (host / "video0").write_text("not a device", encoding="utf-8")
    (host / "sda").write_text("disk", encoding="utf-8")
    assert publish_host_video_devices(host, dest) == 0
    assert list(dest.iterdir()) == []
    assert h264_encoder_present(tmp_path / "missing") is False


def test_preview_restarts_when_the_picture_settings_change(tmp_path: Path):
    started: list[_Proc] = []

    def spawn(argv):
        proc = _Proc(list(argv))
        started.append(proc)
        return proc

    supervisor = Supervisor(tmp_path, spawn=spawn)
    camera = _camera()
    config = VideoConfig(cameras=[camera])
    first = supervisor.tick(config, [], [camera])
    assert first["statuses"][0]["state"] == "preview"
    assert len(started) == 2
    preview = next(proc for proc in started if "image2" in proc.argv)
    assert preview.argv[preview.argv.index("-f") + 1] == "image2"
    supervisor.tick(config, [], [camera])
    assert len(started) == 2

    changed = _camera(resolution="640x480")
    supervisor.tick(config, [], [changed])
    assert len(started) == 3
    assert preview.alive is False
    assert any("scale=640:480" in " ".join(proc.argv) and "image2" in proc.argv for proc in started[2:])


def test_alarm_edges_merge_into_one_incident_and_schedule_video_stop(tmp_path: Path):
    repo = HubRepository(tmp_path / "hub.db")
    first = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    incident = repo.register_incident_start(
        "vm-1",
        name="Oil pressure",
        kind="alert",
        created_at=first,
        camera_ids=["cam1", "cam2"],
        post_seconds=15,
    )
    incident_id = incident["id"]
    extended = repo.register_incident_start(
        "vm-1",
        name="Temperature",
        kind="alert",
        created_at=first + timedelta(seconds=3),
        camera_ids=["cam1", "cam2"],
        post_seconds=15,
    )
    assert extended["id"] == incident_id
    assert len(extended["episodes"]) == 2
    repo.register_incident_end(
        "vm-1",
        name="Oil pressure",
        kind="alert",
        created_at=first + timedelta(seconds=5),
        post_seconds=15,
    )
    still_open = repo.get_incident(incident_id)
    assert still_open["state"] == "finishing"
    assert still_open["stop_at"].endswith("+00:00")
    assert all(item["duration_sec"] == 86400 for item in still_open["episodes"])
    assert all(item["state"] == "queued" for item in still_open["episodes"])


def test_incident_episode_updates_keep_all_segment_paths(tmp_path: Path):
    repo = HubRepository(tmp_path / "hub.db")
    moment = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    incident = repo.register_incident_start("vm-1", name="Alarm", kind="alert", created_at=moment, camera_ids=["cam1"])
    episode = incident["episodes"][0]
    paths = [str(tmp_path / "segment-1.mkv"), str(tmp_path / "segment-2.mkv")]
    repo.apply_episode_events([{"id": episode["id"], "state": "recording", "paths": paths, "started_at": moment.isoformat()}])
    saved = repo.get_incident(incident["id"])["episodes"][0]
    assert saved["paths"] == paths


def test_incident_pages_and_export_routes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_VIDEO_TOKEN", "video-secret")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        assert client.get("/incidents").status_code == 200
        missing = client.get("/incidents/not-found", follow_redirects=False)
        assert missing.status_code == 303
        assert client.get("/api/v1/telemetry/export-package").status_code == 422
        workbook = client.get("/api/v1/telemetry/export-package", params={
            "date_from": "2026-09-28T00:00", "date_to": "2026-09-28T23:59", "format": "xlsx", "include": "analog",
        })
        assert workbook.status_code == 200
        with zipfile.ZipFile(io.BytesIO(workbook.content)) as package:
            assert "xl/workbook.xml" in package.namelist()


def test_incident_download_and_bundle_contain_video(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_VIDEO_TOKEN", "video-secret")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        repo = client.app.state.repo
        moment = datetime.now(timezone.utc).replace(microsecond=0)
        incident = repo.register_incident_start(
            "vm-test", name="Alarm", kind="alert", created_at=moment, camera_ids=["cam1"]
        )
        episode = incident["episodes"][0]
        video_path = tmp_path / "data" / "video" / "incidents" / incident["id"] / "cam1" / "part.mkv"
        video_path.parent.mkdir(parents=True)
        video_path.write_bytes(b"video-data")
        repo.apply_episode_events([{
            "id": episode["id"], "state": "finished", "path": str(video_path), "paths": [str(video_path)],
            "started_at": moment.isoformat(), "ended_at": moment.isoformat(),
        }])
        response = client.get(f"/api/v1/video/incidents/{incident['id']}/videos/{episode['id']}/0")
        assert response.status_code == 200
        assert response.content == b"video-data"
        archive = client.get(f"/api/v1/video/incidents/{incident['id']}/export")
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
            assert f"videos/cam1/0001_{video_path.name}" in bundle.namelist()
            assert "telemetry/analog.csv" in bundle.namelist()
            assert "charts/analog.json" in bundle.namelist()


def test_supervisor_stops_incident_episode_at_stop_at(tmp_path: Path):
    started: list[_Proc] = []

    def spawn(argv):
        proc = _Proc(list(argv))
        started.append(proc)
        return proc

    supervisor = Supervisor(tmp_path, spawn=spawn, hardware_h264=lambda: False)
    camera = _camera()
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    first = supervisor.tick(
        VideoConfig(cameras=[camera]),
        [{"id": "ep1", "camera_id": "cam1", "state": "queued", "duration_sec": 86400, "stop_at": future}],
        [],
    )
    assert first["episodes"][0]["state"] == "recording"
    episode_proc = next(proc for proc in started if "-t" in proc.argv)
    assert episode_proc.argv[episode_proc.argv.index("-t") + 1] == "86400"
    due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    supervisor.tick(
        VideoConfig(cameras=[camera]),
        [{"id": "ep1", "camera_id": "cam1", "state": "recording", "duration_sec": 86400, "stop_at": due}],
        [],
    )
    assert episode_proc.alive is False


def test_camera_settings_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_VIDEO_TOKEN", "video-secret")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        page = client.get("/cameras")
        assert page.status_code == 200
        assert "Добавить камеру" in page.text
        assert 'data-field="storage_resource_id"' in page.text
        assert "Носитель" in page.text
        assert "Журнал" in page.text
        assert "bb-vm-step" in page.text
        assert "/static/cameras.js" in page.text

        csrf = client.cookies.get("bb_csrf")
        disk = tmp_path / "nvme"
        disk.mkdir()
        client.app.state.repo.upsert_resources(
            [
                {
                    "resource_id": "storage:nvme0n1p1",
                    "kind": "storage",
                    "name": "NVMe SSD",
                    "path": str(disk),
                    "available": True,
                    "metadata": {},
                }
            ]
        )
        assert client.app.state.repo.approve_resource("storage:nvme0n1p1", None)
        body = {"storage_resource_id": "storage:nvme0n1p1", "video_subdir": "clips", "cameras": [_camera().model_dump(mode="json")]}
        saved = client.put("/api/v1/cameras", json=body, headers={"X-CSRF-Token": csrf})
        assert saved.status_code == 200
        payload = saved.json()
        assert payload["config"]["storage_resource_id"] == "storage:nvme0n1p1"
        assert payload["config"]["video_subdir"] == "clips"
        assert Path(payload["storage_dir"]) == disk / "clips"
        assert any(item["resource_id"] == "storage:nvme0n1p1" for item in payload["storage_resources"])
        assert payload["config"]["cameras"][0]["resolution"] == "1920x1080"
        assert payload["config"]["cameras"][0]["bitrate_kbps"] == 2000
        assert payload["estimates"]["cameras"][0]["fragment_mib"] == round(2000 * 60 / 8 / 1024, 2)
        assert client.put("/api/v1/cameras", json={"storage_resource_id": "storage:missing", "cameras": []}, headers={"X-CSRF-Token": csrf}).status_code == 409
        assert client.put("/api/v1/cameras", json={"video_subdir": "../etc", "cameras": []}, headers={"X-CSRF-Token": csrf}).status_code == 422

        loaded = client.get("/api/v1/cameras")
        assert loaded.json()["config"]["cameras"][0]["codec"] == "libx264"

        assert client.get("/api/v1/internal/video/config").status_code == 401
        internal = client.get("/api/v1/internal/video/config", headers={"X-Video-Token": "video-secret"})
        assert internal.status_code == 200
        assert internal.json()["config"]["cameras"][0]["id"] == "cam1"
        assert Path(internal.json()["storage_dir"]) == disk / "clips"
        assert internal.json()["episodes"] == []

        started = client.post("/api/v1/cameras/cam1/episodes", headers={"X-CSRF-Token": csrf})
        assert started.status_code == 200
        episode_id = started.json()["id"]
        assert started.json()["state"] == "queued"
        assert client.post("/api/v1/cameras/cam1/episodes", headers={"X-CSRF-Token": csrf}).status_code == 409
        queued = client.get("/api/v1/internal/video/config", headers={"X-Video-Token": "video-secret"})
        assert queued.json()["episodes"][0]["id"] == episode_id

        recording = client.post(
            "/api/v1/internal/video/episodes",
            headers={"X-Video-Token": "video-secret"},
            json={"items": [{"id": episode_id, "state": "recording", "path": "/mnt/nvme/video/cam1/clip.mp4", "started_at": "2026-09-25T12:00:00+00:00"}]},
        )
        assert recording.status_code == 200
        assert recording.json()["items"][0]["state"] == "recording"
        repeat = client.post(
            "/api/v1/internal/video/episodes",
            headers={"X-Video-Token": "video-secret"},
            json={"items": [{"id": episode_id, "state": "recording", "path": "/mnt/nvme/video/cam1/clip.mp4", "started_at": "2026-09-25T12:00:00+00:00"}]},
        )
        assert repeat.json()["items"] == []
        finished = client.post(
            "/api/v1/internal/video/episodes",
            headers={"X-Video-Token": "video-secret"},
            json={"items": [{"id": episode_id, "state": "finished", "path": "/mnt/nvme/video/cam1/clip.mp4", "started_at": "2026-09-25T12:00:00+00:00", "ended_at": "2026-09-25T12:01:00+00:00"}]},
        )
        assert finished.status_code == 200
        assert finished.json()["items"][0]["state"] == "finished"
        assert finished.json()["items"][0]["path"].endswith("clip.mp4")
        listed = client.get("/api/v1/cameras").json()["episodes"]
        assert listed[0]["id"] == episode_id
        assert listed[0]["state"] == "finished"
        assert client.get("/api/v1/internal/video/config", headers={"X-Video-Token": "video-secret"}).json()["episodes"] == []
        assert client.post("/api/v1/internal/video/episodes", json={"items": []}).status_code == 401
        assert client.post("/api/v1/internal/video/logs", json={"items": []}).status_code == 401
        written = client.post(
            "/api/v1/internal/video/logs",
            headers={"X-Video-Token": "video-secret"},
            json={"items": [{"camera_id": "cam1", "level": "error", "source": "ffmpeg", "line": "Nothing was written into output file"}]},
        )
        assert written.status_code == 200
        journal = client.get("/api/v1/cameras/cam1/logs")
        assert journal.status_code == 200
        assert journal.json()["entries"][-1]["line"] == "Nothing was written into output file"
        assert journal.json()["entries"][-1]["level"] == "error"
        assert journal.json()["entries"][-1]["source"] == "ffmpeg"

        watched = client.post(
            "/api/v1/cameras/cam1/preview",
            headers={"X-CSRF-Token": csrf},
            json={"active": True, "camera": _camera().model_dump(mode="json")},
        )
        assert watched.status_code == 200
        previewing = client.get("/api/v1/internal/video/config", headers={"X-Video-Token": "video-secret"})
        assert previewing.json()["previews"][0]["url"] == "rtsp://10.0.0.8/stream"
        assert client.get("/api/v1/cameras/cam1/preview.jpg").status_code == 404
        jpeg = tmp_path / "data" / "video" / ".preview" / "cam1.jpg"
        jpeg.parent.mkdir(parents=True)
        jpeg.write_bytes(b"\xff\xd8\xff\xd9")
        image = client.get("/api/v1/cameras/cam1/preview.jpg")
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/jpeg")
        assert client.post("/api/v1/cameras/cam1/preview", headers={"X-CSRF-Token": csrf}, json={"active": False}).status_code == 200
        assert client.get("/api/v1/internal/video/config", headers={"X-Video-Token": "video-secret"}).json()["previews"] == []

        posted = client.post(
            "/api/v1/internal/video/status",
            headers={"X-Video-Token": "video-secret"},
            json={"items": [{"id": "cam1", "state": "error", "message": "connection refused"}]},
        )
        assert posted.status_code == 200
        assert client.get("/api/v1/cameras").json()["status"]["cam1"]["message"] == "connection refused"

        bad = _camera().model_dump(mode="json")
        bad["url"] = "http://not-rtsp"
        rejected = client.put("/api/v1/cameras", json={"cameras": [bad]}, headers={"X-CSRF-Token": csrf})
        assert rejected.status_code == 422
