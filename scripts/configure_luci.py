#!/usr/bin/env python3
"""Apply the same LuCI customizations to full and incremental builds."""
import argparse
import json
import os
from pathlib import Path
import re


def configure(root, settings):
    luci = root / "feeds/luci"
    for path in (luci / "collections").rglob("Makefile"):
        text = "".join(line for line in path.read_text().splitlines(keepends=True)
                       if "attendedsysupgrade" not in line)
        path.write_text(text.replace("luci-theme-bootstrap", "luci-theme-" + settings["theme"]))
    for path in (luci / "modules/luci-mod-system").rglob("flash.js"):
        path.write_text(re.sub(r"192\.168\.[0-9]+\.[0-9]+", lambda _: settings["ip"], path.read_text()))
    for path in (luci / "modules/luci-mod-status").rglob("10_system.js"):
        expression = "(luciversion || '') + (" + json.dumps(" / " + settings["mark"] + "-" + settings["date"]) + ")"
        path.write_text(re.sub(r"\(luciversion \|\| ''\)(?: \+ \((?:'[^'\n]*'|\"[^\"\n]*\")\))?",
                               lambda _: expression, path.read_text()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()
    state = args.root / "ci-luci-customizations.json"
    if args.reuse:
        settings = json.loads(state.read_text())
    else:
        settings = {name: os.environ["WRT_" + name.upper()] for name in ("theme", "ip", "mark", "date")}
    configure(args.root, settings)
    if not args.reuse:
        state.write_text(json.dumps(settings, indent=2) + "\n")


if __name__ == "__main__":
    main()
