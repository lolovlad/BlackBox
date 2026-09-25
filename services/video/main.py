"""Poll Hub for camera settings, episode commands and preview leases."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

from services.video.settings import CameraSettings, VideoConfig
from services.video.supervisor import Supervisor


def _request(url: str, token: str, *, method: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"X-Video-Token": token}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def fetch_work(hub_url: str, token: str) -> dict:
    return _request(f"{hub_url.rstrip('/')}/api/v1/internal/video/config", token, method="GET")


def post_status(hub_url: str, token: str, items: list[dict[str, str]]) -> None:
    _request(
        f"{hub_url.rstrip('/')}/api/v1/internal/video/status",
        token,
        method="POST",
        payload={"items": items},
    )


def post_logs(hub_url: str, token: str, items: list[dict[str, str]]) -> None:
    _request(
        f"{hub_url.rstrip('/')}/api/v1/internal/video/logs",
        token,
        method="POST",
        payload={"items": items},
    )


def post_episodes(hub_url: str, token: str, items: list[dict[str, str]]) -> None:
    _request(
        f"{hub_url.rstrip('/')}/api/v1/internal/video/episodes",
        token,
        method="POST",
        payload={"items": items},
    )


def run() -> int:
    hub_url = os.getenv("BB_HUB_URL", "http://hub:8080").rstrip("/")
    token = os.getenv("BB_VIDEO_TOKEN", "")
    root = Path(os.getenv("BB_DATA_ROOT", "/data"))
    interval = max(1.0, float(os.getenv("BB_VIDEO_POLL_SEC", "1")))
    supervisor = Supervisor(root)
    while True:
        try:
            payload = fetch_work(hub_url, token)
            config = VideoConfig.model_validate(payload.get("config") or {})
            previews: list[CameraSettings] = []
            for item in payload.get("previews") or []:
                try:
                    previews.append(CameraSettings.model_validate(item))
                except ValueError:
                    continue
            raw_root = str(payload.get("storage_dir") or "").strip()
            output_root = Path(raw_root) if raw_root else root / "video"
            result = supervisor.tick(config, list(payload.get("episodes") or []), previews, output_root=output_root)
            post_status(hub_url, token, result["statuses"])
            if result["logs"]:
                post_logs(hub_url, token, result["logs"])
            if result["episodes"]:
                post_episodes(hub_url, token, result["episodes"])
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            time.sleep(interval)
            continue
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(run())
