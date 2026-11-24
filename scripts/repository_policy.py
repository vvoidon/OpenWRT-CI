#!/usr/bin/env python3
"""Publish only APKs that cannot safely be supplied by the enabled official feeds."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def command(arguments):
    result = subprocess.run([str(value) for value in arguments], text=True, capture_output=True)
    if result.returncode:
        raise ValueError("APK command failed: " + (result.stderr or result.stdout).strip())
    return result.stdout


def read_index(apk, path):
    data = json.loads(command([apk, "adbdump", "--format", "json", path]))
    packages = {}
    for package in data["packages"]:
        name = package["name"]
        if name in packages and packages[name] != package:
            raise ValueError(f"Ambiguous package in {path}: {name}")
        packages[name] = package
    if not packages:
        raise ValueError(f"Empty repository index: {path}")
    return packages


def official_urls(text):
    urls = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("https://downloads.immortalwrt.org/"):
            if not re.fullmatch(
                    r"https://downloads\.immortalwrt\.org/snapshots/packages/"
                    r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+/packages\.adb", line):
                raise ValueError(f"Unsupported official repository: {line}")
            urls.append(line)
    if not urls or len(set(urls)) != len(urls):
        raise ValueError("Missing or duplicate official repositories in firmware")
    return urls


def fetch_index(url, path):
    # A failed/missing feed is not evidence that its packages are unavailable.
    # Fail the build instead of silently publishing duplicate packages.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=45) as response:
                path.write_bytes(response.read())
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(5)


def dependency_name(dependency):
    return re.split(r"[<>=~]", dependency.lstrip("!"), maxsplit=1)[0]


def keep_reason(local, official, exceptions, arch):
    name = local["name"]
    if name == "kernel" or name.startswith("kmod-"):
        return "Kernel/driver packages must come from the matching firmware build."
    if official.get("arch") not in {arch, "all", "noarch"}:
        return "Official package has an incompatible architecture."
    if name in exceptions and local.get("hashes") != official.get("hashes"):
        return exceptions[name]
    capabilities = {dependency_name(item) for item in local.get("provides", [])}
    available = {dependency_name(item) for item in official.get("provides", [])}
    if missing := capabilities - available:
        return "Official package lacks capabilities: " + ", ".join(sorted(missing))
    return None


def installed_records(text):
    records = {}
    for record in text.split("\n\n"):
        fields = dict(line.split(":", 1) for line in record.splitlines() if ":" in line)
        if "P" in fields and "V" in fields:
            records[fields["P"]] = (fields["V"], record)
    return records


def check_solution(result, required):
    # apk 3.0 query can print an ERROR and [] yet return status zero.
    if result.returncode or "ERROR:" in result.stderr:
        return (result.stderr or result.stdout).strip()[:1800] or "APK solver failed"
    try:
        chosen = {package["name"]: package["version"] for package in json.loads(result.stdout)}
    except (ValueError, KeyError, TypeError):
        return "APK solver did not return a package list"
    missing = [f"{name}={version}" for name, version in required.items()
               if chosen.get(name) != version]
    if missing:
        return "Solver changed or omitted required packages: " + ", ".join(missing)[:1400]
    return None


def filter_index(apk, index, output, packages, keys, signing_key):
    if not packages:
        raise ValueError(f"Policy would empty the hosted repository: {index}")
    # APK's index lookup requires a name/version separator; a bare ${name}
    # silently produces an empty index in apk-tools 3.0.5.
    filenames = [f"{name}-{packages[name]['version']}.apk" for name in sorted(packages)]
    command([apk, "mkndx", "--index", index, "--filter-spec", "${name}-${version}.apk",
             "--keys-dir", keys, "--sign-key", signing_key, "--output", output, *filenames])
    command([apk, "verify", "--keys-dir", keys, output])
    if read_index(apk, output) != packages:
        raise ValueError(f"Filtered index lost or changed packages: {index}")


class Solver:
    """Read-only APK resolution in a disposable copy of the firmware database."""

    def __init__(self, apk, firmware, work, arch, repositories, local):
        self.apk, self.work, self.arch = apk, work, arch
        self.repositories, self.local = repositories, local
        shutil.copytree(firmware / "etc/apk", work / "etc/apk")
        shutil.copytree(firmware / "lib/apk", work / "lib/apk")
        self.database = work / "lib/apk/db/installed"
        self.world = work / "etc/apk/world"
        self.original_database = self.database.read_text()
        self.original_world = self.world.read_text()
        self.installed = installed_records(self.original_database)

    def probe(self, package, fresh=False):
        name, version = package["name"], package["version"]
        required = {name: version}
        constraints = [f"{name}={version}"]
        if fresh:
            self.database.write_text("")
            self.world.write_text("")
        else:
            self.database.write_text("\n\n".join(
                record for other, (_, record) in self.installed.items() if other != name) + "\n\n")
            self.world.write_text(self.original_world)
            # Reusing an official package must not downgrade/remove any other
            # installed component (e.g. sing-box must not downgrade HomeProxy).
            for other, (installed_version, _) in self.installed.items():
                if other != name:
                    required[other] = installed_version
                    constraints.append(f"{other}={installed_version}")
            # Also honor versioned consumers that are built for the repository,
            # but are not preinstalled in the image.
            aliases = {name} | {dependency_name(item)
                                for item in self.local[name].get("provides", [])}
            for consumer in self.local.values():
                for dependency in consumer.get("depends", []):
                    if not dependency.startswith("!") and dependency_name(dependency) in aliases:
                        constraints.append(dependency)
        base = [self.apk, "--root", self.work, "--arch", self.arch,
                "--keys-dir", self.work / "etc/apk/keys",
                "--repositories-file", "/dev/null", "--no-cache", "--no-network"]
        for index in self.repositories:
            base += ["--repository", index]
        try:
            result = subprocess.run(
                [str(value) for value in base] +
                ["query", "--recursive", "--world", "--fields", "name,version",
                 "--format", "json", *sorted(set(constraints))],
                text=True, capture_output=True, timeout=60)
            return check_solution(result, required)
        finally:
            self.database.write_text(self.original_database)
            self.world.write_text(self.original_world)


def prune(root, pages, firmware, exceptions):
    apk = root / "staging_dir/host/bin/apk"
    keys = firmware / "etc/apk/keys"
    indices = sorted(pages.rglob("packages.adb"))
    local, sources = {}, {}
    for index in indices:
        sources[index] = read_index(apk, index)
        for name, package in sources[index].items():
            if name in local:
                raise ValueError(f"Duplicate hosted package: {name}")
            local[name] = package
    arch = local["kernel"]["arch"]
    audit = pages.with_name(pages.name + ".audit")
    audit.mkdir()  # Refuse to mix snapshots from different attempts.
    official_dir = audit / "official"
    official_dir.mkdir()
    official, official_indices, snapshots = {}, [], []
    urls = official_urls((firmware / "etc/apk/repositories.d/distfeeds.list").read_text())
    for number, url in enumerate(urls):
        path = official_dir / f"{number}.adb"
        fetch_index(url, path)
        command([apk, "verify", "--keys-dir", keys, path])
        packages = read_index(apk, path)
        for name, package in packages.items():
            if name in official and official[name] != package:
                raise ValueError(f"Ambiguous official package: {name}")
            official[name] = package
        official_indices.append(path)
        snapshots.append({"url": url, "index": path.name,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

    overlap = local.keys() & official.keys()
    retained, removed = {}, set()
    for name in sorted(overlap):
        reason = keep_reason(local[name], official[name], exceptions, arch)
        if reason:
            retained[name] = reason
        else:
            removed.add(name)
    print(f"Comparing {len(overlap)} same-name APKs against verified official indices", flush=True)

    with tempfile.TemporaryDirectory(prefix="re-cs-08-repository-policy-") as temporary:
        work = Path(temporary)
        filtered = [work / f"hosted-{number}.adb" for number in range(len(indices))]

        def rebuild():
            for index, output in zip(indices, filtered):
                packages = {name: package for name, package in sources[index].items()
                            if name not in removed}
                filter_index(apk, index, output, packages, keys, root / "private-key.pem")

        rebuild()
        solver = Solver(apk, firmware, work / "firmware", arch,
                        filtered + official_indices, local)
        for name in sorted(removed.copy()):
            if reason := solver.probe(official[name]):
                removed.remove(name)
                retained[name] = "Installed-firmware compatibility: " + reason
        rebuild()
        print(f"Installed-firmware checks passed for {len(removed)} official replacements", flush=True)
        # Check a clean database too: already-installed dependencies must not
        # disguise a missing package in the reduced repository set.
        while True:
            failed = {}
            for name in sorted(removed):
                if reason := solver.probe(official[name], fresh=True):
                    failed[name] = "Fresh-install dependency resolution: " + reason
            if not failed:
                break
            removed.difference_update(failed)
            retained.update(failed)
            rebuild()

        # Retry conservative rejects after dependencies have been restored.
        # A candidate is removed only if the complete set still passes both checks.
        for name in sorted(retained.copy()):
            if keep_reason(local[name], official[name], exceptions, arch):
                continue
            removed.add(name)
            rebuild()
            reason = solver.probe(official[name]) or solver.probe(official[name], fresh=True)
            if not reason:
                for other in sorted(removed - {name}):
                    if reason := solver.probe(official[other], fresh=True):
                        break
            if reason:
                removed.remove(name)
                retained[name] = "Final-set compatibility: " + reason
                rebuild()
            else:
                del retained[name]

        # Recheck the final set, not just intermediate filter decisions.
        for name in sorted(removed):
            reason = solver.probe(official[name]) or solver.probe(official[name], fresh=True)
            if reason:
                raise ValueError(f"Final official reuse check failed for {name}: {reason}")

        # Only remove verified copies inside this fresh Pages output. Never
        # modify bin/packages, the firmware, or the original build indices.
        for index, output in zip(indices, filtered):
            for name in sources[index].keys() & removed:
                package = sources[index][name]
                path = index.parent / f"{name}-{package['version']}.apk"
                if path.parent.resolve() != index.parent.resolve() or not path.is_file():
                    raise ValueError(f"Unexpected APK path: {path}")
                path.unlink()
            shutil.copyfile(output, index)

    report = {
        "schema": 1, "policy": "official-first-with-compatibility-checks",
        "official_indices": snapshots,
        "built_packages": len(local), "hosted_packages": len(local) - len(removed),
        "official_reused": {name: {"built_version": local[name]["version"],
                                  "official_version": official[name]["version"]}
                            for name in sorted(removed)},
        "retained_overlaps": {name: {"reason": retained[name],
                                    "built_version": local[name]["version"],
                                    "official_version": official[name]["version"]}
                              for name in sorted(retained)},
        "installed_compatibility_test": "passed", "fresh_install_test": "passed",
    }
    (pages / "repository-policy.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Official reuse: {len(removed)} APKs omitted; "
          f"{len(retained)} overlaps retained for compatibility")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("pages", type=Path)
    parser.add_argument("firmware", type=Path)
    args = parser.parse_args()
    prune(args.root.resolve(), args.pages.resolve(), args.firmware.resolve(),
          json.loads((PROJECT / "config/repository-exceptions.json").read_text()))
