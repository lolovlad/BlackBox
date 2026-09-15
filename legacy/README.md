# Legacy BlackBox - NO NEW FEATURES

This directory contains the pre-vNext application and exists only to keep the
lab stand operational while its capabilities move to the new services.

Rules:

- only minimal lab-blocking bug fixes are allowed here;
- do not implement vNext features in this tree;
- migrate a capability to the new tree, switch callers, then delete its legacy
  implementation;
- the entire directory is removed in Phase 6.

Pull requests that touch this directory require the `legacy-bugfix` label and
code-owner review.

The root-level `src`, `blackbox`, and `modbus_acquire` packages are temporary
import adapters. They deliberately contain no product logic.

## Temporary direct launch

From the repository root:

```sh
sh legacy/scripts/linux/create_env.sh
sh legacy/scripts/linux/run_blackbox.sh
```

The old documentation is preserved in
[`README_PRE_VNEXT.md`](README_PRE_VNEXT.md),
[`DEPLOY_ON_DEVICE_RU.md`](DEPLOY_ON_DEVICE_RU.md), and
[`LINUX_AUTOSTART.md`](LINUX_AUTOSTART.md).
