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
