from __future__ import annotations

from pathlib import Path

from services.video.settings import CameraSettings, VideoConfig, build_ffmpeg_argv, camera_estimate, estimate_mib
from services.video.supervisor import Supervisor
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
    copied = build_ffmpeg_argv(_camera(codec="copy", fps=25), tmp_path)
    assert "-vf" not in copied
    assert "scale" not in " ".join(copied)
    assert copied[copied.index("-c:v") + 1] == "copy"
    assert copied[copied.index("-segment_time") + 1] == "60"

    encoded = build_ffmpeg_argv(_camera(codec="libx264", fps=25), tmp_path)
    encoded_text = " ".join(encoded)
    assert "scale=1920:1080" in encoded_text
    assert "2000k" in encoded
    assert encoded[encoded.index("-c:v") + 1] == "libx264"
    assert encoded[encoded.index("-segment_time") + 1] == "60"
    assert str(tmp_path / "%Y%m%d_%H%M%S.mp4") in encoded


class _Proc:
    def __init__(self):
        self.alive = True
        self.stderr = None

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        self.alive = False


def test_supervisor_starts_only_enabled_cameras_and_restarts_on_change(tmp_path: Path):
    started: list[_Proc] = []

    def spawn(argv):
        proc = _Proc()
        started.append(proc)
        return proc

    supervisor = Supervisor(tmp_path, spawn=spawn)
    assert supervisor.reconcile(VideoConfig()) == []
    assert started == []

    disabled = _camera(enabled=False)
    stopped = supervisor.reconcile(VideoConfig(cameras=[disabled]))
    assert started == []
    assert stopped == [{"id": "cam1", "state": "stopped", "message": ""}]
    assert supervisor.reconcile(VideoConfig(cameras=[_camera(url="")]))[0]["state"] == "stopped"
    assert started == []

    camera = _camera()
    first = supervisor.reconcile(VideoConfig(cameras=[camera]))
    assert len(started) == 1
    assert first[0]["state"] == "recording"
    supervisor.reconcile(VideoConfig(cameras=[camera]))
    assert len(started) == 1

    changed = _camera(bitrate_kbps=4000)
    supervisor.reconcile(VideoConfig(cameras=[changed]))
    assert len(started) == 2
    assert started[0].alive is False
    assert started[1].alive is True


def test_camera_settings_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_VIDEO_TOKEN", "video-secret")
    with _client(tmp_path) as client:
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 200
        page = client.get("/cameras")
        assert page.status_code == 200
        assert "Число камер" in page.text
        assert "/static/cameras.js" in page.text

        csrf = client.cookies.get("bb_csrf")
        body = {"cameras": [_camera().model_dump(mode="json")]}
        saved = client.put("/api/v1/cameras", json=body, headers={"X-CSRF-Token": csrf})
        assert saved.status_code == 200
        payload = saved.json()
        assert payload["config"]["cameras"][0]["resolution"] == "1920x1080"
        assert payload["config"]["cameras"][0]["bitrate_kbps"] == 2000
        assert payload["estimates"]["cameras"][0]["fragment_mib"] == round(2000 * 60 / 8 / 1024, 2)

        loaded = client.get("/api/v1/cameras")
        assert loaded.json()["config"]["cameras"][0]["codec"] == "libx264"

        assert client.get("/api/v1/internal/video/config").status_code == 401
        internal = client.get("/api/v1/internal/video/config", headers={"X-Video-Token": "video-secret"})
        assert internal.status_code == 200
        assert internal.json()["config"]["cameras"][0]["id"] == "cam1"

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
