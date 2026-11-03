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
from prepare_host_tools import (configure_rust, copy_portable, disable_host_python_pgo,
                                configure_bpf, file_hash, prepare, rust_requirements,
                                validate_bundle, verify_host_python_pgo)


class HostToolsTests(unittest.TestCase):
    def fixtures(self, base):
        root = base / "wrt"
        rust = root / "feeds/packages/lang/rust"
        python = root / "feeds/packages/lang/python/python3"
        rust.mkdir(parents=True)
        python.mkdir(parents=True)
        (root / ".config").write_text("CONFIG_TARGET_qualcommbe=y\nCONFIG_GOLANG_BUILD_BOOTSTRAP=y\n")
        (root / "rules.mk").write_text("")
        (rust / "rust-values.mk").write_text("RUSTC_TARGET_ARCH:=aarch64-unknown-linux-musl\n")
        (rust / "Makefile").write_text("PKG_VERSION:=1.96.0\n$(eval $(call HostBuild))\n")
        (python / "Makefile").write_text("CONFIGURE_ARGS += \\\n\t--enable-optimizations\n\nHOST_CONFIGURE_ARGS += \\\n\t--enable-optimizations \\\n\t--without-ensurepip\n\n")
        go_source, rust_source = base / "runner-go", base / "runner-rust"
        for provider, names in ((go_source, ["go"]), (rust_source, ["cargo", "rustc", "rustdoc"])):
            (provider / "bin").mkdir(parents=True)
            for name in names:
                binary = provider / "bin" / name
                binary.write_text("#!/bin/sh\nprintf 'compiler\\n'\n")
                binary.chmod(0o755)
        std = rust_source / "lib/rustlib/aarch64-unknown-linux-musl/lib"
        std.mkdir(parents=True)
        (std / "libstd-fixture.rlib").write_bytes(b"target std")
        return root, go_source, rust_source

    def test_pgo_patch_only_changes_host_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _, _ = self.fixtures(Path(temporary))
            makefile = root / "feeds/packages/lang/python/python3/Makefile"
            disable_host_python_pgo(makefile)
            first = makefile.read_text()
            self.assertEqual(first.count("--enable-optimizations"), 1)
            self.assertEqual(first.count("--disable-optimizations"), 1)
            disable_host_python_pgo(makefile)
            self.assertEqual(first, makefile.read_text())
            makefile.write_text("HOST_CONFIGURE_ARGS:=--enable-optimizations\nCONFIGURE_ARGS+=--enable-optimizations\n")
            disable_host_python_pgo(makefile)
            self.assertEqual(makefile.read_text(), "HOST_CONFIGURE_ARGS:=--disable-optimizations\nCONFIGURE_ARGS+=--enable-optimizations\n")

    def test_feed_controls_exact_rust_version_and_target_via_real_make(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _, _ = self.fixtures(Path(temporary))
            self.assertEqual(rust_requirements(root), ("1.96.0", "aarch64-unknown-linux-musl"))

    def test_bundled_tools_survive_runner_removal_and_relocate_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, go_source, rust_source = self.fixtures(base)
            def query(*args, **kwargs):
                if args[0] == "go": return str(go_source)
                if args[0] == go_source / "bin/go": return "go1.26.0"
                if args[-1] == "ci-host-python-args": return "--disable-optimizations"
                if args[0] == "make": return "aarch64-unknown-linux-musl"
                if args[0] == "rustup": return str(rust_source)
                raise AssertionError(args)
            with patch("prepare_host_tools.output", side_effect=query), \
                    patch.dict(os.environ, GITHUB_WORKSPACE=str(base)), \
                    patch("prepare_host_tools.llvm_identity", return_value={"version": "18", "path": "/fixture/llvm"}), \
                    patch("prepare_host_tools.configure_bpf"), \
                    patch("prepare_host_tools.subprocess.run") as install:
                prepare(root)
                self.assertEqual(install.call_args.args[0], ["rustup", "toolchain", "install", "1.96.0",
                    "--profile", "minimal", "--component", "rust-src", "--target", "aarch64-unknown-linux-musl"])
            shutil.move(str(go_source), base / "gone-go")
            shutil.move(str(rust_source), base / "gone-rust")
            relocated = base / "restored"
            shutil.copytree(root, relocated, symlinks=True)
            with patch("prepare_host_tools.output", return_value="aarch64-unknown-linux-musl"), \
                    patch.dict(os.environ, GITHUB_WORKSPACE=str(base)), \
                    patch("prepare_host_tools.llvm_identity", return_value={"version": "18", "path": "/fixture/llvm"}), \
                    patch("prepare_host_tools.configure_bpf"), \
                    patch("prepare_host_tools.verify_host_python_pgo"), \
                    patch("prepare_host_tools.subprocess.run") as network:
                prepare(relocated, reuse=True)
                network.assert_not_called()
            config = (relocated / ".config").read_text()
            self.assertIn(str(relocated / "host-toolchains/go/go1.26.0"), config)
            self.assertNotIn(str(go_source), config)
            recipe = (relocated / "feeds/packages/lang/rust/ci-prebuilt.mk").read_text()
            self.assertIn("$(TOPDIR)/host-toolchains/rust/1.96.0", recipe)
            self.assertNotIn(str(rust_source), recipe)
            self.assertTrue((relocated / "host-toolchains/rust/1.96.0/lib/rustlib/aarch64-unknown-linux-musl/lib/libstd-fixture.rlib").is_file())
            with patch("prepare_host_tools.llvm_identity", return_value={"version": "19", "path": "/fixture/llvm"}):
                with self.assertRaisesRegex(ValueError, "Host LLVM differs"):
                    prepare(relocated, reuse=True)

    def test_effective_python_flags_catch_a_later_pgo_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _, _ = self.fixtures(Path(temporary))
            makefile = root / "feeds/packages/lang/python/python3/Makefile"
            disable_host_python_pgo(makefile)
            verify_host_python_pgo(root)
            with makefile.open("a") as stream:
                stream.write("override HOST_CONFIGURE_ARGS += --enable-optimizations\n")
            disable_host_python_pgo(makefile)
            with self.assertRaisesRegex(ValueError, "Effective host Python"):
                verify_host_python_pgo(root)
            makefile.unlink()
            disable_host_python_pgo(makefile)
            verify_host_python_pgo(root)

    def test_bpf_failure_preserves_config_and_success_selects_one_toolchain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _, _ = self.fixtures(Path(temporary))
            config = root / ".config"
            original = config.read_text() + 'CONFIG_BPF_TOOLCHAIN_BUILD_LLVM=y\nCONFIG_BPF_TOOLCHAIN_HOST_PATH="old"\n'
            config.write_text(original)
            identity = {"path": "/fixture/llvm"}
            with patch("prepare_host_tools.subprocess.run", side_effect=subprocess.CalledProcessError(1, "clang")):
                with self.assertRaises(subprocess.CalledProcessError):
                    configure_bpf(root, identity)
            self.assertEqual(config.read_text(), original)
            with patch("prepare_host_tools.subprocess.run") as run:
                configure_bpf(root, identity)
                first = config.read_text()
                configure_bpf(root, identity)
                self.assertEqual(run.call_count, 10)
            self.assertEqual(config.read_text(), first)
            self.assertIn('CONFIG_BPF_TOOLCHAIN_HOST_PATH="/fixture/llvm"', first)
            self.assertNotIn("CONFIG_BPF_TOOLCHAIN_BUILD_LLVM", first)

    def test_internal_absolute_links_are_rebased_and_external_links_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            (source / "actual").write_text("value")
            (source / "alias").symlink_to(source / "actual")
            copy_portable(source, base / "copied")
            self.assertFalse((base / "copied/alias").readlink().is_absolute())
            self.assertEqual((base / "copied/alias").read_text(), "value")
            (source / "external").symlink_to(base / "outside")
            with self.assertRaisesRegex(ValueError, "External toolchain symlink"):
                copy_portable(source, base / "bad")

    def test_rust_inclusion_is_idempotent_and_cache_identity_tracks_host_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _, _ = self.fixtures(Path(temporary))
            configure_rust(root, "host-toolchains/rust/1.96.0")
            configure_rust(root, "host-toolchains/rust/1.96.0")
            self.assertEqual((root / "feeds/packages/lang/rust/Makefile").read_text().count("include ./ci-prebuilt.mk"), 1)
        script = (PROJECT / "scripts/source_inputs.py").read_text()
        self.assertIn('"HOST_TOOLS_IDENTITY"', script)
        self.assertIn('"scripts/prepare_host_tools.py"', script)
        workflow = (PROJECT / ".github/workflows/re-cs-08.yml").read_text()
        self.assertEqual(workflow.count("./wrt/host-toolchains"), 2)
        self.assertIn("prepare_host_tools.py\" . --reuse", workflow)

    def test_corrupt_or_incompatible_bundles_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, go_source, rust_source = self.fixtures(Path(temporary))
            copy_portable(go_source, root / "host-toolchains/go/version")
            copy_portable(rust_source, root / "host-toolchains/rust/version")
            manifest = {"version": 1, "go": {"version": "go1.26.0", "path": "host-toolchains/go/version",
                        "executables": {"go": file_hash(go_source / "bin/go")}},
                        "rust": {"version": "1.96.0", "path": "host-toolchains/rust/version",
                        "target": "aarch64-unknown-linux-musl", "executables": {
                            name: file_hash(rust_source / "bin" / name) for name in ("cargo", "rustc", "rustdoc")}}}
            validate_bundle(root, manifest)
            for section, key, value, error in (("go", "path", "/runner/go", "outside"),
                                               ("rust", "version", "1.95.0", "no longer matches"),
                                               ("rust", "executables", {}, "Incomplete")):
                changed = json.loads(json.dumps(manifest))
                changed[section][key] = value
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, error):
                    validate_bundle(root, changed)
            (root / "host-toolchains/go/version/bin/go").write_text("corrupt")
            with self.assertRaisesRegex(ValueError, "corrupt"):
                validate_bundle(root, manifest)

    def test_report_collection_continues_without_git_and_keeps_raw_timing(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "wrt"
            (root / "scripts").mkdir(parents=True)
            (root / "rules.mk").write_text("STAGING_DIR:=$(TOPDIR)/staging_dir/target\n")
            feeds = root / "scripts/feeds"
            feeds.write_text("#!/bin/sh\nprintf 'locked feeds\\n'\n")
            feeds.chmod(0o755)
            report_tool = root / "scripts/build-time-report.pl"
            report_tool.write_text("#!/bin/sh\nprintf 'timing report\\n'\n")
            report_tool.chmod(0o755)
            report = base / "build-reports"
            report.mkdir()
            (report / "parallel.tsv").write_text("raw events\n")
            subprocess.run(["bash", str(PROJECT / "scripts/collect-build-reports.sh")], cwd=root,
                           env=dict(os.environ, GITHUB_WORKSPACE=str(base)), check=True, capture_output=True)
            self.assertTrue((report / "source-metadata-errors.txt").is_file())
            self.assertEqual((report / "parallel.tsv").read_text(), "raw events\n")
            self.assertEqual((report / "parallel-report.txt").read_text(), "timing report\n")


if __name__ == "__main__":
    unittest.main()
