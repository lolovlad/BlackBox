"""Camera recording settings and the ffmpeg command they produce.

The Hub stores this document. The video process only supervises one ffmpeg
per enabled camera; it does not decode frames itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RESOLUTIONS = {
    "source": None,
    "3840x2160": (3840, 2160),
    "1920x1080": (1920, 1080),
    "1280x720": (1280, 720),
    "640x480": (640, 480),
}
CODECS = ("libx264", "libx265", "mjpeg", "copy", "h264_v4l2m2m")
PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium")


class CameraSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    url: str = ""
    stream: Literal["main", "sub"] = "main"
    rtsp_transport: Literal["tcp", "udp"] = "tcp"
    resolution: str = "source"
    width: int | None = Field(default=None, ge=160, le=7680)
    height: int | None = Field(default=None, ge=120, le=4320)
    fps: int | None = Field(default=None, ge=1, le=60)
    codec: Literal["libx264", "libx265", "mjpeg", "copy", "h264_v4l2m2m"] = "libx264"
    profile: Literal["baseline", "main", "high"] = "high"
    preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"] = "ultrafast"
    bitrate_kbps: int = Field(default=2000, ge=64, le=50000)
    gop_sec: int = Field(default=2, ge=1, le=30)
    audio: Literal["none", "aac", "copy"] = "none"
    audio_bitrate_kbps: int = Field(default=128, ge=32, le=512)
    container: Literal["mp4", "mkv"] = "mp4"
    segment_sec: int = Field(default=60, ge=5, le=3600)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        text = str(value or "").strip()
        if text and not (text.startswith("rtsp://") or text.startswith("rtsps://")):
            raise ValueError("URL камеры должен начинаться с rtsp:// или rtsps://")
        return text

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("Укажите имя камеры")
        return text

    @model_validator(mode="after")
    def _apply_resolution(self) -> "CameraSettings":
        if self.resolution == "custom":
            if not self.width or not self.height:
                raise ValueError("Для своего разрешения нужны ширина и высота")
            return self
        if self.resolution not in RESOLUTIONS:
            raise ValueError("Неизвестное разрешение")
        preset = RESOLUTIONS[self.resolution]
        if preset is None:
            self.width = None
            self.height = None
        else:
            self.width, self.height = preset
        return self


class VideoConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cameras: list[CameraSettings] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _unique_ids(self) -> "VideoConfig":
        seen: set[str] = set()
        for camera in self.cameras:
            if camera.id in seen:
                raise ValueError(f"Повтор идентификатора камеры {camera.id}")
            seen.add(camera.id)
        return self


def default_config() -> dict:
    return VideoConfig().model_dump(mode="json")


def audio_kbps(camera: CameraSettings) -> int:
    if camera.audio == "none":
        return 0
    return int(camera.audio_bitrate_kbps)


def estimate_mib(video_kbps: int, extra_kbps: int, seconds: float) -> float:
    """MiB = (video_kbps + audio_kbps) * seconds / 8 / 1024."""
    return (int(video_kbps) + int(extra_kbps)) * float(seconds) / 8 / 1024


def _label(mib: float) -> str:
    if mib >= 1024:
        return f"{mib / 1024:.1f} ГиБ"
    return f"{mib:.1f} МиБ"


def camera_estimate(camera: CameraSettings) -> dict:
    extra = audio_kbps(camera)
    fragment = estimate_mib(camera.bitrate_kbps, extra, camera.segment_sec)
    hour = estimate_mib(camera.bitrate_kbps, extra, 3600)
    day = estimate_mib(camera.bitrate_kbps, extra, 86400)
    return {
        "id": camera.id,
        "enabled": camera.enabled,
        "copy": camera.codec == "copy" or camera.audio == "copy",
        "fragment_mib": round(fragment, 2),
        "hour_mib": round(hour, 2),
        "day_mib": round(day, 2),
        "fragment": _label(fragment),
        "hour": _label(hour),
        "day": _label(day),
    }


def config_estimates(config: VideoConfig) -> dict:
    rows = [camera_estimate(camera) for camera in config.cameras if camera.enabled]
    fragment = sum(row["fragment_mib"] for row in rows)
    hour = sum(row["hour_mib"] for row in rows)
    day = sum(row["day_mib"] for row in rows)
    return {
        "cameras": [camera_estimate(camera) for camera in config.cameras],
        "total": {
            "fragment_mib": round(fragment, 2),
            "hour_mib": round(hour, 2),
            "day_mib": round(day, 2),
            "fragment": _label(fragment),
            "hour": _label(hour),
            "day": _label(day),
            "copy": any(row["copy"] for row in rows),
        },
    }


def build_ffmpeg_argv(camera: CameraSettings, output_dir: Path) -> list[str]:
    ext = "mkv" if camera.container == "mkv" else "mp4"
    segment_format = "matroska" if camera.container == "mkv" else "mp4"
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        camera.rtsp_transport,
        "-i",
        camera.url,
    ]
    if camera.codec == "copy":
        argv.extend(["-c:v", "copy"])
    else:
        filters: list[str] = []
        if camera.width and camera.height:
            filters.append(f"scale={camera.width}:{camera.height}")
        if camera.fps:
            filters.append(f"fps={camera.fps}")
        if filters:
            argv.extend(["-vf", ",".join(filters)])
        argv.extend(["-c:v", camera.codec])
        if camera.codec in {"libx264", "libx265"}:
            argv.extend(["-preset", camera.preset])
        if camera.codec == "libx264":
            argv.extend(["-profile:v", camera.profile])
        bitrate = f"{camera.bitrate_kbps}k"
        argv.extend(["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", f"{camera.bitrate_kbps * 2}k"])
        if camera.fps:
            argv.extend(["-g", str(max(1, int(camera.fps) * int(camera.gop_sec)))])
        else:
            argv.extend(["-force_key_frames", f"expr:gte(t,n_forced*{int(camera.gop_sec)})"])
    if camera.audio == "none":
        argv.append("-an")
    elif camera.audio == "copy":
        argv.extend(["-c:a", "copy"])
    else:
        argv.extend(["-c:a", "aac", "-b:a", f"{camera.audio_bitrate_kbps}k"])
    argv.extend(
        [
            "-f",
            "segment",
            "-segment_time",
            str(camera.segment_sec),
            "-segment_format",
            segment_format,
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            str(output_dir / f"%Y%m%d_%H%M%S.{ext}"),
        ]
    )
    return argv
