"""Safe parser and legacy map adapter used by Hub ingest."""

from __future__ import annotations

import hashlib
import json
import ast
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .contracts import MapDocument, Quality, RawBatch, TagSample, VmProtocol

FIELD_TYPE_ALIASES = {
    "u16": "uint16",
    "uint": "uint16",
    "word": "uint16",
    "s16": "int16",
    "i16": "int16",
    "short": "int16",
    "u32": "uint32_be",
    "s32": "int32_be",
    "i32": "int32_be",
}
FIELD_KIND_ALIASES = {
    "analogue": "analog",
    "measurement": "analog",
    "digital": "discrete",
    "status": "discrete",
    "coil": "discrete",
    "alarm": "alert",
    "alarms": "alert",
}


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


SUPPORTED_FIELD_TYPES = {
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
CORE_MAP_KEYS = {"requests", "fields", "protocol", "version", "preset_id", "document", "map_id", "checksum", "immutable", "metadata"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_bit_labels(labels: Any) -> dict[str, str]:
    if isinstance(labels, dict):
        out: dict[str, str] = {}
        for bit, name in labels.items():
            try:
                number = int(bit)
            except (TypeError, ValueError):
                continue
            if 0 <= number <= 31:
                out[str(number)] = str(name)
        return out
    if isinstance(labels, list):
        out = {}
        for index, item in enumerate(labels):
            if isinstance(item, dict):
                bit = _as_int(item.get("bit", index), index)
                if 0 <= bit <= 31:
                    out[str(bit)] = str(item.get("name") or item.get("label") or item)
            else:
                out[str(index)] = str(item)
        return out
    return {}


def adapt_legacy_map(
    payload: dict[str, Any],
    *,
    protocol: VmProtocol = VmProtocol.MODBUS_RTU,
    preset_id: str | None = None,
    version: str = "legacy-1",
) -> MapDocument:
    """Accept any map that has the core requests/fields shape; extra keys stay."""
    if not isinstance(payload, dict):
        raise ValueError("В корне карты должен быть объект")
    if not isinstance(payload.get("requests"), list) and isinstance(payload.get("document"), dict):
        nested = payload["document"]
        if isinstance(nested, dict):
            payload = {key: value for key, value in payload.items() if key != "document"}
            payload.update({key: value for key, value in nested.items() if key not in payload or key in {"requests", "fields"}})
    if not isinstance(payload, dict):
        raise ValueError("В корне карты должен быть объект")
    raw_requests = payload.get("requests")
    raw_fields = payload.get("fields")
    if not isinstance(raw_requests, list) or not isinstance(raw_fields, list):
        raise ValueError("Минимальная структура карты: массивы requests и fields")
    if not raw_fields:
        raise ValueError("В карте нет fields — добавьте хотя бы одно поле с name")
    if len(raw_fields) > 10_000 or len(raw_requests) > 1_000:
        raise ValueError("Карта слишком большая")

    warnings: list[str] = []
    requests: list[dict[str, Any]] = []
    request_names: set[str] = set()
    for index, request in enumerate(raw_requests):
        if not isinstance(request, dict):
            warnings.append(f"requests[{index}] пропущен: это не объект")
            continue
        name = str(request.get("name", "")).strip()
        if not name:
            warnings.append(f"requests[{index}] пропущен: нет name")
            continue
        if name in request_names:
            warnings.append(f"запрос {name} повторён, оставлен первый")
            continue
        fc = _as_int(request.get("fc", 3), 3)
        if fc not in {1, 2, 3, 4}:
            warnings.append(f"запрос {name}: fc={request.get('fc')} не поддерживается, берём 3")
            fc = 3
        address = _as_int(request.get("address", 0), 0)
        count = _as_int(request.get("count", 1), 1)
        max_count = 2000 if fc in {1, 2} else 125
        if address < 0:
            address = 0
        if address > 0xFFFF:
            address = 0xFFFF
        if count < 1:
            count = 1
        if count > max_count:
            warnings.append(f"запрос {name}: count урезан до {max_count}")
            count = max_count
        if address + count > 0x10000:
            count = max(1, 0x10000 - address)
        item = dict(request)
        item["name"] = name
        item["fc"] = fc
        item["address"] = address
        item["count"] = count
        request_names.add(name)
        requests.append(item)

    needs_source = any(
        isinstance(field, dict) and str(field.get("type", "uint16")).lower() != "expr"
        for field in raw_fields
    )
    if needs_source and not requests:
        raise ValueError("Нужен хотя бы один запрос в requests с полем name")

    request_by_name = {str(item["name"]): item for item in requests}
    fields: list[dict[str, Any]] = []
    field_names: set[str] = set()
    for index, field in enumerate(raw_fields):
        if not isinstance(field, dict):
            warnings.append(f"fields[{index}] пропущен: это не объект")
            continue
        name = str(field.get("name", "")).strip()
        if not name:
            warnings.append(f"fields[{index}] пропущен: нет name")
            continue
        if name in field_names:
            warnings.append(f"поле {name} повторено, оставлено первое")
            continue
        item = dict(field)
        item["name"] = name
        item["address"] = max(0, _as_int(field.get("address", 0), 0))
        field_type = FIELD_TYPE_ALIASES.get(str(field.get("type", "uint16")).lower(), str(field.get("type", "uint16")).lower())
        if field_type not in SUPPORTED_FIELD_TYPES:
            warnings.append(f"поле {name}: type {field_type} неизвестен, читаем как uint16")
            field_type = "uint16"
        item["type"] = field_type
        if "bit" in item:
            bit = _as_int(item.get("bit"), 0)
            if not 0 <= bit <= 31:
                warnings.append(f"поле {name}: bit вне 0..31, бит игнорируем")
                item.pop("bit", None)
            else:
                item["bit"] = bit
        if field_type == "expr" and not str(item.get("expr", "")).strip():
            warnings.append(f"поле {name}: expr пустой, поле пропущено")
            continue
        if field_type != "expr":
            source = str(item.get("source", "")).strip()
            if not source and len(request_by_name) == 1:
                source = next(iter(request_by_name))
            if source not in request_by_name:
                warnings.append(f"поле {name}: неизвестный source {source or '∅'}, поле пропущено")
                continue
            item["source"] = source
            width = 2 if field_type in {"uint32", "uint32_be", "uint32_le", "int32", "int32_be"} else 1
            request = request_by_name[source]
            needed = item["address"] + width
            if needed > int(request.get("count", 1)):
                warnings.append(f"поле {name}: адрес выходит за count запроса {source}, значение может быть 0")
        if field_type == "bitfield":
            item["bits"] = _normalize_bit_labels(item.get("bits", item.get("bit_labels", {})))
        kind = str(item.get("kind") or item.get("channel") or "").strip().lower()
        kind = FIELD_KIND_ALIASES.get(kind, kind)
        if kind in {"analog", "discrete", "alert"}:
            item["kind"] = kind
        elif kind:
            warnings.append(f"поле {name}: kind {kind} игнорируем")
            item.pop("kind", None)
        for numeric_key in ("scale", "offset"):
            if numeric_key in item:
                try:
                    item[numeric_key] = float(item[numeric_key])
                except (TypeError, ValueError):
                    warnings.append(f"поле {name}: {numeric_key} не число, игнорируем")
                    item.pop(numeric_key, None)
        if "decimals" in item:
            decimals = _as_int(item.get("decimals"), -1)
            if decimals < 0 or decimals > 12:
                warnings.append(f"поле {name}: decimals игнорируем")
                item.pop("decimals", None)
            else:
                item["decimals"] = decimals
        field_names.add(name)
        fields.append(item)

    if not fields:
        raise ValueError("После проверки не осталось рабочих fields. Нужны объекты с name, а для чтения — source из requests")

    extra = {key: value for key, value in payload.items() if key not in CORE_MAP_KEYS}
    metadata: dict[str, Any] = {"source": "legacy"}
    if extra:
        metadata["extra"] = extra
    if warnings:
        metadata["adapt_warnings"] = warnings
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
        metadata=metadata,
    )


ALERT_FIELD_NAMES = {"active_alarms", "active_status", "alarms"}
CHANNEL_KINDS = {"analog", "discrete", "alert"}


def field_label(field: dict[str, Any], fallback: str | None = None) -> str:
    """Human-readable name from a map field: display_name first, then legacy keys."""
    name = str(fallback if fallback is not None else field.get("name") or "").strip()
    for key in ("display_name", "label", "title", "ru_name", "description"):
        value = field.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return name


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


__all__ = ["adapt_legacy_map", "diagnose_read", "field_channel", "field_label", "parse_batch", "parse_source_values", "split_channels"]
