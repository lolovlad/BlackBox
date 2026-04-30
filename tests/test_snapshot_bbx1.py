from __future__ import annotations

import json

from src.webui.modbus_service import configure_settings_path, decode_to_processed, pack_snapshot, reload_settings_cache


def test_bbx1_snapshot_stores_all_request_segments(tmp_path):
    settings = {
        "requests": [
            {"name": "hr", "fc": 3, "address": 1, "count": 4},
            {"name": "coils", "fc": 1, "address": 16, "count": 8},
            {"name": "ctl_time", "fc": 3, "address": 19000, "count": 7},
        ],
        "fields": [
            {"name": "a", "type": "uint16", "source": "hr", "address": 0},
            {"name": "b", "type": "uint16", "source": "ctl_time", "address": 6},
            {"name": "c", "type": "bool", "source": "coils", "address": 3},
        ],
    }
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")

    configure_settings_path(str(settings_path))
    reload_settings_cache()

    sources = {"hr": [10, 11, 12, 13], "coils": [False, False, False, True, False, False, False, False], "ctl_time": [0, 1, 2, 3, 4, 5, 99]}
    blob = pack_snapshot(sources)

    processed = decode_to_processed(blob)
    assert processed["a"] == 10
    assert processed["b"] == 99
    assert processed["c"] is True

