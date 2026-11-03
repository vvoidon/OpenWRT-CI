#!/usr/bin/env python3
"""Verify the built image, repository compatibility and kernel size before upload."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from source_inputs import PROJECT
from validate_packages import validate_installed
from repository_policy import installed_records

REINSTALL_TEST_PACKAGES = {
    "axonhub", "luci-app-axonhub", "luci-i18n-axonhub-zh-cn",
    "luci-app-homeproxy", "luci-i18n-homeproxy-zh-cn", "sing-box",
    "luci-theme-aurora", "luci-app-aurora-config",
    "docker", "dockerd", "docker-compose", "luci-app-dockerman", "luci-i18n-dockerman-zh-cn",
}


def run(command):
    subprocess.run([str(item) for item in command], check=True)


def retry(command):
    for attempt in range(3):
        try:
            run(command)
            return
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(5)


def kernel_margin(size, limit=6 * 1024 * 1024):
    if size <= 0 or size > limit:
        raise ValueError(f"Kernel does not fit: {size} bytes, limit {limit}")
    margin = limit - size
    print(f"Kernel size: {size}; free: {margin} bytes ({margin / 1024:.1f} KiB)")
    if margin < 256 * 1024:
        print(f"::warning::RE-CS-08 kernel headroom is only {margin / 1024:.1f} KiB")
    return margin


def validate_kernel_config(path):
    configured = dict(re.findall(r"^CONFIG_(\w+)=(\w+)$", path.read_text(), re.M))
    for symbol in ("PCI", "PCIE_QCOM_COMMON", "PCIE_QCOM", "PHY_QCOM_UNIPHY_PCIE_28LP"):
        if configured.get(symbol) != "y":
            raise ValueError(f"RE-CS-08 PCIe support missing: CONFIG_{symbol}")
    for symbol in ("NVME_CORE", "BLK_DEV_NVME", "SATA_AHCI", "EXT4_FS", "BTRFS_FS", "F2FS_FS"):
        if configured.get(symbol) not in {"y", "m"}:
            raise ValueError(f"SSD driver/filesystem missing: CONFIG_{symbol}")
    fragment = (PROJECT / "config/re-cs-08-kernel.config").read_text()
    for symbol in re.findall(r"^# CONFIG_(\w+) is not set$", fragment, re.M):
        if configured.get(symbol) in {"y", "m"}:
            raise ValueError(f"Excluded board hardware was re-enabled: CONFIG_{symbol}")
    print("Kernel config verified: PCIe/UNIPHY, NVMe, AHCI and SSD filesystems retained")


def indexed_packages(apk, pages):
    packages = {}
    for index in pages.rglob("packages.adb"):
        data = json.loads(subprocess.check_output([str(apk), "adbdump", "--format", "json", str(index)]))
        for package in data["packages"]:
            name = package["name"]
            if name in packages and packages[name] != package:
                raise ValueError(f"Ambiguous package across hosted repositories: {name}")
            packages[name] = package
    return packages


def package_architecture(packages):
    # APK uses "noarch" for generic packages. The kernel is the authoritative
    # target, regardless of which repository/package sorts first.
    arch = packages.get("kernel", {}).get("arch", "")
    if arch in {"all", "noarch"} or not re.fullmatch(r"[a-zA-Z0-9_-]+", arch):
        raise ValueError("Missing target kernel architecture")
    mixed = sorted(name for name, package in packages.items()
                   if package.get("arch") not in {arch, "all", "noarch"})
    if mixed:
        raise ValueError("Mismatched package architecture: " + ", ".join(mixed))
    return arch


def baseline_constraints(database, kernel_version, candidates=None):
    installed = installed_records(database)
    if installed.get("kernel", (None,))[0] != kernel_version:
        raise ValueError("Released firmware kernel ABI differs from repository build")
    constraints = [f"{name}{'=' if name == 'kernel' or name.startswith('kmod-') else '>='}{version}"
                   for name, (version, _) in sorted(installed.items())]
    # Do not let the solver hide an incompatible update by keeping the old copy.
    constraints.extend(f"{name}={package['version']}" for name, package in (candidates or {}).items()
                       if name in installed and package["version"] != installed[name][0])
    return constraints


def validate(root, pages, report, baseline_image=None):
    apk = root / "staging_dir/host/bin/apk"
    configs = list(root.glob("build_dir/target-*/linux-qualcommbe_ipq53xx/linux-*/.config"))
    if len(configs) != 1:
        raise ValueError("Expected exactly one built RE-CS-08 kernel configuration")
    validate_kernel_config(configs[0])
    metadata = json.loads((pages / "repositories.json").read_text())
    policy_file = pages / "repository-policy.json"
    if hashlib.sha256(policy_file.read_bytes()).hexdigest() != metadata["repository_policy_sha256"]:
        raise ValueError("Repository policy checksum mismatch")
    policy = json.loads(policy_file.read_text())
    packages = indexed_packages(apk, pages)
    if policy["hosted_packages"] != len(packages):
        raise ValueError("Repository policy package count mismatch")
    if set(policy["official_reused"]) & packages.keys():
        raise ValueError("Officially reusable packages were accidentally republished")
    abi = metadata["kernel_abi"]
    version, release, vermagic = abi.split("-", 2)
    kernel_version = f"{version}~{vermagic}-r{release}"
    if packages.get("kernel", {}).get("version") != kernel_version:
        raise ValueError(f"Kernel APK does not match repository ABI: {abi}")
    for name, package in packages.items():
        if name.startswith("kmod-") and f"kernel={kernel_version}" not in package.get("depends", []):
            raise ValueError(f"KMOD ABI mismatch: {name}")
    for name in REINSTALL_TEST_PACKAGES:
        if name not in packages and name not in policy["official_reused"]:
            raise ValueError(f"Plugin APK unavailable: {name}")

    images = list((root / "bin/targets/qualcommbe/ipq53xx").glob("*re-cs-08*sysupgrade.bin"))
    factories = list((root / "bin/targets/qualcommbe/ipq53xx").glob("*re-cs-08*factory.bin"))
    if len(images) != 1 or len(factories) != 1:
        raise ValueError("Expected exactly one RE-CS-08 sysupgrade and factory image")
    with tempfile.TemporaryDirectory(prefix="re-cs-08-validation-") as temporary:
        work = Path(temporary)
        with tarfile.open(baseline_image or images[0]) as archive:
            kernels = [item for item in archive.getmembers() if item.name.endswith("/kernel")]
            roots = [item for item in archive.getmembers() if item.name.endswith("/root")]
            if len(kernels) != 1 or len(roots) != 1:
                raise ValueError("Malformed sysupgrade image")
            margin = kernel_margin(kernels[0].size)
            with (work / "root.squashfs").open("wb") as output:
                shutil.copyfileobj(archive.extractfile(roots[0]), output)
        firmware = work / "firmware"
        run([root / "staging_dir/host/bin/unsquashfs4", "-no-progress", "-d", firmware,
             work / "root.squashfs", "etc/apk", "lib/apk/db", "etc/uci-defaults"])
        if not baseline_image:
            validate_installed(root / "expected-packages.json", firmware / "lib/apk/db/installed")
        public = (root / "public-key.pem").read_bytes()
        if (pages / "public-key.pem").read_bytes() != public:
            raise ValueError("Published key differs from build key")
        keys = firmware / "etc/apk/keys"
        if not any(path.read_bytes() == public for path in keys.glob("*.pem")):
            raise ValueError("Firmware does not trust the repository signing key")
        feeds = firmware / "etc/apk/repositories.d/distfeeds.list"
        feed_text = feeds.read_text()
        for repo in metadata["repositories"]:
            if repo["url"] not in feed_text.splitlines():
                raise ValueError(f"Firmware feed missing: {repo['url']}")
            feed_text = feed_text.replace(repo["url"], str(pages / repo["path"]))
        if re.search(r"downloads\.immortalwrt\.org/\S*/targets/qualcommbe/", feed_text):
            raise ValueError("Broken official target feed remains in firmware")
        feeds.write_text(feed_text)
        arch = package_architecture(packages)
        base = [apk, "--root", firmware, "--arch", arch, "--keys-dir", keys, "--no-cache"]
        retry([*base, "update"])
        if baseline_image:
            # Keep every installed component, prohibit downgrades and pin kernel
            # modules exactly while resolving upgrades against the published image.
            constraints = baseline_constraints((firmware / "lib/apk/db/installed").read_text(),
                                               kernel_version, packages)
            retry([*base, "add", "--simulate", "--upgrade", *constraints])
        retry([*base, "add", "--simulate", "docker", "dockerd", "luci-app-dockerman",
               "docker-compose"])

        # Remove the tested package records so the test cannot pass by retaining the
        # preinstalled copies. The repositories must really supply these APKs.
        reinstall = REINSTALL_TEST_PACKAGES
        installed = firmware / "lib/apk/db/installed"
        records = installed.read_text().split("\n\n")
        records = [record for record in records
                   if not any(f"P:{name}" in record.splitlines() for name in reinstall)]
        installed.write_text("\n\n".join(records))
        retry([*base, "add", "--simulate",
               *[f"{name}=" + (packages[name]["version"] if name in packages
                               else policy["official_reused"][name]["official_version"])
                 for name in sorted(reinstall)]])
        result = {"kernel_size": 6 * 1024 * 1024 - margin, "kernel_headroom": margin,
                  "kernel_abi": abi, "hosted_packages": len(packages),
                  "source_fingerprint": metadata["fingerprint"],
                  "docker_dependency_test": "passed", "axonhub_reinstall_test": "passed",
                  "plugin_reinstall_test": "passed",
                  "official_packages_reused": len(policy["official_reused"]),
                  "retained_overlaps": len(policy["retained_overlaps"]),
                  "pcie_ssd_support_test": "passed"}
        report.write_text(json.dumps(result, indent=2) + "\n")
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary, "a") as output:
                output.write(f"\nRE-CS-08: kernel headroom **{margin / 1024:.1f} KiB**; "
                             f"{len(packages)} signed APKs; Docker/AxonHub solver checks passed.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("pages", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--baseline-image", type=Path)
    args = parser.parse_args()
    validate(args.root.resolve(), args.pages.resolve(), args.report, args.baseline_image)
