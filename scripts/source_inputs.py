#!/usr/bin/env python3
"""Source locks, build dependencies and conservative change filtering."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
FIRMWARE = "VIKINGYFY/immortalwrt"


def state_rows(path):
    rows = [line.split("|") for line in Path(path).read_text().splitlines() if line]
    if any(len(row) != 4 or not re.fullmatch("[0-9a-f]{40}", row[3]) for row in rows):
        raise ValueError("Invalid source-state.txt")
    if len({tuple(row[:3]) for row in rows}) != len(rows):
        raise ValueError("Duplicate source lock")
    return rows


def feeds():
    return [line.split("|") for line in (PROJECT / "config/feeds.list").read_text().splitlines()
            if line and not line.startswith("#")]


def external_sources():
    return re.findall(r'^UPDATE_PACKAGE "[^"]+" "([^"]+)" "([^"]+)"',
                      (PROJECT / "scripts/prepare-packages.sh").read_text(), re.M)


def source_directories(root, rows):
    feed_names = {repo: name for name, repo, _ in feeds()}
    result = {}
    for repo, _, kind, _ in rows:
        if kind == "firmware":
            result[repo] = root
        elif kind == "feed":
            result[repo] = root / "feeds" / feed_names[repo]
        elif kind == "package":
            result[repo] = root / "package" / repo.split("/")[-1]
    return result


def metadata(path):
    """Keep source-level host dependencies alongside each binary package."""
    source = None
    package = None
    records = {}
    packages = {}
    for line in Path(path).read_text().splitlines():
        key, separator, value = line.partition(": ")
        if key == "Source-Makefile" and separator:
            source = value
            records.setdefault(source, {"packages": [], "build": []})
            package = None
        elif source and key.startswith("Build-Depends") and separator:
            records[source]["build"].extend(value.split())
        elif source and key == "Package" and separator:
            package = value
            records[source]["packages"].append(package)
            packages[package] = {"source": source}
        elif package and separator:
            packages[package][key] = value
    return records, packages


def selections(path):
    return dict(re.findall(r"^CONFIG_PACKAGE_(.+)=([ym])$", Path(path).read_text(), re.M))


def footprint(root, state):
    rows = state_rows(state)
    directories = source_directories(root, rows)
    roots = sorted(directories.items(), key=lambda item: len(str(item[1])), reverse=True)
    records, packages = metadata(root / "tmp/.packageinfo")
    selected = set(selections(root / ".config"))
    active = {packages[name]["source"] for name in selected if name in packages}
    by_name = {}
    for source, record in records.items():
        for name in [Path(source).parent.name, *record["packages"]]:
            by_name.setdefault(name, set()).add(source)
    pending = list(active)
    while pending:
        record = records[pending.pop()]
        deps = list(record["build"])
        for name in record["packages"]:
            deps.extend(packages[name].get("Depends", "").split())
        for dependency in deps:
            name = dependency.rsplit(":", 1)[-1].lstrip("+@").split("/")[0]
            name = re.split(r"[<>=()]", name)[0]
            for source in by_name.get(name, ()):
                if source not in active:
                    active.add(source)
                    pending.append(source)
    result = {repo: {"revision": sha, "known": [], "used": []}
              for repo, _, kind, sha in rows if kind != "ci"}
    for source in records:
        directory = (root / source).resolve().parent
        for repo, checkout in roots:
            if directory.is_relative_to(checkout.resolve()):
                relative = directory.relative_to(checkout.resolve()).as_posix()
                result[repo]["known"].append(relative)
                if source in active:
                    result[repo]["used"].append(relative)
                break
        else:
            raise ValueError(f"Package outside locked sources: {source}")
    for record in result.values():
        for key in ("known", "used"):
            record[key] = sorted(set(record[key]))
    # Follow the board's DTS include closure, not similar device names.
    dts_directory = root / "target/linux/qualcommbe/dts"
    pending_dts = [dts_directory / "ipq5322-re-cs-08.dts"]
    used_dts = set()
    while pending_dts:
        path = pending_dts.pop()
        if path in used_dts or not path.is_file():
            continue
        used_dts.add(path)
        for included in re.findall(r'(?:#include|/include/)\s*"([^"]+)"', path.read_text()):
            candidate = path.parent / included
            if candidate.is_file():
                pending_dts.append(candidate)
    if not used_dts:
        raise ValueError("RE-CS-08 DTS missing")
    return {"version": 1, "sources": result,
            "dts": sorted(path.relative_to(root).as_posix() for path in used_dts)}


def relevant_path(repo, kind, path, coverage=None):
    parts = Path(path).parts
    if (parts[0] in {".github", "docs"} or Path(path).name in {"README", "LICENSE"}
            or Path(path).name.startswith(("README.", "LICENSE."))):
        return False
    if kind == "firmware" and path.startswith("target/linux/"):
        if not path.startswith(("target/linux/qualcommbe/", "target/linux/generic/")):
            return False
        if path.startswith("target/linux/qualcommbe/dts/") and coverage:
            return path in coverage["dts"]
        match = re.match(r"target/linux/qualcommbe/(ipq\d+xx)/", path)
        if match and match[1] != "ipq53xx":
            return False
        if re.fullmatch(r"target/linux/qualcommbe/image/ipq\d+xx\.mk", path):
            return path.endswith("/ipq53xx.mk")
        return True
    record = (coverage or {}).get("sources", {}).get(repo)
    if not record:
        return True

    def within(directory):
        return directory == "." or path == directory or path.startswith(directory + "/")

    if any(within(directory) for directory in record["used"]):
        return True
    # Unknown/new packages and shared files rebuild conservatively.
    return not any(within(directory) for directory in record["known"])


def verified_coverage(path, rows):
    if not path or not Path(path).is_file():
        return None
    data = json.loads(Path(path).read_text())
    expected = {repo: sha for repo, _, kind, sha in rows if kind != "ci"}
    if (data.get("version") != 1 or
            {repo: info["revision"] for repo, info in data["sources"].items()} != expected):
        return None
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sources", "pin-feeds", "verify", "footprint",
                                          "relevant", "cache-key"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--state")
    parser.add_argument("--output")
    parser.add_argument("--coverage")
    parser.add_argument("--repo")
    parser.add_argument("--kind")
    parser.add_argument("--paths")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "sources":
        print(f"{FIRMWARE}|owrt|firmware")
        for _, repo, ref in feeds():
            print(f"{repo}|{ref}|feed")
        for repo, ref in external_sources():
            print(f"{repo}|{ref}|package")
        return
    rows = state_rows(args.state)
    if args.command == "pin-feeds":
        locks = {(repo, ref, kind): sha for repo, ref, kind, sha in rows}
        lines = [f"src-git {name} https://github.com/{repo}.git^{locks[repo, ref, 'feed']}"
                 for name, repo, ref in feeds()]
        (root / "feeds.conf").write_text("\n".join(lines) + "\n")
    elif args.command == "verify":
        directories = source_directories(root, rows)
        for repo, _, kind, sha in rows:
            if kind == "ci":
                continue
            actual = subprocess.check_output(["git", "-C", str(directories[repo]),
                                              "rev-parse", "HEAD"], text=True).strip()
            if actual != sha:
                raise ValueError(f"Source lock mismatch: {repo}: {actual} != {sha}")
        print(f"Verified {len(directories)} source revisions")
    elif args.command == "footprint":
        Path(args.output).write_text(json.dumps(footprint(root, args.state), indent=2) + "\n")
    elif args.command == "relevant":
        coverage = verified_coverage(args.coverage, rows)
        paths = Path(args.paths).read_bytes().decode().split("\0")
        for path in filter(None, paths):
            if relevant_path(args.repo, args.kind, path, coverage):
                print(f"Relevant change: {args.repo}: {path}")
                return
        raise SystemExit(10)  # Distinct from malformed metadata or other errors.
    elif args.command == "cache-key":
        # hostpkg also contains tools from core packages, feeds and external
        # packages. The toolchain includes target-specific Linux headers.
        digest = hashlib.sha256()
        for path in ("tools", "toolchain", "include", "config", "package",
                     "target/linux/generic", "target/linux/qualcommbe",
                     "Makefile", "rules.mk", "Config.in"):
            digest.update(subprocess.check_output(["git", "-C", str(root), "ls-tree",
                                                   "-r", "HEAD", path]))
        for path in ("config/packages.config", "config/re-cs-08.config", "config/re-cs-08-kernel.config",
                     "scripts/patch-packages.sh", "scripts/configure-firmware.sh",
                     "scripts/prepare-packages.sh", "scripts/common.sh"):
            source = PROJECT / path
            digest.update(path.encode())
            digest.update(source.read_bytes() if source.is_file() else b"absent")
        for row in rows:
            if row[2] in {"feed", "package"}:
                digest.update("|".join(row).encode())
        print(digest.hexdigest())


if __name__ == "__main__":
    main()
