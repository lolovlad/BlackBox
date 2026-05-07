from __future__ import annotations

import json
from pathlib import Path

from src.webui.emergency_rule_validation import validate_emergency_rule_expression


def test_emergency_rule_validation_accepts_gpio_vars(tmp_path: Path) -> None:
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    parser_path = settings_dir / "settings.json"
    parser_path.write_text(
        json.dumps(
            {
                "requests": [{"name": "hr", "fc": 3, "address": 0, "count": 1}],
                "fields": [{"name": "r0", "type": "uint16", "source": "hr", "address": 0}],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (settings_dir / "gpio_inputs.json").write_text(
        json.dumps(
            {
                "poll_interval_sec": 1,
                "pins": [
                    {"bcm_pin": 27, "name": "GPIO_27", "trigger_level": 1, "hold_sec": 0.5, "pull": "up"},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    ok, err = validate_emergency_rule_expression("GPIO_27 == True", settings_path=parser_path)
    assert ok, err

