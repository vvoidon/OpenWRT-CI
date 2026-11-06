import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

PROJECT = Path(__file__).resolve().parents[1]


class UpstreamIntegrationTests(unittest.TestCase):
    @unittest.skipIf(os.geteuid() == 0, "Permission regression requires an unprivileged runner")
    @unittest.skipUnless(shutil.which("zstd"), "Baseline archive test requires zstd")
    def test_workflow_saves_baseline_under_a_writable_parent_on_protected_mnt(self):
        workflow = (PROJECT / ".github/workflows/re-cs-08.yml").read_text()

        def step(name):
            block = workflow.split("      - name: " + name + "\n", 1)[1]
            return textwrap.dedent(block.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0])

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            mount = base / "mnt"
            workspace = base / "workspace"
            mount.mkdir()
            workspace.mkdir()
            (workspace / "scripts").symlink_to(PROJECT / "scripts", target_is_directory=True)
            state = workspace / "build-state"
            state.mkdir()
            (state / "source-state.txt").write_text("example/feed|main|feed|" + "a" * 40 + "\n")
            (state / "source-coverage.json").write_text("{}\n")
            # Model /mnt: root-controlled creation is allowed only through sudo.
            # Children created by this shim already belong to the test runner.
            prefix = '''
sudo() {
    case "$1" in
        -E|timedatectl|chown) return 0 ;;
        mkdir)
            chmod u+w "$TEST_MNT"
            command "$@"
            chmod u-w "$TEST_MNT"
            ;;
        ln) command "$@" ;;
        *) return 99 ;;
    esac
}
'''
            mount.chmod(0o555)
            try:
                script = prefix + step("Initialize build environment")
                script += '\nmkdir -p "$GITHUB_WORKSPACE/wrt/tmp"\n'
                script += '\nprintf compiled > "$GITHUB_WORKSPACE/wrt/preserved"\n'
                script += step("Save persistent incremental build baseline")
                result = subprocess.run(["bash", "-euo", "pipefail", "-c", script.replace("/mnt/", str(mount) + "/")],
                                        cwd=workspace, env=dict(os.environ, GITHUB_WORKSPACE=str(workspace),
                                        TEST_MNT=str(mount)), capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                root = (workspace / "wrt").resolve()
                manifest = root.parent / "build-baseline/build-baseline.json"
                self.assertTrue(manifest.is_file())
                data = json.loads(manifest.read_text())
                self.assertEqual(data["root"], str(root))
                self.assertTrue(data["parts"])
                self.assertTrue(all((manifest.parent / part["name"]).is_file() for part in data["parts"]))
                # These are the actual sibling paths used by refresh/compile.
                for name in ("incremental-previous-sources", "incremental-old-apks"):
                    (root.parent / name).mkdir()
                restore = step("Restore immutable build baseline")
                self.assertIn(str(root.relative_to(base)).removeprefix("mnt/") + " ./baseline-downloads", restore)
                package = step("Package firmware release")
                self.assertIn(str(manifest.parent.relative_to(base)) + "/build-baseline*", package)
            finally:
                mount.chmod(0o755)

    def test_luci_refresh_reapplies_baseline_customizations_without_touching_kernel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = {
                "feeds/luci/collections/luci/Makefile": "DEPENDS:=+luci-theme-bootstrap\nDEPENDS+=luci-app-attendedsysupgrade\n",
                "feeds/luci/modules/luci-mod-system/flash.js": "var address = '192.168.1.1';\n",
                "feeds/luci/modules/luci-mod-status/10_system.js": "var version = (luciversion || '');\n",
            }
            def reset_feed():
                for name, content in original.items():
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
            reset_feed()
            (root / ".config").write_text("kernel and package selections\n")
            env = dict(os.environ, WRT_THEME="aurora", WRT_IP="192.168.10.1",
                       WRT_MARK="vvoidon", WRT_DATE="baseline-date")
            command = [sys.executable, PROJECT / "scripts/configure_luci.py", root]
            subprocess.run(command, env=env, check=True)
            expected = {name: (root / name).read_text() for name in original}
            self.assertIn("luci-theme-aurora", expected[next(iter(original))])
            self.assertNotIn("attendedsysupgrade", expected[next(iter(original))])
            self.assertIn("192.168.10.1", expected[list(original)[1]])
            self.assertIn("vvoidon-baseline-date", expected[list(original)[2]])
            reset_feed()
            for _ in range(2):
                subprocess.run([*command, "--reuse"], env=dict(env, WRT_DATE="today"), check=True)
                self.assertEqual({name: (root / name).read_text() for name in original}, expected)
                self.assertEqual((root / ".config").read_text(), "kernel and package selections\n")
            workflow = (PROJECT / ".github/workflows/re-cs-08.yml").read_text()
            incremental = workflow.split("      - name: Refresh only changed userspace inputs", 1)[1].split("      - name:", 1)[0]
            self.assertIn('configure_luci.py" . --reuse', incremental)
            self.assertNotIn("configure-firmware.sh", incremental)

    def test_i2c_python_build_runs_only_when_smbus_is_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            makefile = root / "feeds/packages/utils/i2c-tools/Makefile"
            makefile.parent.mkdir(parents=True)
            makefile.write_text("PKG_BUILD_DEPENDS:=python-setuptools/host\n"
                                "define Package/python3-smbus\n  DEPENDS:=+libi2c +python3-light\nendef\n"
                                "define Py3Build/Compile\n@echo python-compile\nendef\n"
                                "define Py3Build/Install\n@echo python-install\nendef\n"
                                "all:\n\t@echo c-tools\n\t$(call Py3Build/Compile)\n\t$(call Py3Build/Install)\n")
            command = ["bash", PROJECT / "scripts/prepare-i2c-tools.sh"]
            subprocess.run(command, cwd=root, check=True)
            first = makefile.read_text()
            subprocess.run(command, cwd=root, check=True)
            self.assertEqual(makefile.read_text(), first)
            for selected in ("", "y", "m"):
                result = subprocess.check_output(["make", "--no-print-directory", "-s", "-f", makefile,
                                                  "CONFIG_PACKAGE_python3-smbus=" + selected], text=True)
                self.assertIn("c-tools", result)
                self.assertEqual("python-compile" in result, bool(selected))
                self.assertEqual("python-install" in result, bool(selected))

    @unittest.skipUnless(shutil.which("jq"), "Release cleanup test requires jq")
    def test_release_cleanup_preserves_unrelated_and_draft_releases(self):
        workflow = (PROJECT / ".github/workflows/re-cs-08.yml").read_text()
        script = textwrap.dedent(workflow.split("      - name: Keep only the latest firmware release", 1)[1].split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases = [{"tag_name": name, "draft": draft} for name, draft in (
                ("RE-CS-08-current", False), ("RE-CS-08-old", False),
                ("RE-CS-08-draft", True), ("other-device", False), ("v1.0", False))]
            (root / "releases.json").write_text(json.dumps(releases))
            prefix = '''
gh() {
    case "$1 $2" in
        'release view') return 0 ;;
        'api --paginate') jq -r "$5" "$RUNNER_TEMP/releases.json" ;;
        'release delete') printf '%s\\n' "$3" >> "$RUNNER_TEMP/deleted" ;;
        *) return 1 ;;
    esac
}
'''
            subprocess.run(["bash", "-c", prefix + script], env=dict(os.environ,
                           RELEASE_TAG="RE-CS-08-current", GITHUB_REPOSITORY="example/ci",
                           RUNNER_TEMP=str(root)), check=True, capture_output=True)
            self.assertEqual((root / "deleted").read_text(), "RE-CS-08-old\n")
