"""Show the host V4L2 nodes to ffmpeg and detect a Pi H.264 encoder."""

from __future__ import annotations

import ctypes
import os
import stat
from pathlib import Path

# linux/videodev2.h. Query reads a 104-byte v4l2_capability. Enum writes a 64-byte v4l2_fmtdesc.
_VIDIOC_QUERYCAP = 0x80685600
_VIDIOC_ENUM_FMT = 0xC0405602
_V4L2_CAP_VIDEO_M2M = 0x00004000
_V4L2_CAP_VIDEO_M2M_MPLANE = 0x00008000
_V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
_V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE = 9
_V4L2_PIX_FMT_H264 = 0x34363248


class _Capability(ctypes.Structure):
    _fields_ = [
        ("driver", ctypes.c_char * 16),
        ("card", ctypes.c_char * 32),
        ("bus_info", ctypes.c_char * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


class _Format(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("description", ctypes.c_char * 32),
        ("pixelformat", ctypes.c_uint32),
        ("mbus_code", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


def publish_host_video_devices(host: Path = Path("/host-dev"), dest_dir: Path = Path("/dev")) -> int:
    """Link host /dev/videoN into the container so ffmpeg can open the encoder."""
    if not host.is_dir():
        return 0
    made = 0
    for node in sorted(host.iterdir()):
        suffix = node.name.removeprefix("video")
        if not node.name.startswith("video") or not suffix.isdigit():
            continue
        try:
            mode = node.stat().st_mode
        except OSError:
            continue
        if not stat.S_ISCHR(mode):
            continue
        dest = dest_dir / node.name
        if dest.exists() or dest.is_symlink():
            continue
        try:
            dest.symlink_to(node)
        except OSError:
            continue
        made += 1
    return made


def h264_encoder_present(dev_dir: Path | str = "/dev") -> bool:
    """True when a V4L2 node can encode H.264. A capture-only camera does not count."""
    root = Path(dev_dir)
    if not root.is_dir():
        return False
    for node in sorted(root.iterdir()):
        suffix = node.name.removeprefix("video")
        if not node.name.startswith("video") or not suffix.isdigit():
            continue
        try:
            fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            if _encodes_h264(fd):
                return True
        finally:
            os.close(fd)
    return False


def _encodes_h264(fd: int) -> bool:
    import fcntl

    cap = _Capability()
    try:
        fcntl.ioctl(fd, _VIDIOC_QUERYCAP, cap)
    except OSError:
        return False
    caps = int(cap.device_caps or cap.capabilities)
    if not caps & (_V4L2_CAP_VIDEO_M2M | _V4L2_CAP_VIDEO_M2M_MPLANE):
        return False
    for buf_type in (_V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, _V4L2_BUF_TYPE_VIDEO_CAPTURE):
        for index in range(32):
            desc = _Format(index=index, type=buf_type)
            try:
                fcntl.ioctl(fd, _VIDIOC_ENUM_FMT, desc)
            except OSError:
                break
            if int(desc.pixelformat) == _V4L2_PIX_FMT_H264:
                return True
    return False
