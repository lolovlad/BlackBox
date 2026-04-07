import argparse
import ast
import json
import time
from pathlib import Path

import minimalmodbus
import serial


class SafeExprEvaluator(ast.NodeVisitor):
    ALLOWED_NODES = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.USub,
        ast.UAdd,
        ast.Load,
        ast.Name,
        ast.Constant,
        ast.Call,
    )

    ALLOWED_CALLS = {"int": int, "float": float, "round": round, "abs": abs, "max": max, "min": min}

    def __init__(self, variables):
        self.variables = variables

    def visit(self, node):
        if not isinstance(node, self.ALLOWED_NODES):
            raise ValueError(f"Unsupported expression element: {type(node).__name__}")
        return super().visit(node)

    def visit_Expression(self, node):
        return self.visit(node.body)

    def visit_Constant(self, node):
        return node.value

    def visit_Name(self, node):
        return self.variables.get(node.id, 0)

    def visit_UnaryOp(self, node):
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise ValueError("Unsupported unary operation")

    def visit_BinOp(self, node):
        left = self.visit(node.left)
        right = self.visit(node.right)
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            return left / right
        if isinstance(op, ast.FloorDiv):
            return left // right
        if isinstance(op, ast.Mod):
            return left % right
        if isinstance(op, ast.Pow):
            return left ** right
        raise ValueError("Unsupported binary operation")

    def visit_Call(self, node):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only direct function calls are allowed")
        fn_name = node.func.id
        if fn_name not in self.ALLOWED_CALLS:
            raise ValueError(f"Call '{fn_name}' is not allowed")
        args = [self.visit(arg) for arg in node.args]
        return self.ALLOWED_CALLS[fn_name](*args)


def eval_expr(expr, variables):
    parsed = ast.parse(expr, mode="eval")
    return SafeExprEvaluator(variables).visit(parsed)


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_instrument(port, slave_id, baudrate, timeout):
    instrument = minimalmodbus.Instrument(port, slave_id, mode=minimalmodbus.MODE_RTU)
    instrument.serial.baudrate = baudrate
    instrument.serial.bytesize = 8
    instrument.serial.parity = serial.PARITY_NONE
    instrument.serial.stopbits = 1
    instrument.serial.timeout = timeout
    instrument.clear_buffers_before_each_transaction = True
    return instrument


def poll_requests(instrument, requests, verbose=False):
    data_by_source = {}
    errors = []
    for req in requests:
        name = req["name"]
        fc = req["fc"]
        address = req["address"]
        count = req["count"]
        try:
            if fc == 3:
                values = instrument.read_registers(address, count)
            elif fc == 1:
                values = instrument.read_bits(address, count, functioncode=1)
            else:
                raise ValueError(f"Unsupported function code: {fc}")
            data_by_source[name] = values
        except Exception as exc:
            data_by_source[name] = []
            errors.append((name, exc))
            if verbose:
                print(f"[WARN] request '{name}' failed: {type(exc).__name__}: {exc}")
    return data_by_source, errors


def parse_fields(config, raw_sources):
    result = {}
    bit_maps = config.get("bit_maps", {})

    for field in config.get("fields", []):
        f_type = field.get("type", "uint16")
        name = field["name"]

        if f_type == "expr":
            try:
                result[name] = eval_expr(field["expr"], result)
            except Exception:
                result[name] = 0
            continue

        if f_type == "bit_aggregate":
            map_ref = field.get("map_ref", "")
            register_prefix = field.get("register_prefix", "")
            reg_map = bit_maps.get(map_ref, {})
            active = []
            for reg_str, bits in reg_map.items():
                reg_val = int(result.get(f"{register_prefix}{reg_str}", 0))
                for bit_str, label in bits.items():
                    if reg_val & (1 << int(bit_str)):
                        active.append(label)
            result[name] = active
            continue

        source = field.get("source")
        address = int(field.get("address", 0))
        values = raw_sources.get(source, [])

        if f_type == "uint32_be":
            hi = int(values[address]) if address < len(values) else 0
            lo = int(values[address + 1]) if address + 1 < len(values) else 0
            value = (hi << 16) | lo
        elif f_type == "bool":
            value = bool(values[address]) if address < len(values) else False
        else:
            value = int(values[address]) if address < len(values) else 0

        if "bit" in field:
            value = bool(int(value) & (1 << int(field["bit"])))

        if "expr" in field and f_type != "expr":
            try:
                value = eval_expr(field["expr"], {"x": value, **result})
            except Exception:
                pass

        result[name] = value

    for status_name in result.get("active_status", []):
        result[status_name] = True

    return result


def print_snapshot(data, errors):
    print("=" * 80)
    print("DEIF GEMPAC snapshot")
    print(f"Poll errors: {len(errors)}")
    print("-" * 80)
    for key in sorted(data.keys()):
        print(f"{key}: {data[key]}")
    print("=" * 80)


def run_loop(args):
    config = load_config(args.config)
    instrument = build_instrument(args.port, args.slave_id, args.baudrate, args.timeout)

    iteration = 0
    while True:
        raw_sources, errors = poll_requests(instrument, config["requests"], args.verbose)
        parsed = parse_fields(config, raw_sources)
        print_snapshot(parsed, errors)

        iteration += 1
        if args.once or (args.iterations and iteration >= args.iterations):
            break
        time.sleep(args.interval)


def parse_args():
    parser = argparse.ArgumentParser(description="Universal Modbus parser from settings.json")
    parser.add_argument("--config", default="settings/settings.json", help="Path to settings JSON")
    parser.add_argument("--port", default="COM3", help="Serial port (example: COM3)")
    parser.add_argument("--slave-id", type=int, default=1)
    parser.add_argument("--baudrate", type=int, default=9600)
    parser.add_argument("--timeout", type=float, default=0.35)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--iterations", type=int, default=0, help="Stop after N polls (0 = infinite)")
    parser.add_argument("--once", action="store_true", help="Single poll and exit")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"Config not found: {args.config}")
    run_loop(args)
