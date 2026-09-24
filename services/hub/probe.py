"""One-shot connection test used from the VM create/edit form."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from bb_platform.contracts import MapDocument, Quality, RawBatch, RawSample, VmProtocol
from bb_platform.parser import diagnose_read, field_channel, field_label, parse_batch

from .discovery import hub_serial_path, operator_serial_path
from .link import measure_tcp_link

RTURead = Callable[[dict[str, Any], list[dict[str, Any]]], tuple[dict[str, list[Any]], str | None]]
TCPRead = Callable[[dict[str, Any], list[dict[str, Any]]], tuple[dict[str, list[Any]], str | None]]


def _public_error(message: str) -> str:
    return str(message or "").replace("\\", "/").replace("/host-dev/", "/dev/")


def _quality_from_sources(sources: dict[str, list[Any]], failures: list[Any] | None = None) -> Quality:
    if failures and not any(sources.values()):
        return Quality.BAD
    if failures:
        return Quality.DEGRADED
    return Quality.GOOD


def channel_payload(fields: list[Any], sample_tags: dict[str, Any], analog: dict[str, Any], discrete: dict[str, bool], alerts: list[str]) -> dict[str, Any]:
    analog_rows: list[dict[str, Any]] = []
    discrete_rows: list[dict[str, Any]] = []
    alert_catalog: list[dict[str, Any]] = []
    active = {str(item) for item in alerts}
    for field in fields:
        if not isinstance(field, dict) or not field.get("name"):
            continue
        if field.get("system") or field.get("is_system") or field.get("internal"):
            continue
        name = str(field["name"])
        label = field_label(field, name)
        channel = field_channel(field)
        row = {"name": name, "label": label, "kind": channel, "value": analog.get(name, discrete.get(name, sample_tags.get(name)))}
        if channel == "analog":
            analog_rows.append(row)
        elif channel == "discrete":
            discrete_rows.append({**row, "value": discrete.get(name, bool(sample_tags.get(name)))})
        else:
            labels = field.get("bits") if isinstance(field.get("bits"), dict) else {}
            if labels:
                for bit, alarm_name in labels.items():
                    text = str(alarm_name)
                    alert_catalog.append({"name": text, "bit": str(bit), "active": text in active, "source": name})
            elif isinstance(sample_tags.get(name), list):
                for item in sample_tags.get(name) or []:
                    alert_catalog.append({"name": str(item), "bit": None, "active": True, "source": name})
    seen = {item["name"] for item in alert_catalog}
    for name in alerts:
        if name not in seen:
            alert_catalog.append({"name": name, "bit": None, "active": True, "source": "active_alarms"})
    return {"analog": analog_rows, "discrete": discrete_rows, "alerts": alert_catalog, "active_alerts": list(alerts)}


def _result(*, diagnosis: dict[str, Any], quality: str | None = None, last_error: str | None = None, groups: dict[str, Any] | None = None, device: dict[str, Any] | None = None) -> dict[str, Any]:
    groups = groups or {"analog": [], "discrete": [], "alerts": [], "active_alerts": []}
    return {
        "ok": diagnosis.get("code") in {"ok", "device_alerts", "partial"},
        "quality": quality,
        "last_error": last_error,
        "diagnosis": diagnosis,
        "device": device or {},
        **groups,
    }


def read_rtu_sources(reader: dict[str, Any], requests: list[dict[str, Any]]) -> tuple[dict[str, list[Any]], str | None]:
    from workers.modbus_rtu.main import ModbusReader, _close_instrument, _make_instrument

    configured = str(reader.get("port") or "")
    opened = hub_serial_path(configured)
    cfg = dict(reader)
    cfg["port"] = opened
    cfg["timeout_sec"] = min(float(cfg.get("timeout_sec", cfg.get("timeout", 0.35)) or 0.35), 1.0)
    instrument = _make_instrument(cfg)
    try:
        probe_reader = ModbusReader(
            instrument,
            retries=1,
            retry_delay=0,
            address_offset=int(cfg.get("address_offset", 1) or 1),
        )
        sources = probe_reader.read(requests)
        failures = getattr(probe_reader, "last_failures", None) or []
        error = _public_error(str(failures[0][1])) if failures else None
        if failures and not any(sources.values()):
            raise RuntimeError(f"Modbus request {failures[0][0]} failed") from failures[0][1]
        return sources, error
    finally:
        _close_instrument(instrument)


def read_tcp_sources(reader: dict[str, Any], requests: list[dict[str, Any]]) -> tuple[dict[str, list[Any]], str | None]:
    from workers.modbus_tcp.main import ModbusTcpReader

    probe_reader = ModbusTcpReader(
        str(reader.get("host") or "127.0.0.1"),
        int(reader.get("tcp_port", reader.get("port", 502)) or 502),
        int(reader.get("unit_id", reader.get("slave_id", 1)) or 1),
        timeout=min(float(reader.get("timeout_sec", reader.get("timeout", 0.35)) or 0.35), 1.5),
        retries=1,
        retry_delay=0,
        address_offset=int(reader.get("address_offset", 1) or 1),
    )
    sources = probe_reader.read(requests)
    failures = getattr(probe_reader, "last_failures", None) or []
    error = _public_error(str(failures[0][1])) if failures else None
    if failures and not any(sources.values()):
        raise RuntimeError(f"Modbus TCP request {failures[0][0]} failed") from failures[0][1]
    return sources, error


def read_simulator_sources(requests: list[dict[str, Any]]) -> dict[str, list[Any]]:
    sources: dict[str, list[Any]] = {}
    for index, request in enumerate(requests or [{"name": "sim", "count": 3}]):
        name = str(request.get("name") or "sim")
        count = max(1, int(request.get("count") or 1))
        if index == 0:
            sources[name] = [index + 1 if offset == 0 else int(offset % 2 == 0) for offset in range(count)]
            if count >= 2:
                sources[name][1] = 1
        else:
            sources[name] = [0] * count
    return sources


def probe_can(reader: dict[str, Any]) -> dict[str, Any]:
    iface = str(reader.get("can_interface") or "").strip()
    env_root = os.getenv("BB_DISCOVERY_SYS_CLASS_NET", "").strip()
    root = Path(env_root) if env_root else Path("/host/sys/class/net" if Path("/host/sys/class/net").exists() else "/sys/class/net")
    node = root / iface if iface else None
    if not iface or node is None or not node.exists():
        return _result(
            diagnosis={
                "code": "no_answer",
                "title": "CAN-интерфейс не найден",
                "detail": "Нажмите «Найти интерфейсы» и выберите can0 (или другой SocketCAN). Это не аварии карты.",
                "link": "down",
                "cause": "port",
                "can_read_alerts": False,
            },
            quality="bad",
            last_error=f"interface {iface or '—'} is missing",
            device={"kind": "can", "interface": iface},
        )
    operstate = ""
    try:
        operstate = (node / "operstate").read_text(encoding="utf-8").strip()
    except OSError:
        operstate = ""
    if operstate and operstate != "up":
        return _result(
            diagnosis={
                "code": "no_answer",
                "title": "Шина CAN опущена",
                "detail": f"{iface} в состоянии {operstate}. Поднимите интерфейс и проверьте битрейт.",
                "link": "down",
                "cause": "port",
                "can_read_alerts": False,
            },
            quality="bad",
            last_error=f"{iface} is {operstate}",
            device={"kind": "can", "interface": iface, "operstate": operstate},
        )
    return _result(
        diagnosis={
            "code": "ok",
            "title": "Интерфейс CAN доступен",
            "detail": f"{iface} найден" + (f", состояние {operstate}" if operstate else "") + ". Кадры карты CAN этот пробник ещё не разбирает.",
            "link": "up",
            "cause": "none",
            "can_read_alerts": False,
        },
        quality="good",
        device={"kind": "can", "interface": iface, "operstate": operstate or "unknown"},
    )


def parse_probe_sample(protocol: str, map_document: MapDocument, sources: dict[str, list[Any]], quality: Quality) -> Any:
    batch = RawBatch(
        vm_id=uuid4(),
        protocol=VmProtocol(protocol),
        map_version=map_document.version,
        seq_start=1,
        samples=[RawSample(seq=1, captured_at=datetime.now(timezone.utc), sources=sources, quality=quality)],
    )
    return parse_batch(batch, map_document)[0]


def run_probe(
    *,
    protocol: str,
    reader: dict[str, Any],
    map_document: MapDocument,
    rtu_read: RTURead | None = None,
    tcp_read: TCPRead | None = None,
) -> dict[str, Any]:
    requests = list(map_document.requests or [])
    device: dict[str, Any] = {"protocol": protocol}
    sources: dict[str, list[Any]] = {}
    last_error: str | None = None
    quality = Quality.GOOD
    try:
        if protocol == VmProtocol.SIMULATOR.value:
            sources = read_simulator_sources(requests)
            device["kind"] = "simulator"
        elif protocol == VmProtocol.MODBUS_RTU.value:
            port = str(reader.get("port") or "")
            device = {
                "kind": "serial",
                "port": operator_serial_path(port),
                "open_path": hub_serial_path(port),
                "slave_id": reader.get("slave_id"),
                "baudrate": reader.get("baudrate"),
            }
            sources, last_error = (rtu_read or read_rtu_sources)(reader, requests)
            quality = _quality_from_sources(sources, [last_error] if last_error else None)
        elif protocol == VmProtocol.MODBUS_TCP.value:
            host = str(reader.get("host") or "")
            port = int(reader.get("tcp_port") or reader.get("port") or 502)
            measured = measure_tcp_link(host, port, timeout=min(float(reader.get("timeout_sec") or 0.8), 1.5), use_cache=False)
            device = {
                "kind": "tcp",
                "host": host,
                "tcp_port": port,
                "unit_id": reader.get("unit_id") or reader.get("slave_id"),
                "ping_ms": measured.get("ping_ms"),
                "ping_method": measured.get("ping_method"),
                "tcp_ok": measured.get("tcp_ok"),
            }
            if not measured.get("tcp_ok"):
                return _result(
                    diagnosis={
                        "code": "no_answer",
                        "title": measured.get("detail") or "Нет TCP",
                        "detail": (
                            f"Пинг: {measured['ping_ms']} мс. " if measured.get("ping_ms") is not None else "Пинг не прошёл. "
                        ) + "Порт Modbus не открылся — чтение карты не запускалось.",
                        "link": "down",
                        "cause": "port",
                        "can_read_alerts": False,
                    },
                    quality="bad",
                    last_error=str((measured.get("tcp") or {}).get("detail") or measured.get("detail") or "tcp down"),
                    device=device,
                )
            sources, last_error = (tcp_read or read_tcp_sources)(reader, requests)
            quality = _quality_from_sources(sources, [last_error] if last_error else None)
        elif protocol == VmProtocol.CAN.value:
            return probe_can(reader)
        else:
            return _result(
                diagnosis={
                    "code": "no_sample",
                    "title": "Протокол без пробника",
                    "detail": "Для этого протокола тестовое чтение ещё не сделано.",
                    "link": "unknown",
                    "cause": "none",
                    "can_read_alerts": False,
                },
                device=device,
            )
        sample = parse_probe_sample(protocol, map_document, sources, quality)
        groups = channel_payload(list(map_document.fields or []), sample.tags, sample.analog, sample.discrete, sample.alerts)
        diagnosis = diagnose_read(
            quality=sample.quality,
            last_error=last_error,
            alerts=list(sample.alerts),
            has_sample=True,
        )
        return _result(
            diagnosis=diagnosis,
            quality=getattr(sample.quality, "value", sample.quality),
            last_error=last_error,
            groups=groups,
            device=device,
        )
    except Exception as exc:
        error = _public_error(str(exc))
        diagnosis = diagnose_read(quality=Quality.BAD, last_error=error, has_sample=True)
        return _result(diagnosis=diagnosis, quality="bad", last_error=error, device=device)
