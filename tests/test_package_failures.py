import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from report_package_failures import collect


class PackageFailureTests(unittest.TestCase):
    def fixture(self, base):
        root = base / "wrt"
        (root / "tmp").mkdir(parents=True)
        (root / "logs/package/feeds/packages/libpfring").mkdir(parents=True)
        (root / ".config").write_text("CONFIG_PACKAGE_kmod-pf-ring=m\nCONFIG_PACKAGE_libpfring=m\n")
        (root / "tmp/.packageinfo").write_text(
            "Source-Makefile: package/feeds/packages/libpfring/Makefile\n"
            "Package: libpfring\nType: ipkg\n\nPackage: kmod-pf-ring\nType: ipkg\n")
        return root, base / "build-reports/package-failures.json"

    def test_failed_optional_source_is_reported_once_with_bounded_log_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, output = self.fixture(Path(temporary))
            error = "   ERROR: package/feeds/packages/libpfring failed to build.\n"
            (root / "logs/package/error.txt").write_text(error * 2)
            (root / "logs/package/feeds/packages/libpfring/compile.txt").write_text(
                "x" * 100000 + "\npf_ring.c: incompatible sockaddr_unsized\n")
            messages = StringIO()
            with redirect_stdout(messages):
                report = collect(root, output)
            self.assertIn("package/feeds/packages/libpfring", messages.getvalue())
            self.assertEqual(report["skipped_optional_sources"], 1)
            self.assertEqual(len(report["failed_sources"]), 1)
            source = report["failed_sources"][0]
            self.assertEqual(source["selected_packages"], {"libpfring": "m", "kmod-pf-ring": "m"})
            self.assertEqual(len(source["errors"]), 1)
            tail = output.parent / source["log_tails"][0]
            self.assertEqual(tail.stat().st_size, 65536)
            self.assertIn("sockaddr_unsized", tail.read_text())
            self.assertEqual(json.loads(output.read_text()), report)

    def test_a_shared_source_with_any_installed_package_is_not_reported_as_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, output = self.fixture(Path(temporary))
            (root / ".config").write_text("CONFIG_PACKAGE_kmod-pf-ring=m\nCONFIG_PACKAGE_libpfring=y\n")
            (root / "logs/package/error.txt").write_text(
                "ERROR: package/feeds/packages/libpfring [host] failed to build.\n")
            report = collect(root, output)
            self.assertEqual(report["skipped_optional_sources"], 0)
            self.assertFalse(report["failed_sources"][0]["repository_only"])

    def test_no_native_error_log_produces_an_empty_successful_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, output = self.fixture(Path(temporary))
            report = collect(root, output)
            self.assertEqual(report["failed_sources"], [])
            self.assertEqual(report["skipped_optional_sources"], 0)

    def test_feed_symlink_and_real_metadata_path_identify_the_same_failed_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, output = self.fixture(Path(temporary))
            actual = root / "feeds/packages/libs/libpfring"
            actual.mkdir(parents=True)
            alias = root / "package/feeds/packages/libpfring"
            alias.parent.mkdir(parents=True)
            alias.symlink_to(actual, target_is_directory=True)
            info = root / "tmp/.packageinfo"
            info.write_text(info.read_text().replace("package/feeds/packages/libpfring/Makefile",
                                                     "feeds/packages/libs/libpfring/Makefile"))
            (root / "logs/package/error.txt").write_text("ERROR: package/feeds/packages/libpfring failed to build.\n")
            with redirect_stdout(StringIO()):
                report = collect(root, output)
            self.assertEqual(report["skipped_optional_sources"], 1)

    def test_successful_retry_is_not_misreported_as_a_skipped_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, output = self.fixture(Path(temporary))
            (root / "logs/package/error.txt").write_text("ERROR: package/feeds/packages/libpfring failed to build.\n")
            (root / "bin").mkdir()
            for name in ("libpfring", "kmod-pf-ring"):
                (root / "bin" / f"{name}-1.apk").touch()
            def inspect(arguments, **kwargs):
                name = Path(arguments[-1]).name.removesuffix("-1.apk")
                return json.dumps({"info": {"name": name}})
            with patch("report_package_failures.subprocess.check_output", side_effect=inspect):
                report = collect(root, output)
            self.assertEqual(report["skipped_optional_sources"], 0)
            self.assertTrue(report["failed_sources"][0]["recovered"])
            self.assertEqual(report["failed_sources"][0]["missing_packages"], [])
