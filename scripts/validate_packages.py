#!/usr/bin/env python3
"""Check package intent before building, and exact installed packages afterwards."""
import argparse
import json
import re
from pathlib import Path

from source_inputs import PROJECT, metadata, selections
from prune_packages import WIRED_UNUSED_PACKAGES


STORAGE_PACKAGES = {
    "kmod-nvme", "kmod-ata-ahci", "kmod-fs-ext4", "kmod-fs-f2fs", "kmod-fs-btrfs",
    "automount", "blkid", "nvme-cli", "smartmontools",
    "luci-app-mini-diskmanager", "luci-app-partexp",
}
REPOSITORY_ONLY_PACKAGES = {"natmapt", "luci-app-natmapt", "luci-i18n-natmapt-zh-cn"}


def validate_storage(configured):
    missing = sorted(name for name in STORAGE_PACKAGES if configured.get(name) != "y")
    if missing:
        raise ValueError("SSD support must remain installed: " + ", ".join(missing))


def validate_config(root):
    configured = selections(root / ".config")
    validate_storage(configured)
    if any(configured.get(name) != "m" for name in REPOSITORY_ONLY_PACKAGES):
        raise ValueError("NATMapT must be built for APK installation, not preinstalled")
    excluded = set((root / "pruned-packages.txt").read_text().splitlines())
    requested = {}
    for name in ("re-cs-08", "packages"):
        requested.update(selections(PROJECT / "config" / f"{name}.config"))
    requested.update({"luci": "y", "luci-theme-aurora": "y", "luci-app-aurora-config": "y"})
    missing = sorted(name for name, value in requested.items()
                     if value == "y" and name not in excluded and configured.get(name) != "y")
    if missing:
        raise ValueError("Requested packages lost/demoted by defconfig: " + ", ".join(missing))
    _, packages = metadata(root / "tmp/.packageinfo")
    expected = {}
    for name, value in configured.items():
        if value != "y" or name not in packages or packages[name].get("Type") != "ipkg":
            continue
        # APK ABI names are formed like libubus20260925 / libopenssl3.
        abi = packages[name].get("ABI-Version", "")
        binary = name + ("-" if name[-1:].isdigit() and abi else "") + abi
        expected[name] = binary
    if not expected or "luci-app-axonhub" not in expected or "luci-app-homeproxy" not in expected:
        raise ValueError("Incomplete binary-package metadata")
    (root / "expected-packages.json").write_text(json.dumps(expected, indent=2) + "\n")
    print(f"Validated package configuration; {len(expected)} binaries expected in firmware")


def validate_installed(expected, installed):
    names = set(re.findall(r"^P:(.+)$", Path(installed).read_text(), re.M))
    expected_names = set(json.loads(Path(expected).read_text()).values())
    missing = sorted(expected_names - names)
    if missing:
        raise ValueError("Firmware is missing selected packages: " + ", ".join(missing))
    unexpected = sorted((names & WIRED_UNUSED_PACKAGES) - expected_names)
    if unexpected:
        raise ValueError("Unused wireless/relay packages leaked into firmware: " + ", ".join(unexpected))
    if names & REPOSITORY_ONLY_PACKAGES:
        raise ValueError("NATMapT must remain available via APK, not preinstalled")
    print(f"Firmware installed database verified: {len(names)} packages")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("config", "installed"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--expected")
    parser.add_argument("--installed")
    args = parser.parse_args()
    if args.command == "config":
        validate_config(args.root.resolve())
    else:
        validate_installed(args.expected, args.installed)
