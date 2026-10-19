import contextlib
import hashlib
import io
import tarfile
import json
import os
import shutil
import textwrap
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from source_inputs import FIRMWARE, footprint, relevant_path, state_rows, verified_coverage
from validate_packages import STORAGE_PACKAGES, validate_installed, validate_storage
from validate_release import baseline_constraints, kernel_margin, package_architecture, validate_kernel_config
from verify_published import verify
from repository_policy import (check_solution, dependency_name, fetch_index, filter_index, installed_records,
                              keep_reason, official_urls)
from prune_packages import WIRED_UNUSED_PACKAGES
from repository_update import unpack_baseline


class RepositoryUpdateTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("jq"), "workflow publication test requires jq")
    def test_real_publication_step_falls_back_and_keeps_baseline_on_success(self):
        workflow = (PROJECT / ".github/workflows/re-cs-08.yml").read_text()
        block = workflow.split("      - name: Select publication mode", 1)[1]
        script = textwrap.dedent(block.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0])
        for requested, ready, fail, expected, builds in (
                ("repository", "true", "false", "repository", 1),
                ("repository", "true", "true", "firmware", 2),
                ("repository", "false", "false", "firmware", 1),
                ("firmware", "false", "false", "firmware", 1)):
            with self.subTest(requested=requested, ready=ready, fail=fail), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for directory in ("scripts", "build-state", "baseline-downloads", "wrt"):
                    (root / directory).mkdir()
                (root / "build-state/baseline-tag.txt").write_text("RE-CS-08-original\n")
                (root / "build-state/source-state.txt").write_text("state\n")
                (root / "build-state/source-coverage.json").write_text("{}\n")
                (root / "baseline-downloads/test-sysupgrade.bin").touch()
                builder = root / "scripts/build-repositories.sh"
                builder.write_text('#!/bin/bash\nset -e\nmkdir "$2" "$2.audit"\n'
                                   'printf "{}\\n" > "$2/repositories.json"\n'
                                   'echo "${BASELINE_FIRMWARE_ROOT:-current}" >> builds\n')
                builder.chmod(0o755)
                prefix = '''
python3() {
    if [[ "$*" == *--baseline-image* && "$FAIL_BASELINE" == true ]]; then return 1; fi
    return 0
}
'''
                env = dict(os.environ, REQUESTED_MODE=requested, BASELINE_READY=ready,
                           FAIL_BASELINE=fail, GITHUB_WORKSPACE=str(root), WRT_DATE="new",
                           GITHUB_OUTPUT=str(root / "output"), GITHUB_STEP_SUMMARY=str(root / "summary"))
                subprocess.run(["bash", "-c", prefix + script], cwd=root, env=env,
                               check=True, capture_output=True)
                self.assertIn(f"build_mode={expected}\n", (root / "output").read_text())
                self.assertEqual(len((root / "builds").read_text().splitlines()), builds)
                metadata = json.loads((root / "wrt/pages/repositories.json").read_text())
                self.assertEqual(metadata["baseline_tag"],
                                 "RE-CS-08-original" if expected == "repository" else "RE-CS-08-new")
                self.assertTrue((root / "wrt/pages/source-state.txt").is_file())

    def test_baseline_constraints_pin_kernel_and_prohibit_removed_or_downgraded_packages(self):
        database = "P:kernel\nV:6.18~abc-r1\n\nP:kmod-tun\nV:6.18-r1\n\nP:luci-app-homeproxy\nV:1-r2\n\n"
        self.assertEqual(baseline_constraints(database, "6.18~abc-r1"),
                         ["kernel=6.18~abc-r1", "kmod-tun=6.18-r1", "luci-app-homeproxy>=1-r2"])
        with self.assertRaisesRegex(ValueError, "kernel ABI differs"):
            baseline_constraints(database, "6.19~new-r1")
        constraints = baseline_constraints(database, "6.18~abc-r1",
                                           {"luci-app-homeproxy": {"version": "2-r1"}})
        self.assertIn("luci-app-homeproxy=2-r1", constraints)
        self.assertIn("luci-app-homeproxy>=1-r2", constraints)

    def test_build_modes_for_unchanged_plugins_core_ci_and_force(self):
        sources = subprocess.check_output(
            [sys.executable, PROJECT / "scripts/source_inputs.py", "sources"], text=True).splitlines()
        plugin = "VIKINGYFY/packages"
        cases = [(set(), False, "none"), ({plugin}, False, "repository"),
                 ({FIRMWARE}, False, "firmware"), ({"immortalwrt/luci"}, False, "firmware"),
                 ({"example/ci"}, False, "firmware"), (set(), True, "firmware"),
                 ({plugin, FIRMWARE}, False, "firmware")]
        for changed, force, expected in cases:
            with self.subTest(changed=changed, force=force), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                rows = [*sources, "example/ci|main|ci"]
                (root / "previous").write_text("".join(
                    row + "|" + ("c" if row.split("|")[0] in changed else "a") * 40 + "\n"
                    for row in rows))
                script = r'''
git() {
    if [[ $1 == -C ]]; then shift 2; fi
    case "$1" in
        ls-remote) printf '%s\t%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "$3" ;;
        init|fetch|tag) return 0 ;;
        diff) printf 'luci-app-homeproxy/Makefile\0' ;;
        *) return 99 ;;
    esac
}
export -f git
bash "$PROJECT/scripts/check-sources.sh"
'''
                env = dict(os.environ, PROJECT=str(PROJECT), GITHUB_REPOSITORY="example/ci",
                           GITHUB_SHA="a" * 40, GITHUB_OUTPUT=str(root / "output"),
                           GITHUB_WORKSPACE=str(PROJECT), SOURCE_STATE_FILE=str(root / "current"),
                           PREVIOUS_SOURCE_STATE=str(root / "previous"),
                           FORCE_BUILD=str(force).lower())
                subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True)
                output = (root / "output").read_text()
                self.assertIn(f"build_mode={expected}\n", output)
                self.assertIn(f"should_build={str(expected != 'none').lower()}\n", output)

    def test_baseline_checksum_is_verified_before_extracting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "jdcloud_re-cs-08-sysupgrade.bin"
            with tarfile.open(image, "w") as archive:
                member = tarfile.TarInfo("sysupgrade-re-cs-08/root")
                member.size = 4
                archive.addfile(member, io.BytesIO(b"root"))
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            (root / "SHA256SUMS").write_text(f"{digest}  {image.name}\n")
            with patch("repository_update.subprocess.run") as extract:
                self.assertEqual(unpack_baseline(root, root / "valid", Path("unsquashfs")), image)
                extract.assert_called_once()
                image.write_bytes(b"corrupt")
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    unpack_baseline(root, root / "invalid", Path("unsquashfs"))
                self.assertFalse((root / "invalid").exists())

    def test_only_full_builds_publish_firmware_releases(self):
        workflow = (PROJECT / ".github/workflows/re-cs-08.yml").read_text()
        self.assertIn("if: needs.build.outputs.build_mode == 'firmware' && needs.deploy.result == 'success'", workflow)
        self.assertIn("--baseline-image", workflow)
        self.assertIn(".baseline_tag == $tag", workflow)


