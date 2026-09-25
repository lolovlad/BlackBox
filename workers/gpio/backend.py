from __future__ import annotations

from workers.gpio.pins import PinSpec


class GpiodBackend:
    """libgpiod line requests. Imported only when a GPIO worker actually opens a chip."""

    def __init__(self, chip: str, pins: list[PinSpec]) -> None:
        import gpiod
        from gpiod.line import Bias, Direction

        self._gpiod = gpiod
        self._lines: dict[int, object] = {}
        for pin in pins:
            bias = {"up": Bias.PULL_UP, "down": Bias.PULL_DOWN}.get(pin.pull, Bias.DISABLED)
            config = {int(pin.bcm_pin): gpiod.LineSettings(direction=Direction.INPUT, bias=bias)}
            try:
                self._lines[int(pin.bcm_pin)] = gpiod.request_lines(str(chip), consumer="blackbox-gpio", config=config)
            except OSError:
                continue

    def read_pin(self, bcm_pin: int) -> int | None:
        request = self._lines.get(int(bcm_pin))
        if request is None:
            return None
        value = request.get_value(int(bcm_pin))
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
        for request in self._lines.values():
            release = getattr(request, "release", None)
            if callable(release):
                release()
        self._lines.clear()


def build_gpio_backend(chip: str, pins: list[PinSpec]) -> GpiodBackend:
    return GpiodBackend(chip, pins)
