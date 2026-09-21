"""Safe parser and legacy map adapter used by Hub ingest."""

from __future__ import annotations

import hashlib
import json
import ast
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .contracts import MapDocument, Quality, RawBatch, TagSample, VmProtocol


def _safe_eval(expression: str, context: dict[str, Any]) -> Any:
    helpers = {
        "abs": abs,
        "bool": bool,
        "float": float,
        "int": int,
        "len": len,
        "max": max,
        "min": min,
        "round": round,
        "str": str,
    }
    if len(expression) > 512:
        raise ValueError("expression is too long")
    tree = ast.parse(expression, mode="eval")
    allowed_nodes = (
        ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp,
        ast.BoolOp, ast.Compare, ast.IfExp, ast.Subscript, ast.List, ast.Tuple,
        ast.Index, ast.Slice, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
        ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq,
        ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Call,
    )
    nodes = list(ast.walk(tree))
    if len(nodes) > 100 or any(not isinstance(node, allowed_nodes) for node in nodes):
        raise ValueError("expression contains a disallowed operation")
    names = set(helpers) | set(context)
    for node in nodes:
        if isinstance(node, ast.Name) and (node.id not in names or node.id.startswith("__")):
            raise ValueError(f"unknown expression name: {node.id}")
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in helpers):
            raise ValueError("only approved expression helpers may be called")
    return eval(compile(tree, "<map-expression>", "eval"), {"__builtins__": {}}, {**helpers, **context})