class SourceFilterTests(unittest.TestCase):
    def setUp(self):
        self.repo = "immortalwrt/packages"
        self.coverage = {
            "version": 1,
            "dts": ["target/linux/qualcommbe/dts/ipq5322-re-cs-08.dts",
                    "target/linux/qualcommbe/dts/ipq5332-common.dtsi"],
            "sources": {self.repo: {"revision": "a" * 40,
                                   "known": ["net/curl", "net/unneeded", "lang/golang"],
                                   "used": ["net/curl", "lang/golang"]}},
        }

    def test_used_packages_host_tools_and_shared_files_trigger(self):
        for path in ("net/curl/Makefile", "net/curl/patches/fix.patch",
                     "lang/golang/Makefile", "include/common.mk", "new-package/Makefile"):
            self.assertTrue(relevant_path(self.repo, "feed", path, self.coverage), path)

    def test_unused_package_and_docs_do_not_trigger(self):
        for path in ("net/unneeded/Makefile", "README.md", ".github/workflows/test.yml"):
            self.assertFalse(relevant_path(self.repo, "feed", path, self.coverage), path)

    def test_shared_kernel_and_board_dependencies_trigger(self):
        for path in ("target/linux/qualcommbe/patches-6.18/driver.patch",
                     "target/linux/generic/config-6.18",
                     "target/linux/qualcommbe/dts/ipq5332-common.dtsi",
                     "target/linux/qualcommbe/image/ipq53xx.mk",
                     "toolchain/gcc/Makefile"):
            self.assertTrue(relevant_path(FIRMWARE, "firmware", path, self.coverage), path)

    def test_unrelated_devices_do_not_trigger(self):
        for path in ("target/linux/mediatek/Makefile",
                     "target/linux/qualcommbe/ipq95xx/config-default",
                     "target/linux/qualcommbe/image/ipq95xx.mk",
                     "target/linux/qualcommbe/dts/ipq9574-be7000.dts"):
            self.assertFalse(relevant_path(FIRMWARE, "firmware", path, self.coverage), path)

    def test_missing_coverage_is_conservative(self):
        self.assertTrue(relevant_path(self.repo, "feed", "net/unneeded/Makefile"))
        self.assertTrue(relevant_path(FIRMWARE, "firmware",
                                     "target/linux/qualcommbe/dts/ipq9574-be7000.dts"))

    def test_stale_coverage_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.json"
            path.write_text(json.dumps(self.coverage))
            self.assertIsNotNone(verified_coverage(path, [[self.repo, "HEAD", "feed", "a" * 40]]))
            self.assertIsNone(verified_coverage(path, [[self.repo, "HEAD", "feed", "b" * 40]]))

    def test_failed_filter_is_not_treated_as_no_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state").write_text(f"{self.repo}|HEAD|feed|{'a' * 40}\n")
            (root / "coverage").write_text("not json")
            (root / "paths").write_bytes(b"net/unneeded/Makefile\0")
            result = subprocess.run([sys.executable, PROJECT / "scripts/source_inputs.py",
                                     "relevant", "--repo", self.repo, "--kind", "feed",
                                     "--state", root / "state", "--coverage", root / "coverage",
                                     "--paths", root / "paths"], capture_output=True)
            self.assertNotEqual(result.returncode, 10)
            self.assertNotEqual(result.returncode, 0)


