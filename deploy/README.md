# Deployment

`compose.yaml` is the vNext deployment entrypoint. The `dev` and `prod`
profiles run the FastAPI Hub and the Docker socket-proxy. Worker containers are
created dynamically by the Hub; the `build` profile builds the simulator and
Modbus RTU worker images without starting them as static Compose services.

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

After a fast-forward update from GitHub, run `./bbctl update`. It rebuilds the
Hub and worker images, removes only dynamically-created containers carrying
the `bb.vm_id` label, starts the stack again, and runs the smoke test. Hub
metadata and Parquet data remain in `blackbox-data`. Do not use
`docker compose down -v` on the test stand.
