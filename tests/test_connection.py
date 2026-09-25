from __future__ import annotations

from services.hub.connection import PROFILES, connection_profile, inventory_kinds


def test_tcp_is_a_dialed_endpoint_not_an_inventory_device():
    tcp = connection_profile("modbus_tcp")
    assert tcp.discovers is False
    assert tcp.requires_resource is False
    assert tcp.resource_kind is None
    assert tcp.probe_read is True
    assert tcp.scan_label is None
    assert tcp.link == "network"
    assert tcp.as_dict()["shows_ping"] is True
    assert tcp.as_dict()["shows_link"] is True


def test_physical_buses_scan_and_lease_exclusive_nodes():
    rtu = connection_profile("modbus_rtu")
    can = connection_profile("can")
    assert rtu.discovers and rtu.requires_resource and rtu.exclusive
    assert can.discovers and can.requires_resource and can.exclusive
    assert rtu.resource_kind == "serial"
    assert can.resource_kind == "can"
    assert rtu.probe_read and can.probe_read
    assert inventory_kinds() == ("serial", "can")
    assert "tcp" not in inventory_kinds()
    assert "gpio" not in inventory_kinds()


def test_gpio_panel_is_not_an_inventory_device():
    gpio = connection_profile("gpio")
    assert gpio.discovers is False
    assert gpio.requires_resource is False
    assert gpio.probe_read is False
    assert gpio.scan_label is None


def test_every_protocol_has_a_profile():
    assert set(PROFILES) == {"simulator", "modbus_rtu", "modbus_tcp", "can", "gpio"}