class SourceLockTests(unittest.TestCase):
    def test_unchanged_source_state_does_not_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = subprocess.check_output(
                [sys.executable, PROJECT / "scripts/source_inputs.py", "sources"], text=True)
            state = root / "previous"
            state.write_text("".join(line + "|" + "a" * 40 + "\n"
                                     for line in sources.splitlines())
                             + "example/ci|main|ci|" + "b" * 40 + "\n")
            script = r"""
git() {
    [ "$1" = ls-remote ] || return 99
    printf '%s\t%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "$3"
}
export -f git
bash "$PROJECT/scripts/check-sources.sh"
"""
            env = dict(os.environ, PROJECT=str(PROJECT), GITHUB_REPOSITORY="example/ci",
                       GITHUB_SHA="b" * 40, GITHUB_OUTPUT=str(root / "output"),
                       GITHUB_WORKSPACE=str(PROJECT), SOURCE_STATE_FILE=str(root / "current"),
                       PREVIOUS_SOURCE_STATE=str(state), FORCE_BUILD="false")
            subprocess.run(["bash", "-c", script], cwd=root, env=env, capture_output=True, check=True)
            self.assertIn("should_build=false", (root / "output").read_text())

    def test_native_tool_cache_tracks_tool_sources_and_feed_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            git("init", "-q")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "test")
            (root / "tools").mkdir()
            (root / "tools/Makefile").write_text("before")
            git("add", ".")
            git("commit", "-qm", "before")
            state = root / "state"
            state.write_text(f"immortalwrt/packages|HEAD|feed|{'a' * 40}\n")
            def key():
                return subprocess.check_output(
                    [sys.executable, PROJECT / "scripts/source_inputs.py", "cache-key",
                     "--root", root, "--state", state], text=True)
            original = key()
            self.assertEqual(key(), original)
            state.write_text(f"immortalwrt/packages|HEAD|feed|{'b' * 40}\n")
            changed_feed = key()
            self.assertNotEqual(changed_feed, original)
            (root / "tools/Makefile").write_text("after")
            git("add", "tools/Makefile")
            git("commit", "-qm", "after")
            self.assertNotEqual(key(), changed_feed)
            for path in ("package/utils/ucode/Makefile",
                         "target/linux/qualcommbe/ipq53xx/config-default"):
                previous = key()
                file = root / path
                file.parent.mkdir(parents=True)
                file.write_text("changed input")
                git("add", path)
                git("commit", "-qm", "changed input")
                self.assertNotEqual(key(), previous)

    def test_duplicate_and_missing_locks_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state"
            path.write_text("a/b|main|package|" + "c" * 40 + "\n")
            self.assertEqual(len(state_rows(path)), 1)
            path.write_text(path.read_text() * 2)
            with self.assertRaises(ValueError):
                state_rows(path)

    def test_clone_failure_stops_packages_before_removing_feeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed = root / "feeds/luci/luci-theme-aurora"
            feed.mkdir(parents=True)
            (feed / "Makefile").write_text("must remain")
            (root / "feeds/packages").mkdir(parents=True)
            (root / "state").write_text(
                "eamonxg/luci-theme-aurora|master|package|" + "a" * 40 + "\n")
            script = """
git() {
    case " $* " in
        *" fetch "*) echo fetch >> "$ATTEMPTS"; return 42 ;;
        *) return 0 ;;
    esac
}
export -f git
bash "$PROJECT/scripts/prepare-packages.sh"
"""
            env = dict(os.environ, PROJECT=str(PROJECT), SOURCE_STATE_FILE=str(root / "state"),
                       ATTEMPTS=str(root / "attempts"), RETRY_DELAY="0")
            result = subprocess.run(["bash", "-c", script], cwd=root, env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual((root / "attempts").read_text().splitlines(), ["fetch"] * 3)
            self.assertEqual((feed / "Makefile").read_text(), "must remain")

    def test_checkout_uses_lock_even_when_branch_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / "upstream"
            def git(*args):
                return subprocess.check_output(["git", *map(str, args)], text=True).strip()
            git("init", "-q", upstream)
            git("-C", upstream, "config", "user.email", "test@example.invalid")
            git("-C", upstream, "config", "user.name", "test")
            (upstream / "file").write_text("old")
            git("-C", upstream, "add", ".")
            git("-C", upstream, "commit", "-qm", "old")
            locked = git("-C", upstream, "rev-parse", "HEAD")
            (upstream / "file").write_text("new")
            git("-C", upstream, "commit", "-qam", "new")
            (root / "state").write_text(f"example/repo|main|package|{locked}\n")
            env = dict(os.environ, SOURCE_STATE_FILE=str(root / "state"),
                       GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"url.file://{upstream}.insteadOf",
                       GIT_CONFIG_VALUE_0="https://github.com/example/repo.git")
            result = subprocess.run(["bash", "-euc",
                                     'source "$1"; checkout_locked example/repo main package "$2"',
                                     "_", PROJECT / "scripts/common.sh", root / "checkout"],
                                    env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual((root / "checkout/file").read_text(), "old")

    def test_footprint_includes_host_build_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ("tmp", "feeds/packages/net/app", "feeds/packages/lang/golang",
                         "feeds/packages/net/unused", "target/linux/qualcommbe/dts"):
                (root / path).mkdir(parents=True)
            (root / "state").write_text(f"{FIRMWARE}|owrt|firmware|{'a' * 40}\n"
                                       f"immortalwrt/packages|HEAD|feed|{'b' * 40}\n")
            (root / ".config").write_text("CONFIG_PACKAGE_app=y\n")
            (root / "tmp/.packageinfo").write_text(
                "Source-Makefile: feeds/packages/net/app/Makefile\n"
                "Build-Depends: golang/host\nPackage: app\nType: ipkg\n"
                "Source-Makefile: feeds/packages/lang/golang/Makefile\nPackage: golang\n"
                "Source-Makefile: feeds/packages/net/unused/Makefile\nPackage: unused\n")
            dts = root / "target/linux/qualcommbe/dts"
            (dts / "ipq5322-re-cs-08.dts").write_text('#include "common.dtsi"\n')
            (dts / "common.dtsi").write_text("/ {};\n")
            data = footprint(root, root / "state")
            self.assertEqual(data["sources"]["immortalwrt/packages"]["used"],
                             ["lang/golang", "net/app"])
            self.assertIn("target/linux/qualcommbe/dts/common.dtsi", data["dts"])


class WiredPackagePruningTests(unittest.TestCase):
    def test_wireless_and_relay_exclusions_yield_to_retained_dependencies(self):
        for required in (False, True):
            with self.subTest(required=required), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "tmp").mkdir()
                (root / "tmp/.packageinfo").write_text("")
                graph = "config PACKAGE_essential\n"
                if required:
                    graph += ("\tselect PACKAGE_luci-proto-relay\n"
                              "\tselect PACKAGE_ath12k-firmware-qcn9274-ddwrt\n")
                graph += "config PACKAGE_luci-proto-relay\n\tselect PACKAGE_relayd\n"
                (root / "tmp/.config-package.in").write_text(graph)
                (root / ".config").write_text(
                    "CONFIG_PACKAGE_essential=y\n" + "".join(
                        f"CONFIG_PACKAGE_{name}=y\n" for name in sorted(WIRED_UNUSED_PACKAGES)))
                result = subprocess.run([sys.executable, PROJECT / "scripts/prune_packages.py"],
                                        cwd=root, text=True, capture_output=True, check=True)
                config = (root / ".config").read_text()
                kept = {"relayd", "luci-proto-relay", "ath12k-firmware-qcn9274-ddwrt"} if required else set()
                for name in WIRED_UNUSED_PACKAGES:
                    expected = (f"CONFIG_PACKAGE_{name}=y" if name in kept
                                else f"# CONFIG_PACKAGE_{name} is not set")
                    self.assertIn(expected, config)
                self.assertIn("CONFIG_PACKAGE_essential=y", config)
                if required:
                    self.assertIn("Kept for retained-package dependencies:", result.stdout)
                subprocess.run([sys.executable, PROJECT / "scripts/prune_packages.py", "--check"],
                               cwd=root, capture_output=True, check=True)


class ReleaseTests(unittest.TestCase):
    def test_natmapt_remains_repository_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "expected").write_text(json.dumps({"libc": "libc"}))
            for name in ("natmapt", "luci-app-natmapt", "luci-i18n-natmapt-zh-cn"):
                with self.subTest(name=name):
                    (root / "installed").write_text(f"P:libc\nV:1\n\nP:{name}\nV:1\n\n")
                    with self.assertRaisesRegex(ValueError, "NATMapT"):
                        validate_installed(root / "expected", root / "installed")

    def test_unselected_wireless_and_relay_packages_cannot_leak_into_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "expected").write_text(json.dumps({"libc": "libc"}))
            (root / "installed").write_text("P:libc\nV:1\n\nP:relayd\nV:1\n\n")
            with self.assertRaisesRegex(ValueError, "relayd"):
                validate_installed(root / "expected", root / "installed")
            # A real dependency that remained selected is allowed in the image.
            (root / "expected").write_text(json.dumps({"libc": "libc", "relayd": "relayd"}))
            validate_installed(root / "expected", root / "installed")

    def test_noarch_package_cannot_override_device_architecture(self):
        packages = {"accel-support": {"arch": "noarch"},
                    "kernel": {"arch": "aarch64_cortex-a53"},
                    "generic": {"arch": "all"}}
        self.assertEqual(package_architecture(packages), "aarch64_cortex-a53")

    def test_mixed_device_architectures_are_rejected(self):
        packages = {"kernel": {"arch": "aarch64_cortex-a53"}, "wrong": {"arch": "mips_24kc"}}
        with self.assertRaisesRegex(ValueError, "architecture"):
            package_architecture(packages)

    def test_ssd_packages_cannot_be_pruned_or_demoted_to_repository_only(self):
        selected = dict.fromkeys(STORAGE_PACKAGES, "y")
        validate_storage(selected)
        selected["kmod-nvme"] = "m"
        with self.assertRaisesRegex(ValueError, "kmod-nvme"):
            validate_storage(selected)

    def test_kernel_keeps_actual_pcie_phy_and_ssd_drivers(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".config"
            text = "".join(f"CONFIG_{symbol}=y\n" for symbol in
                           ("PCI", "PCIE_QCOM_COMMON", "PCIE_QCOM", "PHY_QCOM_UNIPHY_PCIE_28LP"))
            text += "".join(f"CONFIG_{symbol}=m\n" for symbol in
                            ("NVME_CORE", "BLK_DEV_NVME", "SATA_AHCI", "EXT4_FS", "BTRFS_FS", "F2FS_FS"))
            config.write_text(text)
            validate_kernel_config(config)
            config.write_text(text.replace("CONFIG_PHY_QCOM_UNIPHY_PCIE_28LP=y\n", ""))
            with self.assertRaisesRegex(ValueError, "PHY_QCOM_UNIPHY_PCIE_28LP"):
                validate_kernel_config(config)
            config.write_text(text + "CONFIG_PHY_QCOM_QMP=y\n")
            with self.assertRaisesRegex(ValueError, "Excluded board hardware"):
                validate_kernel_config(config)

    def test_kernel_overflow_fails_and_low_margin_warns(self):
        with self.assertRaises(ValueError):
            kernel_margin(6291457)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(kernel_margin(6246160), 45296)
        self.assertIn("::warning::", output.getvalue())

    def test_missing_plugin_in_actual_firmware_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "expected").write_text(json.dumps({"luci-app-axonhub": "luci-app-axonhub"}))
            (root / "installed").write_text("P:libc\nV:1\n\n")
            with self.assertRaisesRegex(ValueError, "luci-app-axonhub"):
                validate_installed(root / "expected", root / "installed")
            (root / "installed").write_text("P:luci-app-axonhub\nV:1\n\n")
            validate_installed(root / "expected", root / "installed")

    def test_preinstalled_dockerman_requires_its_requested_components_in_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packages = {"docker", "dockerd", "docker-compose", "luci-app-dockerman",
                        "luci-i18n-dockerman-zh-cn"}
            (root / "expected").write_text(json.dumps({name: name for name in packages}))
            (root / "installed").write_text("".join(f"P:{name}\nV:1\n\n" for name in packages))
            validate_installed(root / "expected", root / "installed")
            for package in sorted(packages):
                with self.subTest(package=package):
                    (root / "installed").write_text("".join(
                        f"P:{name}\nV:1\n\n" for name in packages - {package}))
                    with self.assertRaisesRegex(ValueError, package):
                        validate_installed(root / "expected", root / "installed")


class PublishedRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.base = "https://example.invalid/ci"
        self.index_url = self.base + "/snapshots/packages.adb"
        self.expected = {
            "fingerprint": "build-fingerprint",
            "public_key_sha256": hashlib.sha256(b"public").hexdigest(),
            "repositories": [{"url": self.index_url, "sha256": hashlib.sha256(b"index").hexdigest()}],
        }
        self.responses = {
            self.base + "/repositories.json": json.dumps(self.expected).encode(),
            self.base + "/public-key.pem": b"public",
            self.index_url: b"index",
        }

    def test_matching_deployment_passes(self):
        with patch("verify_published.fetch", side_effect=self.responses.__getitem__), \
                patch("verify_published.time.sleep") as sleep:
            verify(self.expected, self.base)
            sleep.assert_not_called()

    def test_published_source_state_and_coverage_are_verified(self):
        expected = {**self.expected, "fingerprint": hashlib.sha256(b"state").hexdigest(),
                    "source_coverage_sha256": hashlib.sha256(b"coverage").hexdigest()}
        responses = {**self.responses, self.base + "/repositories.json": json.dumps(expected).encode(),
                     self.base + "/source-state.txt": b"state", self.base + "/source-coverage.json": b"coverage"}
        with patch("verify_published.fetch", side_effect=responses.__getitem__):
            verify(expected, self.base)
        for name in ("source-state.txt", "source-coverage.json"):
            with self.subTest(name=name), \
                    patch("verify_published.fetch", side_effect={**responses, self.base + "/" + name: b"stale"}.__getitem__), \
                    patch("verify_published.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "source state or coverage"):
                    verify(expected, self.base)

    def test_stale_or_corrupted_deployment_blocks_release(self):
        for url, content in ((self.base + "/repositories.json", b"{}"),
                             (self.base + "/public-key.pem", b"other key"),
                             (self.index_url, b"corrupt index")):
            responses = {**self.responses, url: content}
            with self.subTest(url=url), \
                    patch("verify_published.fetch", side_effect=responses.__getitem__), \
                    patch("verify_published.time.sleep") as sleep, \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError):
                    verify(self.expected, self.base)
                self.assertEqual(sleep.call_count, 5)

    def test_policy_audit_is_verified_after_deployment(self):
        expected = {**self.expected, "repository_policy_sha256":
                    hashlib.sha256(b"policy").hexdigest()}
        responses = {**self.responses, self.base + "/repositories.json": json.dumps(expected).encode(),
                     self.base + "/repository-policy.json": b"corrupt"}
        with patch("verify_published.fetch", side_effect=responses.__getitem__), \
                patch("verify_published.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "policy"):
                verify(expected, self.base)
        responses[self.base + "/repository-policy.json"] = b"policy"
        with patch("verify_published.fetch", side_effect=responses.__getitem__):
            verify(expected, self.base)


class RepositoryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.local = {"name": "example", "version": "2-r1", "hashes": "built",
                      "arch": "aarch64_cortex-a53", "provides": ["example-any"]}
        self.official = {**self.local, "version": "1-r1", "hashes": "official"}

    def test_older_official_version_can_be_reused_after_solver_checks(self):
        self.assertIsNone(keep_reason(self.local, self.official, {}, "aarch64_cortex-a53"))

    def test_kernel_is_never_replaced_by_an_unrelated_official_build(self):
        for name in ("kernel", "kmod-nvme"):
            local = {**self.local, "name": name}
            self.assertIn("matching firmware", keep_reason(
                local, {**local}, {}, "aarch64_cortex-a53"))

    def test_incompatible_architecture_and_missing_capabilities_are_retained(self):
        self.assertIn("architecture", keep_reason(
            self.local, {**self.official, "arch": "mips"}, {}, "aarch64_cortex-a53"))
        self.assertIn("capabilities", keep_reason(
            self.local, {**self.official, "provides": []}, {}, "aarch64_cortex-a53"))
        self.assertIsNone(keep_reason(
            self.local, {**self.official, "arch": "noarch"}, {}, "aarch64_cortex-a53"))

    def test_custom_fork_exception_does_not_duplicate_identical_official_package(self):
        exceptions = {"example": "Custom configuration format differs"}
        self.assertEqual(keep_reason(self.local, self.official, exceptions,
                                     "aarch64_cortex-a53"), exceptions["example"])
        self.assertIsNone(keep_reason(self.local, {**self.local}, exceptions,
                                      "aarch64_cortex-a53"))

    def test_only_enabled_official_feeds_are_compared(self):
        url = "https://downloads.immortalwrt.org/snapshots/packages/aarch64_cortex-a53/base/packages.adb"
        self.assertEqual(official_urls("# " + url + "\n" + url +
                                      "\nhttps://example.com/custom/packages.adb\n"), [url])
        for text in ("# " + url, url + "\n" + url,
                     "https://downloads.immortalwrt.org/snapshots/targets/broken/packages.adb"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                official_urls(text)

    def test_official_index_outage_is_fatal_not_a_reason_to_publish_everything(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("repository_policy.urllib.request.urlopen", side_effect=OSError("offline")), \
                patch("repository_policy.time.sleep") as sleep:
            with self.assertRaises(OSError):
                fetch_index("https://example.invalid/packages.adb", Path(directory) / "index")
            self.assertEqual(sleep.call_count, 2)

    def test_zero_exit_status_with_solver_errors_is_rejected(self):
        result = subprocess.CompletedProcess([], 0, "[]", "ERROR: unable to select packages")
        self.assertIn("ERROR", check_solution(result, {"example": "1-r1"}))
        result.stderr = ""
        self.assertIn("omitted", check_solution(result, {"example": "1-r1"}))

    def test_replacement_must_not_downgrade_another_installed_plugin(self):
        result = subprocess.CompletedProcess(
            [], 0, json.dumps([{"name": "sing-box", "version": "1.12-r1"},
                               {"name": "homeproxy", "version": "old"}]), "")
        self.assertIn("homeproxy", check_solution(
            result, {"sing-box": "1.12-r1", "homeproxy": "new"}))
        self.assertIsNone(check_solution(result, {"sing-box": "1.12-r1", "homeproxy": "old"}))

    def test_installed_records_and_versioned_capabilities_are_parsed(self):
        records = installed_records("P:plugin\nV:1-r1\nD:libc\n\nP:libc\nV:2\n\n")
        self.assertEqual(records["plugin"][0], "1-r1")
        self.assertEqual(dependency_name("sing-box>=1.15.0_alpha10"), "sing-box")
        self.assertEqual(dependency_name("!dnsmasq<2"), "dnsmasq")

    def test_filtered_indices_use_real_filenames_and_preserve_metadata(self):
        packages = {"example": self.local}
        with patch("repository_policy.command") as execute, \
                patch("repository_policy.read_index", return_value=packages):
            filter_index("apk", "original.adb", "filtered.adb", packages, "keys", "key.pem")
            arguments = execute.call_args_list[0].args[0]
            self.assertIn("${name}-${version}.apk", arguments)
            self.assertIn("example-2-r1.apk", arguments)
        with patch("repository_policy.command"), \
                patch("repository_policy.read_index", return_value={}):
            with self.assertRaisesRegex(ValueError, "lost or changed"):
                filter_index("apk", "original.adb", "filtered.adb", packages, "keys", "key.pem")
        with self.assertRaisesRegex(ValueError, "empty"):
            filter_index("apk", "original.adb", "filtered.adb", {}, "keys", "key.pem")


if __name__ == "__main__":
    unittest.main()
