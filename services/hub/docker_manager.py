from __future__ import annotations

import json
import os
from typing import Any, Callable

from bb_platform.contracts import VmLifecycle


class DockerUnavailable(RuntimeError):
    pass


class DockerManager:
    def __init__(self, client: Any = None, *, enabled: bool = True) -> None:
        self.enabled = enabled
        if client is not None:
            self.client = client
        elif enabled:
            try:
                import docker
                self.client = docker.from_env()
            except Exception as exc:
                self.client = None
                self.error = str(exc)
        else:
            self.client = None
            self.error = "disabled"

    def _require(self):
        if self.client is None:
            raise DockerUnavailable(getattr(self, "error", "Docker is unavailable"))

    @staticmethod
    def device_mappings(vm: dict[str, Any]) -> list[str]:
        """Host /dev nodes the worker must see. Docker --device is fixed at create time."""
        paths: set[str] = set()

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text.startswith("/dev/") and text != "/dev/tty":
                paths.add(text)

        for resource in vm.get("read_resources", vm.get("resources", [])) or []:
            if not isinstance(resource, dict):
                continue
            add(resource.get("path"))
            metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
            aliases = metadata.get("aliases") if isinstance(metadata.get("aliases"), list) else []
            for alias in aliases:
                add(alias)
        config = vm.get("config") if isinstance(vm.get("config"), dict) else {}
        reader = config.get("reader") if isinstance(config.get("reader"), dict) else {}
        add(reader.get("port"))
        return [f"{path}:{path}:rwm" for path in sorted(paths)]

    @staticmethod
    def is_not_found(exc: Exception) -> bool:
        """Recognize Docker SDK and fake-client not-found errors."""
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        return name in {"notfound", "not_found", "keyerror"} or "no such container" in message or "not found" in message

    def create(self, vm: dict[str, Any], bootstrap_token: str, *, log: Callable[[str], None] | None = None) -> dict[str, Any]:
        self._require()
        labels = {
            "bb.vm_id": vm["id"],
            "bb.protocol": vm["protocol"],
            "bb.map_version": vm["map_version"],
            "bb.config_revision": str(vm["config_revision"]),
        }
        image = vm["worker_image"]
        environment = {
            "BB_VM_ID": vm["id"],
            "BB_HUB_URL": "http://hub:8080",
            "BB_BOOTSTRAP_TOKEN": bootstrap_token,
            "BB_MAP_VERSION": vm["map_version"],
            "BB_MODBUS_CONFIG": json.dumps(vm.get("config", {})),
            "BB_PROTOCOL_CONFIG": json.dumps(vm.get("config", {})),
            "BB_CONFIG_REVISION": str(vm.get("config_revision", 0)),
        }
        runtime = vm.get("config", {}) if isinstance(vm.get("config", {}), dict) else {}
        reader = runtime.get("reader", {}) if isinstance(runtime.get("reader", {}), dict) else {}
        environment["BB_INTERVAL"] = str(reader.get("poll_interval_sec", runtime.get("poll_interval", "0.12")))
        limits = vm.get("limits", {}) or {}
        devices = self.device_mappings(vm)
        # create (not run) so Hub can record lifecycle=created and start() separately
        create_kwargs: dict[str, Any] = {
            "name": f"bb-vm-{vm['id']}",
            "labels": labels,
            "environment": environment,
            "network": os.getenv("BB_WORKER_NETWORK", "blackbox_control"),
            "read_only": True,
            "tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": limits.get("memory", "256m"),
            "nano_cpus": int(limits.get("nano_cpus", 500_000_000)),
            "pids_limit": int(limits.get("pids", 128)),
        }
        if devices:
            create_kwargs["devices"] = devices
        try:
            container = self.client.containers.create(image, **create_kwargs)
        except Exception as exc:
            message = str(exc)
            if "already in use" in message.lower() or "conflict" in message.lower():
                raise RuntimeError(
                    f"Container name bb-vm-{vm['id']} already exists; remove the orphan or delete the VM first"
                ) from exc
            raise
        if log is not None:
            log(f"created container {getattr(container, 'id', None)} for vm {vm['id']}")
        return {"container_id": getattr(container, "id", None), "lifecycle": VmLifecycle.CREATED.value}

    def _container(self, vm: dict[str, Any]):
        self._require()
        if vm.get("container_id"):
            return self.client.containers.get(vm["container_id"])
        return self.client.containers.get(f"bb-vm-{vm['id']}")

    def bootstrap_token(self, vm: dict[str, Any]) -> str | None:
        """Recover the worker bootstrap token from an existing container.

        The token is generated per VM and passed only to that worker.  Keeping
        it in the container environment lets a Hub restart reconcile existing
        workers without invalidating all live sessions; it is never returned by
        an HTTP API.
        """
        container = self._container(vm)
        container.reload()
        config = (getattr(container, "attrs", {}) or {}).get("Config", {})
        for item in config.get("Env", []) or []:
            if isinstance(item, str) and item.startswith("BB_BOOTSTRAP_TOKEN="):
                return item.split("=", 1)[1] or None
        return None

    def start(self, vm: dict[str, Any]) -> None:
        self._container(vm).start()

    def stop(self, vm: dict[str, Any]) -> None:
        self._container(vm).stop(timeout=10)

    def restart(self, vm: dict[str, Any]) -> None:
        self._container(vm).restart(timeout=10)

    def remove(self, vm: dict[str, Any]) -> None:
        """Force-remove the VM container, including orphans named ``bb-vm-<id>``.

        A missing container is success: Hub can still delete metadata and files.
        """
        self._require()
        keys: list[str] = []
        if vm.get("container_id"):
            keys.append(str(vm["container_id"]))
        keys.append(f"bb-vm-{vm['id']}")
        seen: set[str] = set()
        for key in keys:
            if not key or key in seen:
                continue
            seen.add(key)
            try:
                self.client.containers.get(key).remove(force=True)
            except Exception as exc:
                if self.is_not_found(exc):
                    continue
                raise

    def inspect(self, vm: dict[str, Any]) -> dict[str, Any]:
        container = self._container(vm)
        container.reload()
        state = (getattr(container, "attrs", {}) or {}).get("State", {})
        status = state.get("Status", "unknown")
        lifecycle = {
            "created": VmLifecycle.CREATED.value,
            "running": VmLifecycle.RUNNING.value,
            "exited": VmLifecycle.STOPPED.value,
            "dead": VmLifecycle.FAILED.value,
        }.get(status, VmLifecycle.UNKNOWN.value)
        return {"container_id": getattr(container, "id", None), "lifecycle": lifecycle, "health": state.get("Health", {}).get("Status", "unknown"), "error": state.get("Error") or None}

    def logs(self, vm: dict[str, Any], *, tail: int = 200):
        raw = self._container(vm).logs(stream=False, timestamps=True, tail=max(1, min(tail, 5000)))
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        return text.splitlines()
