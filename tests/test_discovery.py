from __future__ import annotations

from pathlib import Path

from services.hub.discovery import (
    discover_can_resources,
    discover_gpio_resources,
    discover_tcp_resources,
    hub_serial_path,
    human_bytes,
    is_usable_can_interface,
    is_usable_gpio_chip,
    is_usable_serial_port,
    parse_arp_neighbors,
    parse_tcp_endpoint,
    prefer_serial_paths,
    tcp_hints_from_vms,
)


class _OpenOk:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_serial_filter_and_alias_collapse():
    assert not is_usable_serial_port("/dev/tty")
    assert not is_usable_serial_port("/dev/tty0")
    assert is_usable_serial_port("/dev/ttyAMA0")
    assert is_usable_serial_port("/dev/ttyAMA10")
    assert is_usable_serial_port("/dev/serial0")
    resolve = {
        "/dev/serial0": "/real/ttyAMA0",
        "/dev/ttyAMA0": "/real/ttyAMA0",
        "/dev/ttyUSB0": "/real/usb",
        "/dev/serial/by-id/usb-FTDI": "/real/usb",
    }.get
    assert prefer_serial_paths(
        ["/dev/serial0", "/dev/ttyAMA0", "/dev/ttyUSB0", "/dev/serial/by-id/usb-FTDI"],
        resolve=resolve,
    ) == ["/dev/serial/by-id/usb-FTDI", "/dev/ttyAMA0"]


def test_can_and_gpio_skip_virtual(monkeypatch):
    assert is_usable_can_interface("can0")
    assert is_usable_can_interface("slcan1")
    assert not is_usable_can_interface("vcan0")
    assert not is_usable_can_interface("eth0")
    assert is_usable_gpio_chip("/dev/gpiochip0")
    assert not is_usable_gpio_chip("/dev/gpiochip1", label="gpio-mockup")
    monkeypatch.setenv("BB_DISCOVERY_INCLUDE_VIRTUAL", "1")
    assert is_usable_can_interface("vcan0")
    assert is_usable_gpio_chip("/dev/gpiochip1", label="gpio-mockup")


def test_can_sysfs_reads_bitrate_and_skips_ethernet(tmp_path: Path):
    net = tmp_path / "net"
    net.mkdir()
    can0 = net / "can0"
    eth0 = net / "eth0"
    vcan0 = net / "vcan0"
    for iface, kind, state, bitrate in (
        (can0, "280", "up", "250000"),
        (eth0, "1", "up", ""),
        (vcan0, "280", "up", "1000000"),
    ):
        iface.mkdir()
        (iface / "type").write_text(kind)
        (iface / "operstate").write_text(state)
        if bitrate:
            (iface / "can_bittiming").mkdir()
            (iface / "can_bittiming" / "bitrate").write_text(bitrate)
    items = discover_can_resources(sys_class_net=net)
    assert [item["resource_id"] for item in items] == ["can:can0"]
    assert items[0]["metadata"]["bitrate"] == 250000
    assert items[0]["metadata"]["operstate"] == "up"


def test_gpio_sysfs_label(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BB_DISCOVERY_GPIO_PATHS", "/dev/gpiochip0")
    chip = tmp_path / "gpiochip0"
    chip.mkdir()
    (chip / "label").write_text("pinctrl-bcm2711")
    (chip / "ngpio").write_text("58")
    items = discover_gpio_resources(sys_bus_gpio=tmp_path, sys_class_gpio=tmp_path)
    assert items[0]["resource_id"] == "gpio:/dev/gpiochip0"
    assert items[0]["metadata"]["ngpio"] == 58
    assert "pinctrl-bcm2711" in items[0]["name"]


def test_arp_and_tcp_probe(tmp_path: Path):
    assert parse_tcp_endpoint("tcp://10.0.0.8:1502") == ("10.0.0.8", 1502)
    neighbors = parse_arp_neighbors(
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.1.20     0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0\n"
        "192.168.1.1      0x1         0x0         00:00:00:00:00:00     *        eth0\n"
        "172.17.0.2       0x1         0x2         11:22:33:44:55:66     *        docker0\n"
    )
    assert neighbors == [("192.168.1.20", "aa:bb:cc:dd:ee:ff", "eth0")]

    def opener(addr, timeout=0):
        if addr == ("10.0.0.8", 502):
            return _OpenOk()
        raise OSError("down")

    items = discover_tcp_resources(
        ["10.0.0.8:502", "10.0.0.9:502"],
        probe_network=True,
        opener=opener,
        proc_net_arp=tmp_path / "missing-arp",
    )
    by_id = {item["resource_id"]: item for item in items}
    assert by_id["tcp:10.0.0.8:502"]["metadata"]["reachable"] is True
    assert by_id["tcp:10.0.0.9:502"]["metadata"]["reachable"] is False


def test_tcp_hints_ignore_loopback_simulator():
    hints = tcp_hints_from_vms(
        [
            {"protocol": "modbus_tcp", "config": {"reader": {"host": "127.0.0.1", "tcp_port": 502}}},
            {"protocol": "modbus_tcp", "config": {"reader": {"host": "192.168.10.4", "tcp_port": 1502}}},
            {"protocol": "simulator", "config": {"reader": {"host": "10.1.1.1"}}},
        ]
    )
    assert hints == ["192.168.10.4:1502"]


def test_hub_inventory_excludes_tcp_endpoints(tmp_path: Path):
    from services.hub.discovery import discover_resources

    items = discover_resources(tmp_path, extra_tcp_endpoints=["10.0.0.8:502"], include_tcp=False)
    assert all(item.get("kind") != "tcp" for item in items)


def test_host_dev_uart_is_published_as_linux_path(tmp_path: Path, monkeypatch):
    from services.hub.discovery import as_linux_dev_path, discover_serial_resources

    root = tmp_path / "host-dev"
    root.mkdir()
    (root / "ttyAMA10").write_text("")
    (root / "serial0").write_text("")
    assert as_linux_dev_path(str(root / "ttyAMA10"), dev_root=root) == "/dev/ttyAMA10"
    monkeypatch.setenv("BB_DISCOVERY_DEV_ROOT", str(root))
    monkeypatch.setenv("BB_DISCOVERY_SERIAL_PATHS", str(root / "ttyAMA10"))
    items = discover_serial_resources()
    assert any(item["resource_id"] == "serial:/dev/ttyAMA10" and item["path"] == "/dev/ttyAMA10" for item in items)


def test_human_bytes():
    assert human_bytes(512) == "512 Б"
    assert human_bytes(1536) == "1.5 КБ"


def test_hub_serial_path_prefers_bind_mount(tmp_path: Path):
    node = tmp_path / "ttyAMA10"
    node.write_text("")
    assert hub_serial_path("/dev/ttyAMA10", dev_root=tmp_path) == str(node)
    assert hub_serial_path(str(node), dev_root=tmp_path) == str(node)
