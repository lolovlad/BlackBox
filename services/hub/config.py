from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HubConfig:
    db_path: Path
    data_root: Path
    jwt_secret: str
    access_ttl_seconds: int = 900
    refresh_ttl_seconds: int = 604800
    bootstrap_username: str = "admin"
    bootstrap_password: str = ""
    worker_image_rtu: str = "blackbox/worker-modbus-rtu:dev"
    worker_image_tcp: str = "blackbox/worker-modbus-tcp:dev"
    worker_image_simulator: str = "blackbox/worker-simulator:dev"
    docker_enabled: bool = True
    queue_size: int = 2048
    cookie_secure: bool = False
    telemetry_min_free_bytes: int = 64 * 1024 * 1024
    telemetry_quota_bytes: int | None = None

    @classmethod
    def from_env(cls) -> "HubConfig":
        db = Path(os.getenv("BB_HUB_DB_PATH", "instance/hub.db"))
        root = Path(os.getenv("BB_DATA_ROOT", "data"))
        return cls(
            db_path=db,
            data_root=root,
            jwt_secret=os.getenv("BB_JWT_SECRET", os.getenv("SECRET_KEY", "change-me-change-me-change-me-change-me")),
            bootstrap_username=os.getenv("BB_BOOTSTRAP_ADMIN_USERNAME", "admin"),
            bootstrap_password=os.getenv("BB_BOOTSTRAP_ADMIN_PASSWORD", "admin"),
            worker_image_rtu=os.getenv("BB_WORKER_IMAGE_RTU", cls.worker_image_rtu),
            worker_image_tcp=os.getenv("BB_WORKER_IMAGE_TCP", cls.worker_image_tcp),
            worker_image_simulator=os.getenv("BB_WORKER_IMAGE_SIMULATOR", cls.worker_image_simulator),
            docker_enabled=os.getenv("BB_DOCKER_ENABLED", "1") == "1",
            cookie_secure=os.getenv("BB_COOKIE_SECURE", "0") == "1",
            telemetry_min_free_bytes=int(os.getenv("BB_TELEMETRY_MIN_FREE_BYTES", str(64 * 1024 * 1024))),
            telemetry_quota_bytes=(int(os.getenv("BB_TELEMETRY_QUOTA_BYTES")) if os.getenv("BB_TELEMETRY_QUOTA_BYTES") else None),
        )
