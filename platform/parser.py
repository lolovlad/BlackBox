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
    for request in requests:
        if not isinstance(request, dict) or not str(request.get("name", "")).strip():
            raise ValueError("each request requires a name")
        try:
            address = int(request.get("address", 0))
            count = int(request.get("count", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("request address/count must be integers") from exc
        if address < 0 or count < 1 or count > 65535:
            raise ValueError("request address/count is outside the valid range")
    for field in fields:
        if not isinstance(field, dict) or not str(field.get("name", "")).strip():
            raise ValueError("each field requires a name")
        if int(field.get("address", 0)) < 0:
            raise ValueError("field address must be non-negative")
        if "bit" in field and not 0 <= int(field["bit"]) <= 31:
            raise ValueError("field bit must be between 0 and 31")
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
            field_type = field.get("type", "uint16")
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
                elif field_type == "uint32_be":
                    lo = values[address + 1] if address + 1 < len(values) else 0
                    value = (int(raw) << 16) | int(lo)
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
                if field.get("round") and isinstance(value, float):
                    value = round(value, 3)
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
