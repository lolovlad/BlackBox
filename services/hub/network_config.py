from __future__ import annotations

import ipaddress
import shutil
import subprocess
from typing import Any


class NetworkConfigError(RuntimeError):
    pass


def _nmcli(*args: str, timeout: int = 20) -> str:
    executable = shutil.which("nmcli")
    if executable is None:
        raise NetworkConfigError("Управление сетью доступно только на устройстве с NetworkManager")
    try:
        result = subprocess.run(
            [executable, "--colors", "no", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NetworkConfigError(f"NetworkManager не ответил: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise NetworkConfigError(detail or "NetworkManager отклонил настройки")
    return result.stdout.strip()


def _profile_for(interface: str, *, wifi_mode: str | None = None) -> str:
    active = _nmcli("-g", "GENERAL.CONNECTION", "device", "show", interface)
    if active and active != "--":
        if not wifi_mode:
            return active
        kind = _nmcli("-g", "connection.type", "connection", "show", active)
        mode = _nmcli("-g", "802-11-wireless.mode", "connection", "show", active)
        if kind == "802-11-wireless" and mode == wifi_mode:
            return active
    profiles: list[str] = []
    profiles.extend(item for item in _nmcli("-g", "NAME", "connection", "show").splitlines() if item and item not in profiles)
    for profile in profiles:
        configured_interface = _nmcli("-g", "connection.interface-name", "connection", "show", profile)
        if configured_interface != interface:
            continue
        if wifi_mode:
            kind = _nmcli("-g", "connection.type", "connection", "show", profile)
            mode = _nmcli("-g", "802-11-wireless.mode", "connection", "show", profile)
            if kind != "802-11-wireless" or mode != wifi_mode:
                continue
        return profile
    raise NetworkConfigError(f"Профиль интерфейса {interface} не найден")


def _connection_values(profile: str, fields: list[str]) -> dict[str, str]:
    return {
        field: _nmcli("-g", field, "connection", "show", profile)
        for field in fields
    }


def _device_values(interface: str) -> dict[str, Any]:
    raw = _nmcli("-g", "GENERAL.STATE,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "device", "show", interface)
    lines = raw.splitlines()
    return {
        "state": lines[0] if lines else "",
        "addresses": lines[1:2],
        "gateway": lines[2] if len(lines) > 2 else "",
        "dns": [item for item in lines[3:] if item],
    }


def network_status() -> dict[str, Any]:
    ethernet = _profile_for("eth0")
    access_point = _profile_for("wlan0", wifi_mode="ap")
    eth = _connection_values(ethernet, ["ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns"])
    wifi = _connection_values(
        access_point,
        ["802-11-wireless.ssid", "ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns"],
    )
    wifi_device = _device_values("wlan0")
    security = _nmcli("-g", "802-11-wireless-security.key-mgmt", "connection", "show", access_point)
    return {
        "ethernet": {
            "profile": ethernet,
            "interface": "eth0",
            "dhcp": eth["ipv4.method"] == "auto",
            "address": (eth["ipv4.addresses"].split() or [""])[0],
            "gateway": eth["ipv4.gateway"],
            "dns": ", ".join(eth["ipv4.dns"].split()),
            "current": _device_values("eth0"),
        },
        "wifi": {
            "profile": access_point,
            "interface": "wlan0",
            "enabled": wifi_device["state"].startswith("100"),
            "ssid": wifi["802-11-wireless.ssid"],
            "address": (wifi["ipv4.addresses"].split() or [""])[0],
            "gateway": wifi["ipv4.gateway"],
            "dhcp": wifi["ipv4.method"] == "shared",
            "password_configured": security == "wpa-psk",
            "current": wifi_device,
        },
    }


def _validate_network(payload: dict[str, Any]) -> dict[str, Any]:
    dhcp = bool(payload.get("dhcp", True))
    address = str(payload.get("address") or "").strip()
    gateway = str(payload.get("gateway") or "").strip()
    dns = [item.strip() for item in str(payload.get("dns") or "").replace(";", ",").split(",") if item.strip()]
    if not dhcp:
        try:
            interface = ipaddress.ip_interface(address)
            if interface.version != 4:
                raise ValueError
            if gateway and ipaddress.ip_address(gateway).version != 4:
                raise ValueError
            if gateway and ipaddress.ip_address(gateway) not in interface.network:
                raise NetworkConfigError("Шлюз должен находиться в той же подсети, что и IP-адрес")
            for server in dns:
                if ipaddress.ip_address(server).version != 4:
                    raise ValueError
        except ValueError as exc:
            raise NetworkConfigError("Укажите корректные IPv4-адреса и маску сети") from exc
        if not gateway:
            raise NetworkConfigError("Для статического IPv4 укажите шлюз")
    else:
        address = gateway = ""
        dns = []
    return {"dhcp": dhcp, "address": address, "gateway": gateway, "dns": dns}


def apply_network(payload: dict[str, Any]) -> dict[str, Any]:
    ethernet = _validate_network(payload.get("ethernet") if isinstance(payload.get("ethernet"), dict) else {})
    wifi_payload = payload.get("wifi") if isinstance(payload.get("wifi"), dict) else {}
    ssid = str(wifi_payload.get("ssid") or "").strip()
    password = str(wifi_payload.get("password") or "")
    wifi_dhcp = bool(wifi_payload.get("dhcp", True))
    wifi_address = str(wifi_payload.get("address") or "").strip()
    if wifi_payload.get("enabled", True) and not 1 <= len(ssid.encode("utf-8")) <= 32:
        raise NetworkConfigError("Имя Wi-Fi должно содержать от 1 до 32 байт")
    if password and not 8 <= len(password) <= 63:
        raise NetworkConfigError("Пароль Wi-Fi должен содержать от 8 до 63 символов")
    try:
        wifi_interface = ipaddress.ip_interface(wifi_address)
        if wifi_interface.version != 4 or wifi_interface.network.prefixlen > 29:
            raise ValueError
    except ValueError as exc:
        raise NetworkConfigError("Укажите IPv4-адрес точки доступа с маской, например 10.200.200.1/24") from exc
    eth_profile = _profile_for("eth0")
    wifi_profile = _profile_for("wlan0", wifi_mode="ap")
    eth_fields = ["ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns", "ipv4.dns-search", "ipv4.never-default"]
    wifi_fields = ["802-11-wireless.ssid", "ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns", "ipv4.dns-search", "802-11-wireless-security.key-mgmt", "802-11-wireless-security.psk"]
    before = {eth_profile: _connection_values(eth_profile, eth_fields), wifi_profile: _connection_values(wifi_profile, wifi_fields)}

    def modify(profile: str, values: dict[str, str]) -> None:
        args = ["connection", "modify", profile]
        for key, value in values.items():
            args.extend([key, value])
        _nmcli(*args)

    eth_values = {
        "ipv4.method": "auto" if ethernet["dhcp"] else "manual",
        "ipv4.addresses": ethernet["address"],
        "ipv4.gateway": ethernet["gateway"],
        "ipv4.dns": ",".join(ethernet["dns"]),
        "ipv4.dns-search": "",
        "ipv4.never-default": "no",
    }
    wifi_values = {
        "802-11-wireless.ssid": ssid,
        "ipv4.method": "shared" if wifi_dhcp else "manual",
        "ipv4.addresses": wifi_address,
        "ipv4.gateway": "",
        "ipv4.dns": "",
        "ipv4.dns-search": "",
        "802-11-wireless-security.key-mgmt": "wpa-psk",
    }
    try:
        modify(eth_profile, eth_values)
        modify(wifi_profile, wifi_values)
        if password:
            _nmcli("connection", "modify", wifi_profile, "802-11-wireless-security.psk", password)
        _nmcli("connection", "up", eth_profile, timeout=45)
        if wifi_payload.get("enabled", True):
            _nmcli("connection", "up", wifi_profile, timeout=45)
        else:
            _nmcli("connection", "down", wifi_profile)
    except NetworkConfigError as exc:
        for profile, values in before.items():
            try:
                modify(profile, values)
                _nmcli("connection", "up", profile, timeout=45)
            except NetworkConfigError:
                pass
        raise NetworkConfigError(f"Не удалось применить настройки сети: {exc}") from exc
    return network_status()
