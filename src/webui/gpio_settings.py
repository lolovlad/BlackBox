from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


GPIO_INPUTS_FILENAME = "gpio_inputs.json"


class GpioPull(str, Enum):
    up = "up"
    down = "down"
    none = "none"


class GpioPinModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bcm_pin: int = Field(ge=0, le=27)
    name: str = Field(min_length=1, max_length=255)
    trigger_level: int
    hold_sec: float = Field(ge=0.0, le=60.0)
    pull: GpioPull = GpioPull.none
    invert: bool = False

    @field_validator("trigger_level")
    @classmethod
    def _level_supported(cls, v: int) -> int:
        if v not in (0, 1):
            raise ValueError("trigger_level must be 0 or 1")
        return v


class GpioInputsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_interval_sec: float = Field(default=0.05, ge=0.01, le=2.0)
    pins: list[GpioPinModel] = Field(min_length=1)

    @field_validator("pins")
    @classmethod
    def _unique_pins(cls, pins: list[GpioPinModel]) -> list[GpioPinModel]:
        seen: set[int] = set()
        for p in pins:
            if p.bcm_pin in seen:
                raise ValueError(f"Duplicate bcm_pin: {p.bcm_pin}")
            seen.add(p.bcm_pin)
        return pins


def gpio_inputs_path(project_root: Path) -> Path:
    return (project_root / "settings" / GPIO_INPUTS_FILENAME).resolve()


def ensure_gpio_inputs_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = {
        "poll_interval_sec": 0.05,
        "pins": [
            {"bcm_pin": 27, "name": "GPIO_27", "trigger_level": 0, "hold_sec": 0.5, "pull": "up"},
        ],
    }
    path.write_text(json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_gpio_inputs_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        cfg = json.loads(text)
        validated = GpioInputsModel.model_validate(cfg)
        cfg_valid = validated.model_dump(mode="python")
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc}"
    except ValidationError as exc:
        return None, f"Schema validation error: {exc.errors()[0].get('msg', 'invalid settings')}"
    return cfg_valid, None

