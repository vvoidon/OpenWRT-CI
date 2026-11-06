#!/usr/bin/env python3
"""Summarize OpenWrt's native package-error log without changing build results."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from source_inputs import metadata, selections

FAILED_SOURCE = re.compile(r"ERROR:\s+(package/[A-Za-z0-9_./+-]+)(?:\s+\[[^\]]+\])?\s+failed to build")


def collect(root, output):
    configured = selections(root / ".config") if (root / ".config").is_file() else {}
    records, packages = metadata(root / "tmp/.packageinfo") if (root / "tmp/.packageinfo").is_file() else ({}, {})
    by_directory = {(root / name).resolve().parent: record for name, record in records.items()}
    log_root = root / "logs"
    errors = log_root / "package/error.txt"
    failed = {}
    for line in errors.read_text().splitlines() if errors.is_file() else []:
        if not (match := FAILED_SOURCE.search(line)):
            continue
        source = match[1]
        if ".." in Path(source).parts:
            raise ValueError("Invalid source path in package error log")
        failed.setdefault(source, set()).add(line.strip())
    sources = []
    apks = list((root / "bin").rglob("*.apk")) if failed else []
    names = {}

    def was_built(name):
        abi = packages[name].get("ABI-Version", "")
        binary = name + ("-" if name[-1:].isdigit() and abi else "") + abi
        for path in apks:
            if not path.name.startswith(binary + "-"):
                continue
            if path not in names:
                names[path] = json.loads(subprocess.check_output(
                    [str(root / "staging_dir/host/bin/apk"), "adbdump", "--format", "json", str(path)],
                    text=True))["info"]["name"]
            if names[path] == binary:
                return True
        return False

    for source, messages in sorted(failed.items()):
        record = records.get(source + "/Makefile", by_directory.get((root / source).resolve(), {}))
        selected = {name: configured[name] for name in record.get("packages", []) if name in configured}
        optional = bool(selected) and all(value == "m" for value in selected.values())
        missing = sorted(name for name in selected if not was_built(name))
        logs = []
        for path in sorted((log_root / source).rglob("*.txt")):
            relative = Path("package-errors") / path.relative_to(log_root)
            destination = output.parent / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                destination.write_bytes(stream.read())
            logs.append(relative.as_posix())
        sources.append({"source": source, "selected_packages": selected,
                        "repository_only": optional, "missing_packages": missing,
                        "recovered": bool(selected) and not missing,
                        "errors": sorted(messages), "log_tails": logs})
        if optional and missing:
            print(f"::warning::Skipped optional package source: {source}")
    report = {"policy": "IGNORE_ERRORS=m", "failed_sources": sources,
              "skipped_optional_sources": sum(item["repository_only"] and bool(item["missing_packages"]) for item in sources)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as stream:
            stream.write(f"\nOptional package sources skipped: **{report['skipped_optional_sources']}**\n")
            for item in sources:
                label = "recovered" if item["recovered"] else "optional" if item["repository_only"] else "required or unclassified"
                stream.write(f"- `{item['source']}` ({label})\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    collect(arguments.root.resolve(), arguments.output.resolve())
