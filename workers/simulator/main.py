from __future__ import annotations

import os
import time

from bb_platform.contracts import VmProtocol
from workers.common import WorkerClient


def run() -> int:
    client = WorkerClient(protocol=VmProtocol.SIMULATOR)
    map_version = os.getenv("BB_MAP_VERSION", "default-v1")
    interval = float(os.getenv("BB_INTERVAL", "1"))
    client.register()
    while True:
        for command in client.commands():
            if command.get("action") == "apply_map" and command.get("map_version"):
                map_version = str(command["map_version"])
            client.acknowledge(command)
        client.batch({"sim": [client.seq, client.seq % 2, client.seq * 0.5]}, map_version)
        client.heartbeat()
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(run())
