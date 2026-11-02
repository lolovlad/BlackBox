from __future__ import annotations

import pytest

from services.hub import network_config
from services.hub.network_config import NetworkConfigError, _validate_network


def test_static_network_settings_require_valid_same_subnet_gateway():
    assert _validate_network({"dhcp": False, "address": "192.168.20.5/24", "gateway": "192.168.20.1", "dns": "1.1.1.1, 8.8.8.8"}) == {
        "dhcp": False,
        "address": "192.168.20.5/24",
        "gateway": "192.168.20.1",
        "dns": ["1.1.1.1", "8.8.8.8"],
    }
    with pytest.raises(NetworkConfigError, match="той же подсети"):
        _validate_network({"dhcp": False, "address": "192.168.20.5/24", "gateway": "192.168.21.1"})


def test_dhcp_discards_manual_ip_values():
    assert _validate_network({"dhcp": True, "address": "invalid", "gateway": "invalid", "dns": "invalid"}) == {
        "dhcp": True,
        "address": "",
        "gateway": "",
        "dns": [],
    }


def test_active_network_profile_does_not_require_interface_name(monkeypatch):
    def fake_nmcli(*args, **_kwargs):
        values = {
            ("-g", "GENERAL.CONNECTION", "device", "show", "eth0"): "netplan-eth0",
            ("-g", "connection.type", "connection", "show", "netplan-eth0"): "802-3-ethernet",
            ("-g", "802-11-wireless.mode", "connection", "show", "netplan-eth0"): "",
        }
        return values[args]

    monkeypatch.setattr(network_config, "_nmcli", fake_nmcli)
    assert network_config._profile_for("eth0") == "netplan-eth0"
