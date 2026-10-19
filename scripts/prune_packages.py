#!/usr/bin/env python3
"""Prune unused hardware packages and wireless relay support, preserving dependencies."""

import argparse
import re
from pathlib import Path


WIRED_UNUSED_PACKAGES = {
    "ath12k-firmware-ipq5332", "ath12k-firmware-ipq5332-ddwrt",
    "ath12k-firmware-qcn9274", "ath12k-firmware-qcn9274-ddwrt",
    "relayd", "luci-proto-relay",
}


def excluded_packages(metadata):
    excluded = {
        *WIRED_UNUSED_PACKAGES,
        "audio-support", "video-support", "dvb-support",
        "kmod-usb-serial-dmx_usb_module",
        "kmod-input-adc-keys",
        "rtpengine", "rtpengine-no-transcode", "rtpengine-recording",
        "iptables-mod-rtpengine", "kmod-ipt-rtpengine",
    }
    submenus = {
        "Bluetooth Support", "CAN Support", "Industrial I/O Modules",
        "Sound Support", "Video Support", "DVB Support", "Media tuners",
        "Voice over IP", "Wireless Drivers",
    }
    package = {}
    for line in [*metadata.splitlines(), "Package: "]:
        if line.startswith("Package: "):
            name = package.get("Package", "")
            # Keep user-space software and LuCI dependencies, apart from the
            # unused wireless relay feature above. All exclusions still yield
            # to dependencies of retained packages below.
            if package.get("Category") == "Kernel modules" and (
                package.get("Submenu") in submenus
                or name.startswith(("kmod-nfc", "kmod-usb-net-", "kmod-usb-serial-",
                                    "kmod-usb-storage", "kmod-usb-hid", "kmod-usb-printer"))
            ):
                excluded.add(name)
            package = {}
        key, separator, value = line.partition(": ")
        if separator and key in {"Package", "Submenu", "Category"}:
            package[key] = value
    return excluded - {""}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    excluded = excluded_packages(Path("tmp/.packageinfo").read_text())
    config = Path(".config")
    lines = config.read_text().splitlines()
    selected = {
        match.group(1)
        for line in lines
        if (match := re.fullmatch(r"CONFIG_PACKAGE_(.+)=[ym]", line))
    }
    # Preserve dependencies of every retained software/kernel package, even
    # conditional selects. Retained software functionality takes precedence
    # over removing optional wireless firmware or relay support.
    graph = {}
    current = None
    for line in Path("tmp/.config-package.in").read_text().splitlines():
        if match := re.match(r"\s*(?:menu)?config PACKAGE_(\S+)", line):
            current = match.group(1)
            graph.setdefault(current, set())
        elif current and (match := re.match(r"\s*select PACKAGE_(\S+)", line)):
            graph[current].add(match.group(1))
    retained = selected - excluded
    pending = list(retained)
    while pending:
        for dependency in graph.get(pending.pop(), ()):
            if dependency not in retained:
                retained.add(dependency)
                pending.append(dependency)
    required_optional_packages = excluded & retained
    broken = {"rtpengine", "rtpengine-no-transcode", "rtpengine-recording",
              "iptables-mod-rtpengine", "kmod-ipt-rtpengine"}
    if required_optional_packages & broken:
        raise SystemExit("Retained packages require unsupported RTPengine")
    excluded -= required_optional_packages
    if required_optional_packages:
        print("Kept for retained-package dependencies: " + ", ".join(sorted(required_optional_packages)))
    if args.check:
        remaining = sorted(selected & excluded)
        if remaining:
            raise SystemExit("Excluded packages re-selected by dependencies: " + ", ".join(remaining))
        print(f"Pruned package check passed ({len(excluded)} exclusions)")
        return
    symbol = re.compile(r"(?:# )?CONFIG_(?:PACKAGE|MODULE_DEFAULT)_(.+?)(?:=[ymn]| is not set)")
    kept = [
        line for line in lines
        if not ((match := symbol.fullmatch(line)) and match.group(1) in excluded)
    ]
    kept.extend(f"# CONFIG_PACKAGE_{name} is not set" for name in sorted(excluded))
    # Per-device rootfs defaults otherwise force some excluded drivers to m.
    kept.extend(f"# CONFIG_MODULE_DEFAULT_{name} is not set" for name in sorted(excluded))
    config.write_text("\n".join(kept) + "\n")
    Path("pruned-packages.txt").write_text("\n".join(sorted(excluded)) + "\n")
    print(f"Pruned {len(selected & excluded)} selected packages ({len(excluded)} exclusions)")


if __name__ == "__main__":
    main()
