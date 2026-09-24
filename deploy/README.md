# Deployment

`compose.yaml` is the vNext deployment entrypoint. The `dev` and `prod`
profiles run the FastAPI Hub and the Docker socket-proxy. Worker containers are
created dynamically by the Hub; the `build` profile builds the simulator,
Modbus RTU and Modbus TCP worker images without starting them as static Compose
services.

`legacy.env` exists only for the transition fallback profile. Override the Hub
port with `BB_HUB_PORT`, and always set `BB_JWT_SECRET` plus bootstrap admin
credentials outside a lab.

Telemetry is written to the named `blackbox-data` volume (`/data/meta` and
`/data/telemetry`). The Hub reaches Docker only through the socket-proxy;
workers never receive the host Docker socket.

## Raspberry Pi test stand

Use a 64-bit Raspberry Pi OS installation with Docker Engine and the Compose
plugin. Clone this repository on the Pi, create `.env` from `.env.example`,
set a unique `BB_JWT_SECRET` and bootstrap admin password, then run:

```sh
export BB_PROFILE=prod
./bbctl up
./bbctl smoke
```

After a fast-forward update from GitHub, run `./bbctl update`. It stashes local
tracked changes if needed, fast-forwards, and rebuilds the Hub and worker
images. Dependency layers stay cached when only application code changed, so
a routine update does not reinstall Python packages. Set `BB_PULL=1` to also
pull newer base images. The command then removes only dynamically-created
containers carrying the `bb.vm_id` label, starts the stack again, and runs the
smoke test. Hub metadata and Parquet data remain in `blackbox-data`. Do not
use `docker compose down -v` on the test stand.

If a stand still on an older `bbctl` stops with `Your local changes to the
following files would be overwritten by merge: bbctl`, the executable bit from
`chmod +x bbctl` is usually the only local change. Discard it and rerun:

```sh
git checkout -- bbctl
chmod +x bbctl
./bbctl update
```

To use an SSD, mount it on the Pi (for example at `/mnt/blackbox-ssd`), set
`BB_EXTERNAL_STORAGE_HOST_PATH=/mnt/blackbox-ssd` in `.env`, and start with:

```sh
export BB_PROFILE=prod
export BB_COMPOSE_OVERRIDE=deploy/compose.pi.yaml
./bbctl up
./bbctl smoke
```

The admin resource scan will show `storage:ssd` separately from serial, CAN
and GPIO read resources. Approve the SSD and select it as the VM storage
target; only the Hub writes Parquet there.
