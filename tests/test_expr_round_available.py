from __future__ import annotations

from src.webui.modbus_service import parse_fields


def test_expr_round_is_available_for_uint16_expr() -> None:
    cfg = {
        "requests": [{"name": "hr", "fc": 3, "address": 1, "count": 2}],
        "fields": [
            {"name": "Gov.Reg.Value", "source": "hr", "address": 0, "type": "uint16", "expr": "round((x - 512) * 100.0 / 511.0, 2)"},
            {"name": "AVR.Reg.Value", "source": "hr", "address": 1, "type": "uint16", "expr": "round((x - 512) * 100.0 / 511.0, 2)"},
        ],
    }
    out = parse_fields(cfg, {"hr": [500, 1023]})
    assert out["Gov.Reg.Value"] == round((500 - 512) * 100.0 / 511.0, 2)
    assert out["AVR.Reg.Value"] == round((1023 - 512) * 100.0 / 511.0, 2)


def test_auto_round_defaults_to_3_decimals() -> None:
    cfg = {
        "requests": [{"name": "hr", "fc": 3, "address": 1, "count": 1}],
        "fields": [
            {"name": "x", "source": "hr", "address": 0, "type": "uint16", "expr": "x / 3"},
        ],
    }
    out = parse_fields(cfg, {"hr": [1]})
    assert abs(out["x"] - (1 / 3)) < 1e-12


def test_auto_round_can_be_disabled_per_field() -> None:
    cfg = {
        "requests": [{"name": "hr", "fc": 3, "address": 1, "count": 1}],
        "fields": [
            {"name": "x", "source": "hr", "address": 0, "type": "uint16", "expr": "x / 3", "round": True},
        ],
    }
    out = parse_fields(cfg, {"hr": [1]})
    assert out["x"] == round((1 / 3), 3)

