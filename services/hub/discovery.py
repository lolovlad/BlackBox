"""Discover physical I/O that protocol workers can actually open."""

from __future__ import annotations

import glob
import os
import re
import shutil
import socket
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from bb_platform.contracts import ResourceDescriptor, ResourceKind

ARPHRD_CAN = 280
DEFAULT_MODBUS_TCP_PORT = 502
KIND_LABELS = {
    ResourceKind.SERIAL.value: "Serial · Modbus RTU",
    ResourceKind.TCP.value: "TCP · Modbus TCP",
    ResourceKind.CAN.value: "CAN",
    ResourceKind.GPIO.value: "GPIO",
    ResourceKind.STORAGE.value: "Накопитель",
}

_SERIAL_DEVICE = re.compile(
    r"^(ttyUSB\d+|ttyACM\d+|ttyAMA\d+|ttyS\d+|ttyMLB\d+|serial\d+|rfcomm\d+|COM\d+)$",
    re.IGNORECASE,
)
_CAN_DEVICE = re.compile(r"^(can|slcan)\d+$", re.IGNORECASE)
_GPIO_DEVICE = re.compile(r"^gpiochip\d+$", re.IGNORECASE)
_VIRTUAL_CAN = re.compile(r"^v(x)?can\d+$", re.IGNORECASE)
_SKIP_ARP_IFACES = {"lo", "docker0"}
_SKIP_ARP_PREFIXES = ("veth", "br-", "docker", "virbr", "cni", "flannel", "tun", "tap", "wg")
_VIRTUAL_GPIO_LABELS = {"gpio-mockup", "gpio-sim", "gpio-virt"}


