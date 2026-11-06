import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from incremental_build import affected_closure, compile_affected, refresh, restore, save
from source_inputs import FIRMWARE, change_mode, device_projection, relevant_path


class PreciseFilterTests(unittest.TestCase):
    def test_check_script_selects_incremental_and_keeps_ignored_source_locks(self):
        sources = subprocess.check_output([sys.executable, PROJECT / "scripts/source_inputs.py", "sources"], text=True).splitlines()
        for repo, path, expected in (("VIKINGYFY/packages", "plugin/Makefile", "repository"),
                                     (FIRMWARE, "package/boot/uboot-tools/uboot-envtools/files/qualcommbe_ipq95xx", "none")):
            with self.subTest(repo=repo), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                rows = [line.split("|") + [("c" if line.split("|")[0] == repo else "a") * 40] for line in sources]
                rows.append(["example/ci", "main", "ci", "a" * 40])
                (root / "previous").write_text("\n".join("|".join(row) for row in rows) + "\n")
                coverage = {"version": 1, "dts": [], "sources": {
                    name: {"revision": sha, "known": ["plugin"], "used": ["plugin"], "userspace": ["plugin"]}
                    for name, _, kind, sha in rows if kind != "ci"}}
                (root / "source-coverage.json").write_text(json.dumps(coverage))
                script = '''
git() {
    if [[ $1 == -C ]]; then shift 2; fi
    case "$1" in
        ls-remote) printf '%s\\t%s\\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "$3" ;;
        init|fetch|tag) return 0 ;;
        diff) printf '%s\\0' "$CHANGED_PATH" ;;
        *) return 99 ;;
    esac
}
export -f git
bash "$PROJECT/scripts/check-sources.sh"
'''
                env = dict(os.environ, PROJECT=str(PROJECT), CHANGED_PATH=path,
                           GITHUB_REPOSITORY="example/ci", GITHUB_SHA="a" * 40, FORCE_BUILD="false",
                           GITHUB_OUTPUT=str(root / "output"), SOURCE_STATE_FILE=str(root / "current"),
                           PREVIOUS_SOURCE_STATE=str(root / "previous"))
                subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True)
                self.assertIn(f"build_mode={expected}\n", (root / "output").read_text())
                if expected == "none":
                    self.assertIn(f"{repo}|owrt|firmware|{'c' * 40}", (root / "current").read_text())

    def test_other_platform_envtools_is_ignored_but_our_platform_and_shared_code_are_not(self):
        prefix = "package/boot/uboot-tools/uboot-envtools/"
        self.assertFalse(relevant_path(FIRMWARE, "firmware", prefix + "files/qualcommbe_ipq95xx"))
        self.assertTrue(relevant_path(FIRMWARE, "firmware", prefix + "files/qualcommbe_ipq53xx"))
        self.assertTrue(relevant_path(FIRMWARE, "firmware", prefix + "Makefile"))

    def test_image_projection_keeps_our_device_inheritance_and_shared_rules(self):
        original = """KERNEL_SIZE := 6144k
define Device/helper
  SOC := ipq5332
endef
define Device/jdcloud_re-cs-08
  $(Device/helper)
  DEVICE_MODEL := RE-CS-08
endef
TARGET_DEVICES += jdcloud_re-cs-08
define Device/other
  DEVICE_MODEL := OTHER
endef
TARGET_DEVICES += other
"""
        self.assertEqual(device_projection(original), device_projection(original.replace("OTHER", "OTHER2")))
        for before, after in (("RE-CS-08", "RE-CS-08-v2"), ("6144k", "8192k"), ("ipq5332", "ipq5322")):
            self.assertNotEqual(device_projection(original), device_projection(original.replace(before, after)))

    def test_feed_userspace_is_repository_mode_but_shared_host_or_unknown_input_is_full(self):
        coverage = {"sources": {"example/feed": {
            "known": ["net/plugin", "net/unused", "lang/compiler"],
            "used": ["net/plugin", "lang/compiler"], "userspace": ["net/plugin"]}}}
        self.assertEqual(change_mode("example/feed", "feed", ["net/plugin/Makefile"], coverage), "repository")
        self.assertEqual(change_mode("example/feed", "feed", ["lang/compiler/Makefile"], coverage), "firmware")
        self.assertEqual(change_mode("example/feed", "feed", ["new/Makefile"], coverage), "firmware")
        self.assertEqual(change_mode("example/feed", "feed", ["net/unused/Makefile"], coverage), "none")
        self.assertEqual(change_mode("example/feed", "feed", ["net/plugin/Makefile"], None), "firmware")
        core_coverage = {"sources": {FIRMWARE: {"known": ["package/network/utils/curl"],
                                                "used": ["package/network/utils/curl"],
                                                "userspace": ["package/network/utils/curl"]}}}
        self.assertEqual(change_mode(FIRMWARE, "firmware", ["package/network/utils/curl/Makefile"], core_coverage),
                         "repository")

    def test_actual_git_image_diff_is_ignored_only_when_selected_and_shared_rules_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "target/linux/qualcommbe/image/ipq53xx.mk"
            path.parent.mkdir(parents=True)
            def git(*args):
                subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            git("init", "-q")
            git("config", "user.name", "test")
            git("config", "user.email", "test@example.invalid")
            path.write_text("define Device/other\n KERNEL_SIZE := 6144k\nendef\nTARGET_DEVICES += other\n")
            git("add", ".")
            git("commit", "-qm", "before")
            git("tag", "old-source")
            path.write_text(path.read_text().replace("6144k", "8192k"))
            git("add", ".")
            git("commit", "-qm", "after")
            git("tag", "new-source")
            self.assertEqual(change_mode(FIRMWARE, "firmware", [path.relative_to(root).as_posix()], None, root), "none")


