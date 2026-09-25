from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bb_platform.contracts import MapDocument, VmProtocol

DEFAULT_GPIO_VERSION = "gpio-default-v1"
DEFAULT_GPIO_PINS = [
    {"bcm_pin": 27, "name": "GPIO_27", "trigger_level": 0, "hold_sec": 0.5, "pull": "up", "invert": False},
]


class GpioPin(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bcm_pin: int = Field(ge=0, le=27)
    name: str = Field(min_length=1, max_length=255)
    trigger_level: int
    hold_sec: float = Field(ge=0.0, le=60.0)
    pull: str = "up"
    invert: bool = False

    @field_validator("trigger_level")
    @classmethod
    def _level(cls, value: int) -> int:
        if int(value) not in {0, 1}:
            raise ValueError("trigger_level must be 0 or 1")
        return int(value)

    @field_validator("pull")
    @classmethod
    def _pull(cls, value: str) -> str:
        text = str(value or "none").strip().lower()
        if text not in {"up", "down", "none"}:
            raise ValueError("pull must be up, down or none")
        return text

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("name is required")
        return text


@dataclass(frozen=True)
class PinSpec:
    bcm_pin: int
    name: str
    trigger_level: int
    hold_sec: float
    pull: str
    invert: bool
    address: int


def normalize_pins(pins: list[dict]) -> list[dict]:
    if not pins:
        raise ValueError("Нужен хотя бы один GPIO-пин")
    parsed = [GpioPin.model_validate(item).model_dump() for item in pins]
    seen: set[int] = set()
    names: set[str] = set()
    for pin in parsed:
        if pin["bcm_pin"] in seen:
            raise ValueError(f"Повтор BCM {pin['bcm_pin']}")
        if pin["name"] in names:
            raise ValueError(f"Повтор имени {pin['name']}")
        seen.add(pin["bcm_pin"])
        names.add(pin["name"])
    return parsed


def pins_from_fields(fields: list[dict]) -> list[PinSpec]:
    specs: list[PinSpec] = []
    for index, field in enumerate(fields or []):
        if not isinstance(field, dict) or "bcm_pin" not in field:
            continue
        pin = GpioPin.model_validate(field)
        specs.append(
            PinSpec(
                bcm_pin=pin.bcm_pin,
                name=pin.name,
                trigger_level=pin.trigger_level,
                hold_sec=pin.hold_sec,
                pull=pin.pull,
                invert=pin.invert,
                address=int(field.get("address", index)),
            )
        )
    specs.sort(key=lambda item: item.address)
    return specs


def build_gpio_map(pins: list[dict], version: str = DEFAULT_GPIO_VERSION) -> dict:
    normalized = normalize_pins(pins)
    fields = [
        {
            "name": pin["name"],
            "type": "bool",
            "kind": "discrete",
            "source": "pins",
            "address": index,
            "bcm_pin": pin["bcm_pin"],
            "trigger_level": pin["trigger_level"],
            "hold_sec": pin["hold_sec"],
            "pull": pin["pull"],
            "invert": pin["invert"],
        }
        for index, pin in enumerate(normalized)
    ]
    requests = [{"name": "pins", "address": 0, "count": len(fields)}]
    canonical = {
        "protocol": VmProtocol.GPIO.value,
        "preset_id": "gpio-raspberry",
        "version": version,
        "requests": requests,
        "fields": fields,
    }
    checksum = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    document = MapDocument(
        version=version,
        protocol=VmProtocol.GPIO,
        preset_id="gpio-raspberry",
        checksum=checksum,
        requests=requests,
        fields=fields,
    )
    return document.model_dump(mode="json")
