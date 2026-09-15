from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_vnext_repository_trees_exist() -> None:
    expected = {
        "platform",
        "services",
        "workers",
        "ui",
        "presets",
        "deploy",
        "legacy",
    }

    assert expected <= {path.name for path in ROOT.iterdir() if path.is_dir()}


def test_root_packages_are_compatibility_adapters_only() -> None:
    for package in ("src", "blackbox", "modbus_acquire"):
        python_files = sorted(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / package).rglob("*.py")
        )
        assert python_files == [f"{package}/__init__.py"]


def test_legacy_freeze_and_deployment_entrypoints_exist() -> None:
    expected_files = (
        "legacy/README.md",
        "legacy/src/web_app.py",
        "legacy/scripts/linux/run_blackbox.sh",
        "deploy/compose.yaml",
        "bbctl",
        "bbctl.ps1",
    )

    for relative_path in expected_files:
        assert (ROOT / relative_path).is_file(), relative_path

