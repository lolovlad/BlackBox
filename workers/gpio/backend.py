from __future__ import annotations

from workers.gpio.pins import PinSpec


class GpiodBackend:
    """libgpiod line requests. Imported only when a GPIO worker actually opens a chip."""

    def __init__(self, chip: str, pins: list[PinSpec]) -> None:
        import gpiod
        from gpiod.line import Bias, Direction

        config = {}
        for pin in pins:
            bias = {"up": Bias.PULL_UP, "down": Bias.PULL_DOWN}.get(pin.pull, Bias.DISABLED)
            config[int(pin.bcm_pin)] = gpiod.LineSettings(direction=Direction.INPUT, bias=bias)
        self._gpiod = gpiod
        self._request = gpiod.request_lines(str(chip), consumer="blackbox-gpio", config=config)

    def read_pin(self, bcm_pin: int) -> int:
        value = self._request.get_value(int(bcm_pin))
        active = getattr(getattr(self._gpiod, "line", None), "Value", None)
        if active is not None and value == active.ACTIVE:
            return 1
        if active is not None and value == active.INACTIVE:
            return 0
        try:
            return 1 if int(value) else 0
        except (TypeError, ValueError):
            return 1 if str(value).endswith("ACTIVE") else 0

    def cleanup(self) -> None:
        release = getattr(self._request, "release", None)
        if callable(release):
            release()


def build_gpio_backend(chip: str, pins: list[PinSpec]) -> GpiodBackend:
    return GpiodBackend(chip, pins)