class IncrementalBuildTests(unittest.TestCase):
    def test_refresh_uses_locked_git_revision_and_preserves_old_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            upstream = base / "upstream"
            upstream.mkdir()
            def git(directory, *args):
                return subprocess.check_output(["git", "-C", str(directory), *args], stderr=subprocess.DEVNULL, text=True).strip()
            git(upstream, "init", "-q")
            git(upstream, "config", "user.name", "test")
            git(upstream, "config", "user.email", "test@example.invalid")
            (upstream / "plugin").mkdir()
            (upstream / "plugin/Makefile").write_text("PKG_VERSION:=1\n")
            git(upstream, "add", ".")
            git(upstream, "commit", "-qm", "old")
            old = git(upstream, "rev-parse", "HEAD")
            (upstream / "plugin/Makefile").write_text("PKG_VERSION:=2\n")
            git(upstream, "commit", "-qam", "new")
            new = git(upstream, "rev-parse", "HEAD")
            root = base / "wrt"
            checkout = root / "package/packages"
            checkout.parent.mkdir(parents=True)
            subprocess.run(["git", "clone", "-q", str(upstream), str(checkout)], check=True)
            git(checkout, "checkout", "-q", "--detach", old)
            (root / "tmp").mkdir()
            (root / "tmp/.packageinfo").write_text("Source-Makefile: package/packages/plugin/Makefile\nPackage: plugin\nType: ipkg\n")
            (root / ".config").write_text("CONFIG_PACKAGE_plugin=y\n")
            state = base / "state"
            state.write_text(f"VIKINGYFY/packages|main|package|{new}\n")
            manifest = {"state": [["VIKINGYFY/packages", "main", "package", old]], "coverage": {
                "sources": {"VIKINGYFY/packages": {"used": ["plugin"], "known": ["plugin"], "userspace": ["plugin"]}}}}
            def local_run(*args, **kwargs):
                args = [str(arg) for arg in args]
                args = [str(upstream) if arg == "https://github.com/VIKINGYFY/packages.git" else arg for arg in args]
                return subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
            with patch("incremental_build.run", side_effect=local_run):
                refresh(root, state, manifest, base / "plan.json")
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), new)
            self.assertEqual((base / "incremental-previous-sources/VIKINGYFY--packages/plugin/Makefile").read_text(),
                             "PKG_VERSION:=1\n")
            self.assertEqual(json.loads((base / "plan.json").read_text())["changed"], ["package/packages/plugin/Makefile"])

    def graph(self, dependency="libfoo"):
        records = {"package/lib/Makefile": {"packages": ["libfoo"], "build": []},
                   "package/app/Makefile": {"packages": ["app"], "build": []},
                   "package/unrelated/Makefile": {"packages": ["unrelated"], "build": []}}
        packages = {"libfoo": {"source": "package/lib/Makefile", "Type": "ipkg"},
                    "app": {"source": "package/app/Makefile", "Depends": dependency, "Type": "ipkg"},
                    "unrelated": {"source": "package/unrelated/Makefile", "Type": "ipkg"}}
        return records, packages

    def test_reverse_closure_uses_old_and_new_edges_without_unrelated_packages(self):
        old, old_packages = self.graph()
        new, new_packages = self.graph(dependency="")
        self.assertEqual(affected_closure(old, old_packages, new, new_packages,
                                         ["package/lib/Makefile"], set(new_packages)),
                         ["package/lib/Makefile", "package/app/Makefile"])
        with self.assertRaisesRegex(ValueError, "deleted/renamed"):
            affected_closure(old, old_packages, {}, {}, ["package/lib/Makefile"], set())

    def test_cli_runtime_consumers_do_not_need_recompilation_but_build_consumers_do(self):
        records = {"package/cli/Makefile": {"packages": ["tool"], "build": []},
                   "package/app/Makefile": {"packages": ["app"], "build": []}}
        packages = {"tool": {"source": "package/cli/Makefile", "Type": "ipkg"},
                    "app": {"source": "package/app/Makefile", "Type": "ipkg", "Depends": "tool"}}
        self.assertEqual(affected_closure(records, packages, records, packages,
                                         ["package/cli/Makefile"], set(packages)), ["package/cli/Makefile"])
        records["package/app/Makefile"]["build"] = ["tool"]
        self.assertEqual(affected_closure(records, packages, records, packages,
                                         ["package/cli/Makefile"], set(packages)),
                         ["package/cli/Makefile", "package/app/Makefile"])

    @unittest.skipUnless(shutil.which("zstd"), "baseline archive test requires zstd")
    def test_baseline_roundtrip_excludes_private_key_and_rejects_corrupt_parts(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "wrt"
            root.mkdir()
            (root / "private-key.pem").write_text("SECRET")
            (root / "preserved").write_bytes(b"content" * 200)
            state, coverage = base / "state", base / "coverage"
            state.write_text("example/feed|main|feed|" + "a" * 40 + "\n")
            coverage.write_text("{}")
            save(root, base / "archive", state, coverage, chunk_size=256)
            shutil.move(str(root), base / "original")
            manifest = restore(base / "archive", root)
            self.assertFalse((root / "private-key.pem").exists())
            self.assertEqual((root / "preserved").read_bytes(), b"content" * 200)
            first = base / "archive" / manifest["parts"][0]["name"]
            first.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                restore(base / "archive", root)

    @unittest.skipUnless(shutil.which("cc"), "real compiler smoke test requires cc")
    def test_package_only_compiles_real_c_and_preserves_unaffected_apks_without_image_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "wrt"
            root.mkdir()
            (root / "tmp").mkdir()
            records, packages = self.graph()
            config = "CONFIG_TARGET_qualcommbe=y\nCONFIG_PACKAGE_libfoo=y\nCONFIG_PACKAGE_app=y\nCONFIG_PACKAGE_unrelated=m\n"
            (root / ".config").write_text(config)
            lines = []
            for source, record in records.items():
                lines.append(f"Source-Makefile: {source}\n")
                for name in record["packages"]:
                    lines.append(f"Package: {name}\nType: ipkg\nDepends: {packages[name].get('Depends', '')}\n")
                directory = (root / source).parent
                directory.mkdir(parents=True)
                (directory / "test.c").write_text("int main(void) { return 0; }\n")
            (root / "tmp/.packageinfo").write_text("".join(lines))
            report = root.parent / "plan.json"
            report.write_text(json.dumps({"changed": ["package/lib/Makefile"], "records": records,
                                          "packages": packages, "config": config}))
            (root / "bin").mkdir()
            for name in ("libfoo", "app", "unrelated", "kernel", "kmod-tun"):
                (root / "bin" / f"{name}-1.apk").write_bytes(b"original " + name.encode())
            hostbin = root / "staging_dir/host/bin"
            hostbin.mkdir(parents=True)
            apk = hostbin / "apk"
            apk.write_text('#!/bin/bash\nname=${4##*/}\nname=${name%-[12].apk}\nprintf \'{"info":{"name":"%s"}}\' "$name"\n')
            apk.chmod(0o755)
            tools = root.parent / "tools"
            tools.mkdir()
            make = tools / "make"
            make.write_text('''#!/bin/bash
set -e
echo "$1" >> calls
target=${1%/*}
if [[ $1 == */compile ]]; then
    name=app
    [[ $target == package/lib ]] && name=libfoo
    cc "$target/test.c" -o "bin/$name-2.apk"
fi
''')
            make.chmod(0o755)
            with patch.dict(os.environ, PATH=str(tools) + ":" + os.environ["PATH"]):
                compile_affected(root, report)
            calls = (root / "calls").read_text().splitlines()
            self.assertEqual(set(calls), {"package/app/clean", "package/lib/clean",
                                          "package/app/compile", "package/lib/compile"})
            self.assertLess(calls.index("package/lib/compile"), calls.index("package/app/compile"))
            self.assertEqual((root / "bin/app-2.apk").read_bytes()[:4], b"\x7fELF")
            self.assertEqual((root / "bin/unrelated-1.apk").read_bytes(), b"original unrelated")
            self.assertEqual((root / "bin/kernel-1.apk").read_bytes(), b"original kernel")
            self.assertTrue((root.parent / "incremental-old-apks/libfoo-1.apk").exists())

    def test_workflow_incremental_failure_falls_back_and_success_skips_full_build(self):
        workflow = (PROJECT / ".github/workflows/re-cs-08.yml").read_text()
        self.assertIn("needs.incremental.outputs.ready != 'true'", workflow)
        incremental = workflow.split("  incremental:\n", 1)[1].split("\n  build:", 1)[0]
        self.assertNotIn("run: make -j", incremental)
        self.assertNotIn("target/install", incremental)
        self.assertIn("incremental_build.py compile", incremental)
        self.assertIn("artifact_name: ${{ needs.incremental.outputs.ready", workflow)


if __name__ == "__main__":
    unittest.main()
