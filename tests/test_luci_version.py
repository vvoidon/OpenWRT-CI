import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]

# The branch expression from upstream luci.mk, not a replacement display label.
BRANCH_MAKE = r"""
PKG_GITBRANCH:=$(strip $(shell \
 variant="LuCI"; \
 if git log -1 >/dev/null 2>/dev/null; then \
  branch=$$(git branch --format='%(refname:strip=3)' --remote --no-abbrev --contains 2>/dev/null | tail -n1); \
  branch=$${branch:-$$(git -c core.abbrev=7 branch --format='%(refname:strip=2)' --contains 2>/dev/null | tail -n1)}; \
  if [ "$$branch" != "master" ]; then variant="LuCI $${branch:-unknown} branch"; else variant="LuCI Master"; fi; \
 fi; echo "$$variant"))
.PHONY: label
label:
	@printf '%s\n' '$(PKG_GITBRANCH)'
"""


class LuCIVersionTests(unittest.TestCase):
    def test_real_version_expression_uses_master_without_changing_locked_sha_or_patches(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            upstream = base / "upstream"
            upstream.mkdir()
            def git(directory, *args):
                return subprocess.check_output(["git", "-C", str(directory), *args], stderr=subprocess.DEVNULL, text=True).strip()
            git(upstream, "init", "-q", "--initial-branch=master")
            git(upstream, "config", "user.email", "test@example.invalid")
            git(upstream, "config", "user.name", "test")
            (upstream / "tracked").write_text("old\n")
            git(upstream, "add", ".")
            git(upstream, "commit", "-qm", "locked")
            locked = git(upstream, "rev-parse", "HEAD")
            (upstream / "tracked").write_text("new\n")
            git(upstream, "commit", "-qam", "new upstream head")
            tip = git(upstream, "rev-parse", "HEAD")
            root = base / "wrt"
            checkout = root / "feeds/luci"
            checkout.parent.mkdir(parents=True)
            subprocess.run(["git", "clone", "-q", "--depth=1", upstream.as_uri(), str(checkout)], check=True)
            git(checkout, "fetch", "-q", "--depth=1", "origin", locked)
            git(checkout, "checkout", "-q", "--detach", locked)
            (checkout / "tracked").write_text("CI patch\n")
            def label():
                return subprocess.check_output(["make", "--no-print-directory", "-s", "-f", "-", "label"],
                                               cwd=checkout, input=BRANCH_MAKE, text=True).strip()
            self.assertIn("detached", label())
            # The original luci-base version.uc recipe used by the firmware UI.
            version_recipe = BRANCH_MAKE + r'''
LUCI_GITBRANCH=$(PKG_GITBRANCH)
.PHONY: version.uc
version.uc:
	@echo "export const revision = '$(LUCI_VERSION)', branch = '$(LUCI_GITBRANCH)';" > $@
'''
            def version_file():
                subprocess.run(["make", "--no-print-directory", "-s", "-f", "-", "version.uc",
                                f"LUCI_VERSION={locked[:7]}"], cwd=checkout, input=version_recipe,
                               text=True, check=True)
                return (checkout / "version.uc").read_text()
            self.assertIn("detached", version_file())
            state = base / "state"
            state.write_text(f"immortalwrt/luci|master|feed|{locked}\n")
            subprocess.run([sys.executable, str(PROJECT / "scripts/source_inputs.py"), "verify",
                            "--root", str(root), "--state", str(state)], check=True, capture_output=True)
            self.assertEqual(label(), "LuCI Master")
            self.assertEqual(version_file(), f"export const revision = '{locked[:7]}', branch = 'LuCI Master';\n")
            self.assertEqual(git(checkout, "rev-parse", "HEAD"), locked)
            self.assertEqual(git(checkout, "rev-parse", "origin/master"), tip)
            self.assertEqual((checkout / "tracked").read_text(), "CI patch\n")
            self.assertEqual(git(checkout, "rev-parse", "refs/ci/previous-branch/master"), tip)
            subprocess.run([sys.executable, str(PROJECT / "scripts/source_inputs.py"), "verify",
                            "--root", str(root), "--state", str(state)], check=True, capture_output=True)
            self.assertEqual(label(), "LuCI Master")


if __name__ == "__main__":
    unittest.main()
