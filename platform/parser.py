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
        if errors:
            tags["_parse_errors"] = errors
        samples.append(
            TagSample(
                vm_id=batch.vm_id,
                seq=sample.seq,
                captured_at=sample.captured_at,
                map_version=batch.map_version,
                tags=tags,
                quality=quality,
                protocol=batch.protocol,
            )
        )
    return samples


__all__ = ["adapt_legacy_map", "parse_batch", "parse_source_values"]
