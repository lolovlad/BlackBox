import argparse
import json
import time
from pathlib import Path

import minimalmodbus
import serial


def eval_expr(expr, context):
    return eval(expr, {"__builtins__": {}}, context)


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

        if f_type == "bitfield":
            source = field.get("source")
            address = int(field.get("address", 0))
            values = raw_sources.get(source, [])
            reg_value = int(values[address]) if address < len(values) else 0
            bits_map = field.get("bits", {})
            active = []
            for bit_idx, label in bits_map.items():
                if reg_value & (1 << int(bit_idx)):
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


def build_display_map(config):
    display_map = {}
    for field in config.get("fields", []):
        name = field.get("name")
        if not name:
            continue
        display_name = (
            field.get("display_name")
            or field.get("title")
            or field.get("ru_name")
            or field.get("label")
            or name
        )
        display_map[name] = display_name
    return display_map


def print_snapshot(data, errors, display_map):
    print("=" * 80)
    print("DEIF GEMPAC snapshot")
    print(f"Poll errors: {len(errors)}")
    print("-" * 80)
    for key in sorted(data.keys()):
        pretty_key = display_map.get(key, key)
        if pretty_key != key:
            print(f"{pretty_key} ({key}): {data[key]}")
        else:
            print(f"{key}: {data[key]}")
    print("=" * 80)


def run_loop(args):
    config = load_config(args.config)
    display_map = build_display_map(config)
    instrument = build_instrument(args.port, args.slave_id, args.baudrate, args.timeout)

    iteration = 0
    while True:
        raw_sources, errors = poll_requests(instrument, config["requests"], args.verbose)
        parsed = parse_fields(config, raw_sources)
        print_snapshot(parsed, errors, display_map)

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
