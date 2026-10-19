#!/usr/bin/env python3
"""Persist a disposable build baseline and rebuild its affected userspace closure."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from source_inputs import (change_mode, metadata, selections, source_directories, state_rows)


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(root, output, state, coverage, chunk_size=512 * 1024 * 1024):
    if output.resolve().is_relative_to(root.resolve()) or chunk_size <= 0:
        raise ValueError("Baseline output must be outside the build tree with positive part size")
    output.mkdir(parents=True, exist_ok=False)
    # This is a release asset, not a Pages payload or an evictable Actions cache.
    command = ["tar", "-I", "zstd -T0 -3", "-cf", "-", "--exclude=./private-key.pem",
               "--exclude=./dl", "--exclude=./.ccache", "--exclude=./pages*",
               "--exclude=./release", "-C", str(root), "."]
    parts = []
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
        try:
            number = 0
            while True:
                data = process.stdout.read(min(chunk_size, 1024 * 1024))
                if not data:
                    break
                path = output / f"build-baseline-{number:04}.part"
                with path.open("wb") as stream:
                    stream.write(data)
                    remaining = chunk_size - len(data)
                    while remaining:
                        block = process.stdout.read(min(remaining, 1024 * 1024))
                        if not block:
                            break
                        stream.write(block)
                        remaining -= len(block)
                parts.append({"name": path.name, "sha256": digest(path)})
                number += 1
            if process.wait():
                raise ValueError("Build baseline archive failed")
        except BaseException:
            process.terminate()
            raise
    manifest = {"version": 1, "root": str(root.resolve()), "parts": parts,
                "state": state_rows(state), "coverage": json.loads(coverage.read_text())}
    (output / "build-baseline.json").write_text(json.dumps(manifest, indent=2) + "\n")


def restore(downloads, root):
    manifest = json.loads((downloads / "build-baseline.json").read_text())
    if manifest.get("version") != 1 or manifest["root"] != str(root.resolve()):
        raise ValueError("Baseline schema/path mismatch; a full build is required")
    if not manifest.get("parts"):
        raise ValueError("Empty baseline")
    if len({part["name"] for part in manifest["parts"]}) != len(manifest["parts"]):
        raise ValueError("Duplicate baseline parts")
    for part in manifest["parts"]:
        if not re.fullmatch(r"build-baseline-[0-9]{4}\.part", part["name"]):
            raise ValueError("Invalid baseline part name")
        if digest(downloads / part["name"]) != part["sha256"]:
            raise ValueError("Build baseline checksum mismatch")
    if root.exists() and any(root.iterdir()):
        raise ValueError("Baseline restore requires an empty disposable directory")
    root.mkdir(parents=True, exist_ok=True)
    with subprocess.Popen(["tar", "--no-same-owner", "-I", "zstd", "-xf", "-", "-C", str(root)],
                          stdin=subprocess.PIPE) as process:
        for part in manifest["parts"]:
            with (downloads / part["name"]).open("rb") as source:
                shutil.copyfileobj(source, process.stdin)
        process.stdin.close()
        if process.wait():
            raise ValueError("Build baseline extraction failed")
    return manifest


def source_for_path(root, directories, repo, path, records):
    checkout = directories[repo].resolve()
    matches = [source for source in records
               if (checkout / path).is_relative_to((root / source).resolve().parent)]
    if not matches:
        raise ValueError(f"Cannot map changed path to a package: {repo}:{path}")
    return max(matches, key=lambda source: len(str((root / source).resolve())))


def affected_closure(old_records, old_packages, new_records, new_packages, changed, selected):
    """Union old/new dependency graphs so removed and newly added edges both count."""
    reverse = {}
    for records, packages in ((old_records, old_packages), (new_records, new_packages)):
        aliases = {}
        for source, record in records.items():
            for name in [Path(source).parent.name, *record["packages"]]:
                aliases.setdefault(name, set()).add(source)
            for name in record["packages"]:
                for alias in packages[name].get("Provides", "").split():
                    aliases.setdefault(re.split(r"[<>=]", alias)[0], set()).add(source)
        for source, record in records.items():
            dependencies = [(dependency, True) for dependency in record["build"]]
            for name in record["packages"]:
                dependencies.extend((dependency, False) for dependency in packages[name].get("Depends", "").split())
            for dependency, build_dependency in dependencies:
                name = re.split(r"[<>=()]", dependency.rsplit(":", 1)[-1].lstrip("+@").split("/")[0])[0]
                for provider in aliases.get(name, ()):
                    # Rebuilding a script that invokes an updated CLI cannot fix
                    # its runtime semantics. Only link/ABI or explicit build edges
                    # require reverse compilation; APK resolves runtime versions.
                    library = any(package.startswith("lib") or packages[package].get("ABI-Version") or
                                  packages[package].get("Category") == "Libraries"
                                  for package in records[provider]["packages"])
                    if not build_dependency and not library:
                        continue
                    reverse.setdefault(provider, set()).add(source)
    active = {info["source"] for name, info in new_packages.items() if name in selected}
    affected = set(changed)
    pending = list(changed)
    while pending:
        for consumer in reverse.get(pending.pop(), set()) & active:
            if consumer not in affected:
                affected.add(consumer)
                pending.append(consumer)
    if not affected <= new_records.keys():
        raise ValueError("An affected source package was deleted/renamed")
    for source in affected:
        names = new_records[source]["packages"]
        if not names or any(new_packages[name].get("Type") != "ipkg" or name.startswith("kmod-") or name in {"kernel", "libc", "libgcc", "libstdcpp", "base-files", "busybox", "procd", "ubox", "ubus"}
                            for name in names):
            raise ValueError(f"Incremental closure reaches a core/kernel/host package: {source}")
    # Topologically order providers before consumers, even if targeted make does
    # not rebuild a newly enabled runtime dependency on its own.
    ordered = []
    pending = set(affected)
    while pending:
        ready = sorted(source for source in pending if not any(
            source in reverse.get(provider, ()) for provider in pending if provider != source))
        if not ready:
            raise ValueError("Cyclic affected build dependencies require a full build")
        ordered.extend(ready)
        pending.difference_update(ready)
    return ordered


def refresh(root, state, manifest, report):
    old = {tuple(row[:3]): row[3] for row in manifest["state"]}
    desired = state_rows(state)
    directories = source_directories(root, desired)
    records, packages = metadata(root / "tmp/.packageinfo")
    changes = set()
    moved = root.parent / "incremental-previous-sources"
    moved.mkdir(exist_ok=False)
    for repo, ref, kind, sha in desired:
        previous = old.get((repo, ref, kind))
        if previous == sha:
            continue
        if kind not in {"firmware", "feed", "package"} or previous is None:
            raise ValueError(f"Not an incremental userspace input: {repo}")
        checkout = directories[repo]
        run("git", "-C", checkout, "fetch", "--no-tags", "--depth=1", f"https://github.com/{repo}.git", sha)
        paths = subprocess.check_output(["git", "-C", str(checkout), "diff", "--no-renames", "--name-only", "-z",
                                         previous, "FETCH_HEAD"]).decode().strip("\0").split("\0")
        paths = [path for path in paths if path]
        run("git", "-C", checkout, "tag", "-f", "old-source", previous)
        run("git", "-C", checkout, "tag", "-f", "new-source", "FETCH_HEAD")
        mode = change_mode(repo, kind, paths, manifest["coverage"], checkout)
        if mode == "firmware":
            raise ValueError(f"Unsafe cumulative source changes: {repo}")
        for path in paths:
            if change_mode(repo, kind, [path], manifest["coverage"], checkout) != "none":
                changes.add(source_for_path(root, directories, repo, path, records))
        if kind == "firmware":
            # Normal checkout preserves non-overlapping board customizations and
            # refuses conflicts; never reset a patched tree with --hard.
            run("git", "-C", checkout, "checkout", "--detach", sha)
            continue
        shutil.move(str(checkout), moved / repo.replace("/", "--"))
        checkout.mkdir(parents=True)
        run("git", "-C", checkout, "init", "-q")
        run("git", "-C", checkout, "fetch", "--no-tags", "--depth=1", f"https://github.com/{repo}.git", sha)
        run("git", "-C", checkout, "checkout", "--detach", "FETCH_HEAD")
    report.write_text(json.dumps({"changed": sorted(changes), "records": records, "packages": packages,
                                  "config": (root / ".config").read_text()}, indent=2))


def compile_affected(root, report):
    previous = json.loads(report.read_text())
    records, packages = metadata(root / "tmp/.packageinfo")
    def protected_config(text):
        return {line for line in text.splitlines() if line.startswith(("CONFIG_", "# CONFIG_")) and
                ("CONFIG_PACKAGE_" not in line or "CONFIG_PACKAGE_kmod-" in line)}
    if protected_config(previous["config"]) != protected_config((root / ".config").read_text()):
        raise ValueError("Non-package configuration changed")
    before = selections_from_text(previous["config"])
    current = selections(root / ".config")
    if {key: value for key, value in before.items() if key.startswith("kmod-")} != {
            key: value for key, value in current.items() if key.startswith("kmod-")}:
        raise ValueError("Kernel module selections changed")
    added = {packages[name]["source"] for name in current.keys() - before.keys() if name in packages}
    affected = affected_closure(previous["records"], previous["packages"], records, packages,
                                set(previous["changed"]) | added, set(current))
    for source in affected:
        if {item for item in records[source]["build"] if "/host" in item} != {
                item for item in previous["records"].get(source, {}).get("build", []) if "/host" in item}:
            raise ValueError("Changed host-tool requirements require a full build")
    # Do not delete old binaries in place: preserve them outside bin while rebuilding.
    old_names = {name for source in affected for name in previous["records"].get(source, {}).get("packages", [])}
    abi_names = {name + ("-" if name[-1:].isdigit() and info.get("ABI-Version") else "") + info.get("ABI-Version", "")
                 for name, info in previous["packages"].items() if name in old_names}
    apk = root / "staging_dir/host/bin/apk"
    def immutable_packages():
        return {str(path.relative_to(root)): digest(path) for path in (root / "bin").rglob("*.apk")
                if path.name.startswith(("kernel-", "kmod-"))}
    immutable = immutable_packages()
    archive = root.parent / "incremental-old-apks"
    binaries = [(path, json.loads(subprocess.check_output(
        [str(apk), "adbdump", "--format", "json", str(path)]))["info"])
        for path in (root / "bin").rglob("*.apk")]
    for path, info in binaries:
        if info["name"] in abi_names:
            target = archive / path.relative_to(root / "bin")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), target)
    for source in affected:
        target = str(Path(source).parent)
        if not re.fullmatch(r"package/[A-Za-z0-9_./+-]+", target) or ".." in Path(target).parts:
            raise ValueError("Unsafe package target")
        run("make", f"{target}/clean", cwd=root)
    for source in affected:
        run("make", f"{Path(source).parent}/compile", f"-j{os.cpu_count() or 2}", cwd=root)
    if immutable_packages() != immutable:
        raise ValueError("Incremental build changed kernel or a kmod")
    built_names = {json.loads(subprocess.check_output(
        [str(apk), "adbdump", "--format", "json", str(path)]))["info"]["name"]
        for path in (root / "bin").rglob("*.apk")}
    expected = {name + ("-" if name[-1:].isdigit() and info.get("ABI-Version") else "") + info.get("ABI-Version", "")
                for name, info in packages.items() if info["source"] in affected and name in current
                and info.get("Type") == "ipkg"}
    if not expected <= built_names:
        raise ValueError("Affected packages were not rebuilt: " + ", ".join(sorted(expected - built_names)))
    report.write_text(json.dumps({"affected_sources": affected, "kernel_apks_preserved": len(immutable)}, indent=2))


def selections_from_text(text):
    return dict(re.findall(r"^CONFIG_PACKAGE_(.+)=([ym])$", text, re.M))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("save", "restore", "refresh", "compile"))
    parser.add_argument("root", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--coverage", type=Path)
    args = parser.parse_args()
    if args.command == "save":
        save(args.root, args.directory, args.state, args.coverage)
    elif args.command == "restore":
        restore(args.directory, args.root)
    elif args.command == "refresh":
        refresh(args.root, args.state, json.loads((args.directory / "build-baseline.json").read_text()),
                args.directory / "incremental-plan.json")
    else:
        compile_affected(args.root, args.directory / "incremental-plan.json")