def _checksum(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def adapt_legacy_map(
    payload: dict[str, Any],
    *,
    protocol: VmProtocol = VmProtocol.MODBUS_RTU,
    preset_id: str | None = None,
    version: str = "legacy-1",
) -> MapDocument:
    """Normalize either legacy JSON shape into an immutable map document."""
    if not isinstance(payload, dict):
        raise ValueError("map root must be an object")
    requests = payload.get("requests", [])
    fields = payload.get("fields", [])
    if not isinstance(requests, list) or not isinstance(fields, list):
        raise ValueError("requests and fields must be arrays")
    if len(fields) > 10_000 or len(requests) > 1_000:
        raise ValueError("map is too large")
    request_names: set[str] = set()
    for request in requests:
        if not isinstance(request, dict) or not str(request.get("name", "")).strip():
            raise ValueError("each request requires a name")
        name = str(request["name"]).strip()
        if name in request_names:
            raise ValueError(f"duplicate request name: {name}")
        request_names.add(name)
        try:
            fc = int(request.get("fc", 3))
            address = int(request.get("address", 0))
            count = int(request.get("count", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("request fc/address/count must be integers") from exc
        if fc not in {1, 2, 3, 4}:
            raise ValueError("request function code must be one of 1, 2, 3 or 4")
        max_count = 2000 if fc in {1, 2} else 125
        if address < 0 or address > 0xFFFF or count < 1 or count > max_count or address + count > 0x10000:
            raise ValueError("request address/count is outside the valid range")
    field_names: set[str] = set()
    request_by_name = {str(request["name"]).strip(): request for request in requests}
    supported_types = {
        "bool",
        "boolean",
        "uint16",
        "int16",
        "sint16",
        "uint32",
        "uint32_be",
        "uint32_le",
        "int32",
        "int32_be",
        "bitfield",
        "expr",
    }
    for field in fields:
        if not isinstance(field, dict) or not str(field.get("name", "")).strip():
            raise ValueError("each field requires a name")
        name = str(field["name"]).strip()
        if name in field_names:
            raise ValueError(f"duplicate field name: {name}")
        field_names.add(name)
        try:
            field_address = int(field.get("address", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("field address must be an integer") from exc
        if field_address < 0:
            raise ValueError("field address must be non-negative")
        field_type = str(field.get("type", "uint16")).lower()
        if field_type not in supported_types:
            raise ValueError(f"unsupported field type: {field_type}")
        if "bit" in field:
            try:
                bit = int(field["bit"])
            except (TypeError, ValueError) as exc:
                raise ValueError("field bit must be an integer") from exc
            if not 0 <= bit <= 31:
                raise ValueError("field bit must be between 0 and 31")
        if field_type == "expr" and not str(field.get("expr", "")).strip():
            raise ValueError(f"expression field {name} requires expr")
        if field_type != "expr":
            source = str(field.get("source", "")).strip()
            if source not in request_by_name:
                raise ValueError(f"field {name} references an unknown request: {source}")
            request = request_by_name[source]
            width = 2 if field_type in {"uint32", "uint32_be", "uint32_le", "int32", "int32_be"} else 1
            if field_address + width > int(request.get("count", 1)):
                raise ValueError(f"field {name} exceeds request {source} count")
        if field_type == "bitfield":
            labels = field.get("bits", field.get("bit_labels", {}))
            if not isinstance(labels, dict):
                raise ValueError(f"bitfield labels for {name} must be an object")
            for bit in labels:
                try:
                    if not 0 <= int(bit) <= 31:
                        raise ValueError
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"bitfield bit for {name} must be between 0 and 31") from exc
        kind = str(field.get("kind") or field.get("channel") or "").strip().lower()
        if kind and kind not in {"analog", "discrete", "alert"}:
            raise ValueError(f"field {name} kind must be analog, discrete or alert")
        for numeric_key in ("scale", "offset"):
            if numeric_key in field:
                try:
                    float(field[numeric_key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"field {numeric_key} must be numeric") from exc
        if "decimals" in field:
            try:
                if int(field["decimals"]) < 0 or int(field["decimals"]) > 12:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ValueError("field decimals must be between 0 and 12") from exc
    canonical = {
        "protocol": protocol.value,
        "preset_id": preset_id,
        "version": version,
        "requests": requests,
        "fields": fields,
    }
    return MapDocument(
        version=version,
        protocol=protocol,
        preset_id=preset_id,
        checksum=_checksum(canonical),
        requests=requests,
        fields=fields,
        metadata={"source": "legacy"},
    )


ALERT_FIELD_NAMES = {"active_alarms", "active_status", "alarms"}
CHANNEL_KINDS = {"analog", "discrete", "alert"}


def field_channel(field: dict[str, Any]) -> str:
    """Classify a map field the way legacy CSV/DB registration did.

    Analogues are numeric measurements written on every poll. Discretes are
    boolean coils/status bits. Alerts are instrument alarm messages
    (bitfields such as ``active_alarms``), never the UART/link failure.
    """
    explicit = str(field.get("kind") or field.get("channel") or "").strip().lower()
    if explicit in CHANNEL_KINDS:
        return explicit
    name = str(field.get("name") or "").strip()
    field_type = str(field.get("type") or "uint16").lower()
    if field_type == "bitfield" or name in ALERT_FIELD_NAMES:
        return "alert"
    if field_type in {"bool", "boolean"} or "bit" in field:
        return "discrete"
    return "analog"


def split_channels(fields: list[Any], tags: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bool], list[str]]:
    analog: dict[str, Any] = {}
    discrete: dict[str, bool] = {}
    alerts: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        if not name or field.get("system") or field.get("is_system") or field.get("internal"):
            continue
        kind = field_channel(field)
        value = tags.get(name)
        if kind == "alert":
            if isinstance(value, list):
                alerts.extend(str(item) for item in value if str(item).strip())
            elif value not in {None, "", False}:
                alerts.append(str(value))
        elif kind == "discrete":
            discrete[name] = bool(value)
        else:
            analog[name] = value
    seen: set[str] = set()
    unique: list[str] = []
    for item in alerts:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return analog, discrete, unique


def diagnose_read(
    *,
    quality: Quality | str | None,
    last_error: str | None = None,
    alerts: list[str] | None = None,
    has_sample: bool = False,
) -> dict[str, Any]:
    """Tell an operator whether the fault is the link or the instrument."""
    alerts = [str(item) for item in (alerts or []) if str(item).strip()]
    quality_value = getattr(quality, "value", quality)
    error = str(last_error or "")
    lowered = error.lower()
    if not has_sample and not any(
        token in lowered for token in ("no communication", "no answer", "could not open port", "errno")
    ):
        return {
            "code": "no_sample",
            "title": "Ещё нет точки чтения",
            "detail": "ВМ не прислала ни одной выборки. Запустите её и подождите первый опрос.",
            "link": "unknown",
            "cause": "none",
            "can_read_alerts": False,
        }
    if not has_sample:
        quality_value = Quality.BAD.value
    if quality_value == Quality.BAD.value:
        cause = "silent"
        title = "Нет связи с прибором"
        detail = (
            "Порт открылся, но slave не ответил. Это не «нет аналогов» и не аварии контроллера. "
            "Алерты читаются из holding-регистров карты (active_alarms) и сейчас недоступны. "
            "Проверьте Slave ID, скорость, A/B и питание прибора."
        )
        if "could not open port" in lowered or "errno 2" in lowered:
            cause = "port"
            title = "Порт недоступен"
            detail = (
                "Контейнер не смог открыть выбранный UART. Это ошибка конфигурации порта, "
                "а не отсутствие измерений и не алерты прибора."
            )
        elif "errno 6" in lowered:
            cause = "port"
            title = "Выбран служебный TTY"
            detail = (
                "Открыт управляющий терминал, а не UART прибора. Выберите порт из сканера "
                "(например ttyAMA10 / ttyUSB0) и перезапустите ВМ."
            )
        return {
            "code": "no_answer",
            "title": title,
            "detail": detail,
            "link": "down",
            "cause": cause,
            "can_read_alerts": False,
        }
    if quality_value == Quality.DEGRADED.value:
        return {
            "code": "partial",
            "title": "Связь частичная",
            "detail": "Часть запросов карты прошла, часть нет. Похоже на несовпадение карты или адреса, а не на полное отсутствие данных.",
            "link": "partial",
            "cause": "map",
            "can_read_alerts": True,
        }
    if alerts:
        return {
            "code": "device_alerts",
            "title": "Связь есть, прибор сообщает аварии",
            "detail": "Чтение прошло. Список ниже — активные алерты из регистров прибора, а не ошибка UART.",
            "link": "up",
            "cause": "none",
            "can_read_alerts": True,
        }
    return {
        "code": "ok",
        "title": "Связь есть, активных аварий нет",
        "detail": "Прибор отвечает. Нулевые аналоги — текущие измерения, а не обрыв связи.",
        "link": "up",
        "cause": "none",
        "can_read_alerts": True,
    }


def parse_source_values(
    config: dict[str, Any],
    source_values: dict[str, list[Any]],
) -> tuple[dict[str, Any], Quality, list[str]]:
    """Parse legacy requests/fields without exposing Python builtins."""
    result: dict[str, Any] = {}
    errors: list[str] = []
    for field in config.get("fields", []):
        name = str(field.get("name", "")).strip()
        if not name:
            continue
        try:
            field_type = str(field.get("type", "uint16")).lower()
            if field_type == "expr" and "expr" in field:
                value = _safe_eval(str(field["expr"]), result)
            else:
                source = str(field.get("source", ""))
                address = int(field.get("address", 0))
                values = source_values.get(source, [])
                raw = values[address] if 0 <= address < len(values) else 0
                if field_type in {"bool", "boolean"}:
                    value: Any = bool(raw)
                elif field_type in {"int16", "sint16"}:
                    value = int(raw) & 0xFFFF
                    value = value - 65536 if value >= 32768 else value
                elif field_type in {"uint32_be", "uint32", "uint32_le", "int32_be", "int32"}:
                    lo = values[address + 1] if address + 1 < len(values) else 0
                    if field_type == "uint32_le":
                        value = (int(lo) << 16) | int(raw)
                    else:
                        value = (int(raw) << 16) | int(lo)
                    if field_type in {"int32_be", "int32"} and value >= 2**31:
                        value -= 2**32
                elif field_type == "bitfield":
                    labels = field.get("bits", field.get("bit_labels", {}))
                    value = [
                        str(label)
                        for bit, label in labels.items()
                        if int(raw) & (1 << int(bit))
                    ]
                else:
                    value = int(raw)
                if "bit" in field:
                    value = bool(int(value) & (1 << int(field["bit"])))
                if "expr" in field:
                    value = _safe_eval(str(field["expr"]), {"x": value, **result})
                if not isinstance(value, bool) and isinstance(value, (int, float)):
                    if "scale" in field:
                        value = value * float(field["scale"])
                    if "offset" in field:
                        value = value + float(field["offset"])
                if field.get("round") and isinstance(value, float):
                    value = round(value, 3)
                if "decimals" in field and isinstance(value, (int, float)) and not isinstance(value, bool):
                    value = round(value, int(field["decimals"]))
            result[name] = value
        except Exception as exc:  # malformed fields degrade only that field
            result[name] = None
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    for name in result.get("active_status", []) or []:
        result[str(name)] = True
    quality = Quality.BAD if errors and not result else Quality.DEGRADED if errors else Quality.GOOD
    return result, quality, errors


def parse_batch(batch: RawBatch, map_document: MapDocument) -> list[TagSample]:
    if batch.protocol != map_document.protocol:
        raise ValueError("batch protocol mismatch")
    samples: list[TagSample] = []
    for sample in batch.samples:
        tags, quality, errors = parse_source_values(
            {"requests": map_document.requests, "fields": map_document.fields},
            sample.sources,
        )
        # A worker can mark a sample degraded/bad when the physical read had
        # an error even if the map itself parsed successfully (for example a
        # partial protocol response). Never upgrade that quality during parse.
        if sample.quality == Quality.BAD:
            quality = Quality.BAD
        elif sample.quality == Quality.DEGRADED and quality == Quality.GOOD:
            quality = Quality.DEGRADED
        if sample.quality == Quality.BAD:
            analog, discrete, alerts = {}, {}, []
        else:
            analog, discrete, alerts = split_channels(list(map_document.fields or []), tags)
        if errors:
            tags["_parse_errors"] = errors
        samples.append(
            TagSample(
                vm_id=batch.vm_id,
                seq=sample.seq,
                captured_at=sample.captured_at,
                map_version=batch.map_version,
                tags=tags,
                analog=analog,
                discrete=discrete,
                alerts=alerts,
                quality=quality,
                protocol=batch.protocol,
            )
        )
    return samples


__all__ = ["adapt_legacy_map", "diagnose_read", "field_channel", "parse_batch", "parse_source_values", "split_channels"]
