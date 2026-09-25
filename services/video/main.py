"""Poll Hub for camera settings and keep ffmpeg processes in step with them."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

from services.video.settings import VideoConfig
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


def fetch_config(hub_url: str, token: str) -> VideoConfig:
    payload = _request(f"{hub_url.rstrip('/')}/api/v1/internal/video/config", token, method="GET")
    return VideoConfig.model_validate(payload.get("config") or payload)


def post_status(hub_url: str, token: str, items: list[dict[str, str]]) -> None:
    _request(
        f"{hub_url.rstrip('/')}/api/v1/internal/video/status",
        token,
        method="POST",
        payload={"items": items},
    )


def run() -> int:
    hub_url = os.getenv("BB_HUB_URL", "http://hub:8080").rstrip("/")
    token = os.getenv("BB_VIDEO_TOKEN", "")
    root = Path(os.getenv("BB_DATA_ROOT", "/data"))
    interval = max(1.0, float(os.getenv("BB_VIDEO_POLL_SEC", "3")))
    supervisor = Supervisor(root)
    while True:
        try:
            config = fetch_config(hub_url, token)
            post_status(hub_url, token, supervisor.reconcile(config))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            time.sleep(interval)
            continue
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(run())
