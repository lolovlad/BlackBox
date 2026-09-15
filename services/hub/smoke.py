"""Small end-to-end smoke test used by ``bbctl smoke``."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import time
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener


class SmokeClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self.username = username
        self.password = password

    def request(self, path: str, *, method: str = "GET", payload: dict | None = None, csrf: bool = False, _retry_auth: bool = True) -> dict:
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Accept": "application/json"}
        cookie_header = self.cookie_header()
        if cookie_header:
            # Explicitly pin the current jar contents.  This keeps the smoke
            # client deterministic across urllib cookie-handler versions and
            # also exercises the API's documented bearer fallback.
            headers["Cookie"] = cookie_header
            access = next((cookie.value for cookie in self.jar if cookie.name == "bb_access"), "")
            if access:
                headers["Authorization"] = f"Bearer {access}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-CSRF-Token"] = next((cookie.value for cookie in self.jar if cookie.name == "bb_csrf"), "")
        request = Request(f"{self.base_url}{path}", data=body, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.loads(response.read().decode())
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if _retry_auth and exc.code == 401 and path not in {"/api/v1/auth/login", "/api/v1/auth/refresh"}:
                try:
                    self.request("/api/v1/auth/refresh", method="POST", _retry_auth=False)
                    return self.request(path, method=method, payload=payload, csrf=csrf, _retry_auth=False)
                except Exception:
                    pass
            raise RuntimeError(f"{method} {path}: HTTP {exc.code} {detail}") from exc

    def login(self) -> None:
        self.request("/api/v1/auth/login", method="POST", payload={"username": self.username, "password": self.password})
        me = self.request("/api/v1/auth/me")
        if me.get("role") != "admin":
            raise RuntimeError(f"bootstrap account is not admin: {me}")

    def cookie_header(self) -> str:
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.jar)


def websocket_snapshot(client: SmokeClient) -> dict:
    host_port = client.base_url.removeprefix("http://").removeprefix("https://")
    host, port = host_port.split(":", 1)
    sock = socket.create_connection((host, int(port)), timeout=10)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    request = (
        f"GET /ws/v1/events HTTP/1.1\r\nHost: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        f"Cookie: {client.cookie_header()}\r\n\r\n"
    ).encode()
    sock.sendall(request)
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("WebSocket closed during handshake")
        header += chunk
    if not header.startswith(b"HTTP/1.1 101"):
        raise RuntimeError(f"WebSocket handshake failed: {header[:200]!r}")
    frame = bytearray(header.split(b"\r\n\r\n", 1)[1])
    while len(frame) < 2:
        frame.extend(sock.recv(4096))
    first, second = frame[0], frame[1]
    if (first & 0x0F) != 1:
        raise RuntimeError(f"expected text snapshot frame, opcode={first & 0x0F}")
    length = second & 0x7F
    offset = 2
    if length == 126:
        while len(frame) < 4:
            frame.extend(sock.recv(4096))
        length = int.from_bytes(frame[2:4], "big")
        offset = 4
    elif length == 127:
        while len(frame) < 10:
            frame.extend(sock.recv(4096))
        length = int.from_bytes(frame[2:10], "big")
        offset = 10
    while len(frame) < offset + length:
        frame.extend(sock.recv(4096))
    payload = json.loads(bytes(frame[offset : offset + length]).decode())
    sock.close()
    if payload.get("type") != "snapshot":
        raise RuntimeError(f"unexpected WebSocket payload: {payload}")
    return payload


def run(base_url: str, username: str, password: str, data_root: Path) -> None:
    client = SmokeClient(base_url, username, password)
    client.login()
    name = f"smoke-{secrets.token_hex(4)}"
    vm = client.request("/api/v1/vms", method="POST", csrf=True, payload={"name": name, "protocol": "simulator", "map_version": "default-v1"})
    vm_id = vm["id"]
    try:
        client.request(f"/api/v1/vms/{vm_id}/start", method="POST", csrf=True)
        deadline = time.time() + 25
        current = {}
        while time.time() < deadline:
            current = client.request(f"/api/v1/vms/{vm_id}")
            if current.get("lifecycle") == "running":
                break
            time.sleep(1)
        if current.get("lifecycle") != "running":
            raise RuntimeError(f"simulator did not start: {current}")
        time.sleep(6)
        logs = client.request(f"/api/v1/vms/{vm_id}/logs")
        if not isinstance(logs.get("lines"), list):
            raise RuntimeError(f"logs endpoint returned an invalid payload: {logs}")
        snapshot = websocket_snapshot(client)
        if not isinstance(snapshot.get("payload"), dict):
            raise RuntimeError("WebSocket snapshot has no payload")
        parquet = list(data_root.joinpath("telemetry").rglob("*.parquet")) if data_root.exists() else []
        if not parquet:
            raise RuntimeError(f"no Parquet sample under {data_root / 'telemetry'}")
        print(f"smoke ok: vm={vm_id} lifecycle=running parquet={len(parquet)} snapshot_seq={snapshot['payload'].get('seq', 0)}")
    finally:
        try:
            client.request(f"/api/v1/vms/{vm_id}/stop", method="POST", csrf=True)
        except Exception as exc:
            print(f"smoke cleanup stop warning: {exc}")
        try:
            client.request(f"/api/v1/vms/{vm_id}", method="DELETE", csrf=True)
        except Exception as exc:
            print(f"smoke cleanup delete warning: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("BB_SMOKE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--username", default=os.getenv("BB_BOOTSTRAP_ADMIN_USERNAME", "admin"))
    parser.add_argument("--password", default=os.getenv("BB_BOOTSTRAP_ADMIN_PASSWORD", "admin"))
    parser.add_argument("--data-root", type=Path, default=Path(os.getenv("BB_DATA_ROOT", "/data")))
    args = parser.parse_args()
    run(args.url, args.username, args.password, args.data_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