def _env_csv(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def _path_from_env(name: str, *defaults: str) -> Path:
    raw = os.getenv(name, "").strip()
    if raw:
        return Path(raw)
    for default in defaults:
        if Path(default).exists():
            return Path(default)
    return Path(defaults[-1] if defaults else ".")


def discovery_dev_root() -> Path:
    """Host /dev, bind-mounted into Hub as /host-dev when running in Docker."""
    return _path_from_env("BB_DISCOVERY_DEV_ROOT", "/host-dev", "/dev")


def as_linux_dev_path(path: str, *, dev_root: Path | None = None) -> str:
    """Turn /host-dev/ttyAMA10 into /dev/ttyAMA10 for Docker --device and workers."""
    candidate = Path(path)
    root = Path(dev_root) if dev_root is not None else discovery_dev_root()
    try:
        actual = candidate.resolve() if candidate.exists() else candidate
        base = root.resolve() if root.exists() else root
        relative = actual.relative_to(base)
        mapped = relative.as_posix().lstrip("/")
        return f"/dev/{mapped}" if mapped else "/dev"
    except (ValueError, OSError):
        posix = str(path).replace("\\", "/")
        if posix.startswith("/dev/"):
            return posix
        return f"/dev/{candidate.name}"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _include_virtual() -> bool:
    return _env_flag("BB_DISCOVERY_INCLUDE_VIRTUAL", False)


def human_bytes(value: int | float | None) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    for index, unit in enumerate(units):
        if number < 1024 or index == len(units) - 1:
            if unit == "Б":
                return f"{int(number)} {unit}"
            text = f"{number:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
        number /= 1024
    return f"{int(value)} Б"


def is_usable_serial_port(path: str) -> bool:
    """Match legacy UART names; never treat /dev/tty (controlling TTY) as Modbus."""
    raw = str(path or "").strip()
    if not raw:
        return False
    resolved = Path(raw)
    name = resolved.name
    if name in {"tty", "console", "ttyprintk"} or re.fullmatch(r"tty\d+", name):
        return False
    parent = resolved.parent
    if parent.name in {"by-id", "by-path"} and parent.parent.name == "serial":
        return True
    return bool(_SERIAL_DEVICE.match(name))


def is_usable_can_interface(name: str) -> bool:
    raw = str(name or "").strip()
    if not raw:
        return False
    if _VIRTUAL_CAN.match(raw):
        return _include_virtual()
    return bool(_CAN_DEVICE.match(raw))


def is_usable_gpio_chip(path: str, *, label: str = "") -> bool:
    name = Path(str(path or "").strip()).name
    if not _GPIO_DEVICE.match(name):
        return False
    if label.strip().lower() in _VIRTUAL_GPIO_LABELS and not _include_virtual():
        return False
    return True


def _safe_realpath(path: str) -> str:
    candidate = Path(path)
    try:
        if candidate.exists():
            return str(candidate.resolve())
    except OSError:
        pass
    return path


def _is_present_node(path: str) -> bool:
    candidate = Path(path)
    try:
        if not candidate.exists():
            return False
        if os.name == "nt":
            return True
        return candidate.is_char_device() or candidate.is_symlink() or candidate.is_socket()
    except OSError:
        return False


def _serial_rank(path: str) -> tuple[int, str]:
    normalized = path.replace("\\", "/")
    name = Path(path).name.lower()
    if "/serial/by-id/" in normalized:
        return (0, name)
    if name.startswith("ttyusb") or name.startswith("ttyacm"):
        return (1, name)
    if name.startswith("ttyama"):
        return (2, name)
    if name.startswith("serial"):
        return (3, name)
    if name.startswith("ttys"):
        return (4, name)
    if name.startswith("rfcomm"):
        return (5, name)
    if name.upper().startswith("COM"):
        return (6, name)
    if "/serial/by-path/" in normalized:
        return (9, name)
    return (8, name)


def prefer_serial_paths(paths: Iterable[str], *, resolve: Callable[[str], str] | None = None) -> list[str]:
    """Collapse aliases like serial0 → ttyAMA0 and by-id → ttyUSB0 to one node."""
    resolver = resolve or _safe_realpath
    groups: dict[str, list[str]] = {}
    for path in paths:
        raw = str(path).strip()
        if not raw:
            continue
        groups.setdefault(resolver(raw), []).append(raw)
    chosen: list[str] = []
    for aliases in groups.values():
        chosen.append(sorted(aliases, key=_serial_rank)[0])
    return sorted(chosen, key=_serial_rank)


def _serial_transport(path: str) -> str:
    name = Path(path).name.lower()
    text = path.replace("\\", "/").lower()
    if "by-id" in text or name.startswith("ttyusb") or name.startswith("ttyacm"):
        return "usb"
    if name.startswith("rfcomm"):
        return "bluetooth"
    if name.upper().startswith("COM"):
        return "windows"
    return "onboard"


def _pyserial_ports() -> dict[str, dict[str, Any]]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return {}
    try:
        ports = list_ports.comports()
    except Exception:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for port in ports:
        device = str(getattr(port, "device", "") or "").strip()
        if not device:
            continue
        if os.name != "nt" and device == Path(device).name:
            continue
        found[device] = {
            "description": getattr(port, "description", None),
            "hwid": getattr(port, "hwid", None),
            "vid": getattr(port, "vid", None),
            "pid": getattr(port, "pid", None),
            "serial_number": getattr(port, "serial_number", None),
            "manufacturer": getattr(port, "manufacturer", None),
            "product": getattr(port, "product", None),
        }
    return found


def _serial_info_for(path: str, catalog: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if path in catalog:
        return dict(catalog[path])
    name = Path(path).name
    for device, info in catalog.items():
        if Path(device).name == name:
            return dict(info)
    return {}


def _serial_display_name(path: str, info: dict[str, Any], transport: str, aliases: list[str] | None = None) -> str:
    node = Path(path).name
    alias_names = {Path(item).name.lower() for item in (aliases or [])}
    product = str(info.get("product") or "").strip()
    description = str(info.get("description") or "").strip()
    manufacturer = str(info.get("manufacturer") or "").strip()
    label = product or description
    if label and label.lower() not in {node.lower(), "n/a", "com port"}:
        return f"{label} ({node})"
    if manufacturer:
        return f"{manufacturer} ({node})"
    if "serial0" in alias_names or node.lower() == "serial0":
        return f"GPIO UART ({node})"
    if transport == "onboard":
        return f"Бортовой UART ({node})"
    if transport == "usb":
        return f"USB serial ({node})"
    if transport == "bluetooth":
        return f"Bluetooth ({node})"
    return node


def _serial_detail(path: str, info: dict[str, Any], aliases: list[str]) -> str:
    parts: list[str] = [path]
    manufacturer = str(info.get("manufacturer") or "").strip()
    serial_number = str(info.get("serial_number") or "").strip()
    if manufacturer:
        parts.append(manufacturer)
    if serial_number:
        parts.append(f"S/N {serial_number}")
    extra = [item for item in aliases if item != path]
    if extra:
        parts.append("алиасы: " + ", ".join(extra[:3]))
    return " · ".join(parts)


def discover_serial_resources() -> list[dict[str, Any]]:
    root = discovery_dev_root()
    forced = _env_csv("BB_DISCOVERY_SERIAL_PATHS")
    catalog = _pyserial_ports()
    found: list[str] = list(forced)
    found.extend(catalog.keys())
    for pattern in (
        str(root / "ttyUSB*"),
        str(root / "ttyACM*"),
        str(root / "ttyAMA*"),
        str(root / "ttyS*"),
        str(root / "serial0"),
        str(root / "serial1"),
        str(root / "serial" / "by-id" / "*"),
        str(root / "serial" / "by-path" / "*"),
        str(root / "rfcomm*"),
    ):
        found.extend(glob.glob(pattern))
    usable: list[str] = []
    for path in found:
        if not is_usable_serial_port(path):
            continue
        if path in forced or os.name == "nt" or _is_present_node(path):
            usable.append(path)
    preferred = prefer_serial_paths(usable)
    grouped: dict[str, list[str]] = {}
    for path in usable:
        grouped.setdefault(_safe_realpath(path), []).append(path)

    resources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in preferred:
        host_path = as_linux_dev_path(path, dev_root=root)
        if host_path in seen:
            continue
        seen.add(host_path)
        info = _serial_info_for(path, catalog)
        transport = _serial_transport(host_path)
        aliases = sorted({as_linux_dev_path(item, dev_root=root) for item in grouped.get(_safe_realpath(path), [path])})
        resources.append(
            ResourceDescriptor(
                resource_id=f"serial:{host_path}",
                kind=ResourceKind.SERIAL,
                name=_serial_display_name(host_path, info, transport, aliases),
                path=host_path,
                metadata={
                    "protocol": "modbus_rtu",
                    "transport": transport,
                    "detail": _serial_detail(host_path, info, aliases),
                    "aliases": aliases,
                    "manufacturer": info.get("manufacturer"),
                    "product": info.get("product") or info.get("description"),
                    "hwid": info.get("hwid"),
                    "serial_number": info.get("serial_number"),
                },
            ).model_dump(mode="json")
        )
    return resources


def _sysfs_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def discover_can_resources(*, sys_class_net: Path | None = None) -> list[dict[str, Any]]:
    root = sys_class_net or _path_from_env("BB_DISCOVERY_SYS_CLASS_NET", "/host/sys/class/net", "/sys/class/net")
    names: set[str] = set(_env_csv("BB_DISCOVERY_CAN_IFACES"))
    if root.exists():
        try:
            entries = sorted(root.iterdir())
        except OSError:
            entries = []
        for iface in entries:
            type_text = _sysfs_text(iface / "type")
            try:
                if int(type_text) != ARPHRD_CAN:
                    continue
            except ValueError:
                continue
            names.add(iface.name)
    resources: list[dict[str, Any]] = []
    for name in sorted(names):
        if not is_usable_can_interface(name):
            continue
        iface = root / name
        operstate = _sysfs_text(iface / "operstate")
        bitrate_text = _sysfs_text(iface / "can_bittiming" / "bitrate")
        driver_link = iface / "device" / "driver"
        try:
            driver_name = driver_link.resolve().name if driver_link.is_symlink() else ""
        except OSError:
            driver_name = ""
        try:
            bitrate = int(bitrate_text) if bitrate_text else None
        except ValueError:
            bitrate = None
        detail_parts = [name]
        if bitrate:
            detail_parts.append(f"{bitrate // 1000} кбит/с" if bitrate >= 1000 else f"{bitrate} бит/с")
        if operstate:
            detail_parts.append("шина включена" if operstate == "up" else f"состояние: {operstate}")
        if driver_name:
            detail_parts.append(driver_name)
        resources.append(
            ResourceDescriptor(
                resource_id=f"can:{name}",
                kind=ResourceKind.CAN,
                name=name,
                path=f"/sys/class/net/{name}",
                metadata={
                    "protocol": "can",
                    "operstate": operstate or None,
                    "bitrate": bitrate,
                    "driver": driver_name or None,
                    "detail": " · ".join(detail_parts),
                },
            ).model_dump(mode="json")
        )
    return resources


def _gpio_sysfs_info(name: str, *, sys_bus_gpio: Path, sys_class_gpio: Path) -> dict[str, str]:
    for root in (sys_bus_gpio / name, sys_class_gpio / name):
        label = _sysfs_text(root / "label")
        ngpio = _sysfs_text(root / "ngpio")
        if label or ngpio:
            return {"label": label, "ngpio": ngpio}
    return {"label": "", "ngpio": ""}


def discover_gpio_resources(*, sys_bus_gpio: Path | None = None, sys_class_gpio: Path | None = None) -> list[dict[str, Any]]:
    bus_root = sys_bus_gpio or _path_from_env("BB_DISCOVERY_SYS_BUS_GPIO", "/host/sys/bus/gpio/devices", "/sys/bus/gpio/devices")
    class_root = sys_class_gpio or _path_from_env("BB_DISCOVERY_SYS_CLASS_GPIO", "/host/sys/class/gpio", "/sys/class/gpio")
    root = discovery_dev_root()
    forced = _env_csv("BB_DISCOVERY_GPIO_PATHS")
    paths = list(forced)
    paths.extend(glob.glob(str(root / "gpiochip*")))
    resources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(set(paths)):
        host_path = as_linux_dev_path(path, dev_root=root)
        name = Path(host_path).name
        info = _gpio_sysfs_info(name, sys_bus_gpio=bus_root, sys_class_gpio=class_root)
        if not is_usable_gpio_chip(host_path, label=info["label"]):
            continue
        if path not in forced and not _is_present_node(path) and os.name != "nt":
            continue
        if host_path in seen:
            continue
        seen.add(host_path)
        lines = info["ngpio"]
        label = info["label"] or name
        detail_parts = [host_path]
        if info["label"]:
            detail_parts.append(info["label"])
        if lines:
            detail_parts.append(f"{lines} линий")
        resources.append(
            ResourceDescriptor(
                resource_id=f"gpio:{host_path}",
                kind=ResourceKind.GPIO,
                name=f"{name} · {label}" if info["label"] else name,
                path=host_path,
                metadata={
                    "protocol": "gpio",
                    "label": info["label"] or None,
                    "ngpio": int(lines) if lines.isdigit() else None,
                    "detail": " · ".join(detail_parts),
                },
            ).model_dump(mode="json")
        )
    return resources


def parse_tcp_endpoint(raw: str, *, default_port: int = DEFAULT_MODBUS_TCP_PORT) -> tuple[str, int] | None:
    text = str(raw or "").strip().removeprefix("tcp://").removeprefix("tcp:")
    if not text:
        return None
    host, sep, port_text = text.rpartition(":")
    if sep and port_text.isdigit() and host:
        return host, int(port_text)
    if text.replace(".", "").isdigit() or "." in text or text in {"localhost", "::1"}:
        return text, default_port
    return text, default_port


def probe_tcp(host: str, port: int, *, timeout: float = 0.2, opener: Callable[..., socket.socket] | None = None) -> bool:
    connect = opener or socket.create_connection
    try:
        with connect((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tcp_ports() -> list[int]:
    ports: list[int] = []
    for item in _env_csv("BB_DISCOVERY_TCP_PORTS") or [str(DEFAULT_MODBUS_TCP_PORT)]:
        if item.isdigit():
            ports.append(int(item))
    return ports or [DEFAULT_MODBUS_TCP_PORT]


def _lan_scan_enabled(*, probe_network: bool) -> bool:
    if not probe_network:
        return False
    raw = os.getenv("BB_DISCOVERY_TCP_SCAN")
    if raw is not None and raw.strip():
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return not os.getenv("PYTEST_CURRENT_TEST")


def parse_arp_neighbors(text: str) -> list[tuple[str, str, str]]:
    neighbors: list[tuple[str, str, str]] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        ip, _hw_type, flags, mac, _mask, iface = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        if flags in {"0x0", "0x1"}:
            continue
        if mac in {"00:00:00:00:00:00", "*"}:
            continue
        if iface in _SKIP_ARP_IFACES or iface.startswith(_SKIP_ARP_PREFIXES):
            continue
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        neighbors.append((ip, mac, iface))
    return neighbors


def _arp_neighbors(proc_net_arp: Path) -> list[tuple[str, str, str]]:
    try:
        return parse_arp_neighbors(proc_net_arp.read_text(encoding="utf-8"))
    except OSError:
        return []


def _tcp_resource(host: str, port: int, *, reachable: bool | None, source: str, mac: str | None = None, iface: str | None = None) -> dict[str, Any]:
    endpoint = f"{host}:{port}"
    detail_parts = [endpoint]
    if reachable is True:
        detail_parts.append("порт отвечает")
    elif reachable is False:
        detail_parts.append("нет ответа")
    if iface:
        detail_parts.append(iface)
    return ResourceDescriptor(
        resource_id=f"tcp:{endpoint}",
        kind=ResourceKind.TCP,
        name=endpoint,
        address=endpoint,
        metadata={
            "protocol": "modbus_tcp",
            "host": host,
            "port": port,
            "reachable": reachable,
            "source": source,
            "mac": mac,
            "iface": iface,
            "detail": " · ".join(detail_parts),
        },
    ).model_dump(mode="json")


def discover_tcp_resources(
    extra_endpoints: Iterable[str] | None = None,
    *,
    probe_network: bool = True,
    proc_net_arp: Path | None = None,
    opener: Callable[..., socket.socket] | None = None,
) -> list[dict[str, Any]]:
    timeout = float(os.getenv("BB_DISCOVERY_TCP_TIMEOUT", "0.2"))
    configured: list[tuple[str, int, str]] = []
    for raw in [*_env_csv("BB_DISCOVERY_TCP_ENDPOINTS"), *(extra_endpoints or [])]:
        parsed = parse_tcp_endpoint(raw)
        if parsed:
            configured.append((*parsed, "configured"))
    arp_targets: list[tuple[str, int, str, str, str]] = []
    if _lan_scan_enabled(probe_network=probe_network):
        for ip, mac, iface in _arp_neighbors(proc_net_arp or _path_from_env("BB_DISCOVERY_PROC_NET_ARP", "/host/proc/net/arp", "/proc/net/arp")):
            for port in _tcp_ports():
                arp_targets.append((ip, port, "arp", mac, iface))

    seen: set[tuple[str, int]] = set()
    resources: list[dict[str, Any]] = []

    def add(host: str, port: int, *, reachable: bool | None, source: str, mac: str | None = None, iface: str | None = None) -> None:
        key = (host, port)
        if key in seen:
            return
        seen.add(key)
        resources.append(_tcp_resource(host, port, reachable=reachable, source=source, mac=mac, iface=iface))

    for host, port, source in configured:
        reachable = probe_tcp(host, port, timeout=timeout, opener=opener) if probe_network else None
        add(host, port, reachable=reachable, source=source)

    if arp_targets:
        with ThreadPoolExecutor(max_workers=min(32, len(arp_targets))) as pool:
            futures = [
                (host, port, mac, iface, pool.submit(probe_tcp, host, port, timeout=timeout, opener=opener))
                for host, port, _source, mac, iface in arp_targets
            ]
        for host, port, mac, iface, future in futures:
            if (host, port) in seen:
                continue
            try:
                reachable = bool(future.result())
            except Exception:
                reachable = False
            if reachable:
                add(host, port, reachable=True, source="arp", mac=mac, iface=iface)
    return resources


def discover_storage_resources(data_root: Path) -> list[dict[str, Any]]:
    data_root.mkdir(parents=True, exist_ok=True)
    found: list[dict[str, Any]] = []
    try:
        usage = shutil.disk_usage(data_root)
        storage_meta = {
            "class": "internal",
            "free_bytes": int(usage.free),
            "total_bytes": int(usage.total),
            "free_human": human_bytes(usage.free),
            "total_human": human_bytes(usage.total),
            "detail": f"{human_bytes(usage.free)} свободно из {human_bytes(usage.total)}",
        }
    except OSError:
        storage_meta = {"class": "internal", "detail": "внутренний диск Hub"}
    found.append(
        ResourceDescriptor(
            resource_id="storage:data",
            kind=ResourceKind.STORAGE,
            name="Внутренний диск Hub",
            path=str(data_root),
            metadata=storage_meta,
        ).model_dump(mode="json")
    )
    storage_candidates: list[tuple[str, str]] = []
    for item in _env_csv("BB_STORAGE_PATHS"):
        if "=" not in item:
            continue
        resource_id, path = item.split("=", 1)
        resource_id, path = resource_id.strip(), path.strip()
        if resource_id and path and Path(path).exists():
            storage_candidates.append((resource_id, path))
    for mount_root in ("/mnt", "/media", "/run/media"):
        for candidate in sorted(glob.glob(f"{mount_root}/*")):
            path = Path(candidate)
            try:
                if not path.is_dir() or not os.path.ismount(path):
                    continue
            except OSError:
                continue
            storage_candidates.append((path.name, str(path)))
            for nested in sorted(glob.glob(f"{candidate}/*")):
                nested_path = Path(nested)
                try:
                    if nested_path.is_dir() and os.path.ismount(nested_path):
                        storage_candidates.append((nested_path.name, str(nested_path)))
                except OSError:
                    continue
    seen_storage_paths: set[str] = set()
    seen_storage_ids: set[str] = set()
    for resource_id, path in storage_candidates:
        resolved_path = str(Path(path).resolve())
        if resource_id == "data" or resolved_path == str(data_root.resolve()) or resolved_path in seen_storage_paths or resource_id in seen_storage_ids:
            continue
        seen_storage_paths.add(resolved_path)
        seen_storage_ids.add(resource_id)
        try:
            usage = shutil.disk_usage(path)
            metadata = {
                "class": "external",
                "free_bytes": int(usage.free),
                "total_bytes": int(usage.total),
                "free_human": human_bytes(usage.free),
                "total_human": human_bytes(usage.total),
                "detail": f"{human_bytes(usage.free)} свободно из {human_bytes(usage.total)}",
            }
        except OSError:
            metadata = {"class": "external", "detail": "внешний носитель"}
        found.append(
            ResourceDescriptor(
                resource_id=f"storage:{resource_id}",
                kind=ResourceKind.STORAGE,
                name=resource_id,
                path=path,
                metadata=metadata,
            ).model_dump(mode="json")
        )
    return found


def tcp_hints_from_vms(vms: Iterable[dict[str, Any]]) -> list[str]:
    hints: list[str] = []
    for vm in vms:
        if str(vm.get("protocol") or "") != "modbus_tcp":
            continue
        config = vm.get("config") if isinstance(vm.get("config"), dict) else {}
        reader = config.get("reader") if isinstance(config.get("reader"), dict) else {}
        host = str(reader.get("host") or "").strip()
        if not host or host in {"127.0.0.1", "localhost", "::1"}:
            continue
        port = int(reader.get("tcp_port") or DEFAULT_MODBUS_TCP_PORT)
        hints.append(f"{host}:{port}")
    return hints


def discover_resources(
    data_root: Path,
    extra_tcp_endpoints: Iterable[str] | None = None,
    *,
    probe_network: bool = True,
    sys_class_net: Path | None = None,
    sys_bus_gpio: Path | None = None,
    sys_class_gpio: Path | None = None,
    proc_net_arp: Path | None = None,
) -> list[dict[str, Any]]:
    return [
        *discover_serial_resources(),
        *discover_gpio_resources(sys_bus_gpio=sys_bus_gpio, sys_class_gpio=sys_class_gpio),
        *discover_can_resources(sys_class_net=sys_class_net),
        *discover_tcp_resources(extra_tcp_endpoints, probe_network=probe_network, proc_net_arp=proc_net_arp),
        *discover_storage_resources(data_root),
    ]


def discovery_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    present = [item for item in items if item.get("available", True)]
    return {
        "serial": sum(1 for item in present if item.get("kind") == ResourceKind.SERIAL.value),
        "tcp": sum(1 for item in present if item.get("kind") == ResourceKind.TCP.value),
        "can": sum(1 for item in present if item.get("kind") == ResourceKind.CAN.value),
        "gpio": sum(1 for item in present if item.get("kind") == ResourceKind.GPIO.value),
        "storage": sum(1 for item in present if item.get("kind") == ResourceKind.STORAGE.value),
    }
