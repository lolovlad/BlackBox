from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent


def test_worker_image_pins_match_lock() -> None:
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    requirements = (ROOT / "deploy" / "worker-requirements.txt").read_text(encoding="utf-8")
    pinned: dict[str, str] = {}
    for line in requirements.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, version = line.split("==")
        pinned[name] = version

    assert set(pinned) == {"pydantic", "minimalmodbus", "pyserial"}

    for name, version in pinned.items():
        match = re.search(
            rf'\[\[package\]\]\r?\nname = "{re.escape(name)}"\r?\nversion = "([^"]+)"',
            lock,
        )
        assert match is not None, name
        assert match.group(1) == version
