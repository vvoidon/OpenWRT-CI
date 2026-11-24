#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Adapted from VIKINGYFY/OpenWRT-CI host bootstrap preparation.
"""Bundle host toolchains inside the build tree for cache and baseline portability."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

PROJECT = Path(__file__).resolve().parents[1]


def output(*args, **kwargs):
    return subprocess.check_output([str(arg) for arg in args], text=True, **kwargs).strip()


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_portable(source, destination):
    """Copy internal symlinks; never persist a link to a runner-only directory."""
    source = source.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    for path in destination.rglob("*"):
        if not path.is_symlink():
            continue
        resolved = (source / path.relative_to(destination)).resolve()
        if not resolved.is_relative_to(source):
            raise ValueError(f"External toolchain symlink: {path.relative_to(destination)}")
        path.unlink()
        path.symlink_to(os.path.relpath(destination / resolved.relative_to(source), path.parent))


def disable_host_python_pgo(makefile):
    if not makefile.is_file():
        return
    text = makefile.read_text()
    lines = text.splitlines(keepends=True)
    in_host = False
    for index, line in enumerate(lines):
        if re.match(r"^HOST_CONFIGURE_ARGS\s*[+:?]?=", line):
            in_host = True
        elif not line.strip() or not line.startswith((" ", "\t")):
            in_host = False
        if in_host:
            lines[index] = line.replace("--enable-optimizations", "--disable-optimizations")
    updated = "".join(lines)
    if updated != text:
        makefile.write_text(updated)


def verify_host_python_pgo(root):
    directory = root / "feeds/packages/lang/python/python3"
    if not (directory / "Makefile").is_file():
        return
    arguments = output("make", "--no-print-directory", "-s", "-C", directory,
                       "-f", "Makefile", "-f", "-", f"TOPDIR={root}", "ci-host-python-args",
                       input=".PHONY: ci-host-python-args\nci-host-python-args:\n\t@printf '%s\\n' '$(HOST_CONFIGURE_ARGS)'\n")
    if "--enable-optimizations" in arguments.split() or "--disable-optimizations" not in arguments.split():
        raise ValueError("Effective host Python configure arguments do not disable PGO")


def llvm_identity():
    directory = Path(output("llvm-config", "--prefix")).resolve()
    tools = {}
    for tool in ("clang", "llc", "opt", "llvm-dis", "llvm-strip"):
        path = directory / "bin" / tool
        if not os.access(path, os.X_OK):
            raise ValueError(f"Missing host LLVM tool: {path}")
        tools[tool] = file_hash(path)
    return {"path": str(directory), "version": output("llvm-config", "--version"), "executables": tools}


def configure_bpf(root, identity):
    """Exercise the upstream BPF pipeline before selecting a single host LLVM."""
    tools = Path(identity["path"]) / "bin"
    with tempfile.TemporaryDirectory(prefix="wrt-bpf-") as temporary:
        directory = Path(temporary)
        source = directory / "smoke.c"
        source.write_text("int wrt_bpf_smoke(void) { return 0; }\n")
        commands = [
            ("clang", "--target=bpfel", "-O2", "-emit-llvm", "-c", source, "-o", directory / "smoke.bc"),
            ("opt", "-O2", "-mtriple=bpfel", directory / "smoke.bc", "-o", directory / "smoke.opt"),
            ("llvm-dis", directory / "smoke.opt", "-o", directory / "smoke.ll"),
            ("llc", "-march=bpfel", "-mcpu=v3", "-filetype=obj", directory / "smoke.ll", "-o", directory / "smoke.o"),
            ("llvm-strip", "--strip-debug", directory / "smoke.o"),
        ]
        for tool, *arguments in commands:
            subprocess.run([str(tools / tool), *map(str, arguments)], check=True)
    path = root / ".config"
    text = re.sub(r"(?m)^(?:CONFIG_BPF_TOOLCHAIN_.*|# CONFIG_BPF_TOOLCHAIN_.* is not set)\n?", "", path.read_text())
    path.write_text(text.rstrip() + '\nCONFIG_BPF_TOOLCHAIN_HOST=y\n'
                    + f'CONFIG_BPF_TOOLCHAIN_HOST_PATH="{identity["path"]}"\n')


def host_identity():
    environment = dict(os.environ, GOTOOLCHAIN="local")
    go = Path(output("go", "env", "GOROOT", env=environment)) / "bin/go"
    return {"go": {"version": output(go, "env", "GOVERSION", env=environment), "sha256": file_hash(go)},
            "llvm": llvm_identity()}


def rust_requirements(root):
    makefile = root / "feeds/packages/lang/rust/Makefile"
    version = re.search(r"(?m)^PKG_VERSION\s*:?=\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", makefile.read_text())
    if not version:
        raise ValueError("Cannot determine the feed's pinned Rust version")
    target = output("make", "--no-print-directory", "-s", "-f", "-", f"TOPDIR={root}", "ci-rust-target",
                    cwd=root, input="""include $(TOPDIR)/rules.mk
include $(TOPDIR)/feeds/packages/lang/rust/rust-values.mk
.PHONY: ci-rust-target
ci-rust-target:
\t@printf '%s\\n' '$(RUSTC_TARGET_ARCH)'
""")
    if not re.fullmatch(r"[a-z0-9_]+(?:-[a-z0-9_]+)+", target):
        raise ValueError("Invalid feed Rust target triple")
    return version[1], target


def configure_go(root, relative):
    path = root / ".config"
    text = re.sub(r"(?m)^(?:CONFIG_GOLANG_(?:BUILD_BOOTSTRAP|EXTERNAL_BOOTSTRAP_ROOT)=.*|# CONFIG_GOLANG_BUILD_BOOTSTRAP is not set)\n?", "", path.read_text())
    path.write_text(text.rstrip() + '\n# CONFIG_GOLANG_BUILD_BOOTSTRAP is not set\n'
                    + f'CONFIG_GOLANG_EXTERNAL_BOOTSTRAP_ROOT="{root / relative}"\n')


def configure_rust(root, relative):
    directory = root / "feeds/packages/lang/rust"
    (directory / "ci-prebuilt.mk").write_text(
        f"CI_RUST_ROOT:=$(TOPDIR)/{relative}\n" + (PROJECT / "scripts/rust-prebuilt.mk").read_text())
    makefile = directory / "Makefile"
    text = re.sub(r"(?m)^include \./ci-prebuilt\.mk\n?", "", makefile.read_text())
    marker = "$(eval $(call HostBuild))"
    if text.count(marker) != 1:
        raise ValueError("Unexpected Rust host-build rules")
    makefile.write_text(text.replace(marker, "include ./ci-prebuilt.mk\n" + marker))


def validate_bundle(root, manifest):
    if manifest.get("version") != 1:
        raise ValueError("Unsupported portable toolchain manifest")
    version, target = rust_requirements(root)
    if (manifest["rust"]["version"], manifest["rust"]["target"]) != (version, target):
        raise ValueError("Bundled Rust no longer matches the feed or target")
    for name in ("go", "rust"):
        required = {"go"} if name == "go" else {"cargo", "rustc", "rustdoc"}
        if set(manifest[name]["executables"]) != required:
            raise ValueError("Incomplete compiler checksums")
        relative = Path(manifest[name]["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("host-toolchains",):
            raise ValueError("Toolchain path is outside the baseline")
        directory = (root / relative).resolve()
        if not directory.is_relative_to(root / "host-toolchains"):
            raise ValueError("Toolchain escaped the baseline")
        for executable, checksum in manifest[name]["executables"].items():
            path = directory / "bin" / executable
            if path.is_symlink() and not path.resolve().is_relative_to(directory):
                raise ValueError("External compiler link")
            if not os.access(path, os.X_OK) or file_hash(path) != checksum:
                raise ValueError(f"Bundled compiler missing or corrupt: {path}")
    std = root / manifest["rust"]["path"] / "lib/rustlib" / target / "lib"
    if not std.is_dir() or not any(std.glob("libstd-*.rlib")):
        raise ValueError("Bundled Rust target standard library is missing")


def prepare(root, reuse=False):
    root = root.resolve()
    manifest_path = root / "host-toolchains/manifest.json"
    llvm = llvm_identity()
    if reuse:
        manifest = json.loads(manifest_path.read_text())
    else:
        go_env = dict(os.environ, GOTOOLCHAIN="local")
        go_source = Path(output("go", "env", "GOROOT", env=go_env)).resolve()
        go_version = output(go_source / "bin/go", "env", "GOVERSION", env=go_env)
        if not re.fullmatch(r"go[0-9]+\.[0-9]+(?:\.[0-9]+)?", go_version):
            raise ValueError("A stable runner Go compiler is required")
        rust_version, target = rust_requirements(root)
        go_relative = f"host-toolchains/go/{go_version}"
        rust_relative = f"host-toolchains/rust/{rust_version}"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if manifest["go"]["version"] != go_version:
                raise ValueError("Native cache has a different Go bootstrap identity")
            validate_bundle(root, manifest)
        else:
            subprocess.run(["rustup", "toolchain", "install", rust_version, "--profile", "minimal",
                            "--component", "rust-src", "--target", target], check=True)
            rust_source = Path(output("rustup", "run", rust_version, "rustc", "--print", "sysroot"))
            copy_portable(go_source, root / go_relative)
            copy_portable(rust_source, root / rust_relative)
            manifest = {"version": 1,
                        "go": {"version": go_version, "path": go_relative,
                               "executables": {"go": file_hash(root / go_relative / "bin/go")}},
                        "rust": {"version": rust_version, "path": rust_relative, "target": target,
                                 "executables": {name: file_hash(root / rust_relative / "bin" / name)
                                                 for name in ("cargo", "rustc", "rustdoc")}}}
            manifest["llvm"] = llvm
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    if manifest.get("llvm") != llvm:
        raise ValueError("Host LLVM differs from the cached baseline; a full build with a fresh cache is required")
    validate_bundle(root, manifest)
    configure_go(root, manifest["go"]["path"])
    configure_rust(root, manifest["rust"]["path"])
    disable_host_python_pgo(root / "feeds/packages/lang/python/python3/Makefile")
    verify_host_python_pgo(root)
    configure_bpf(root, llvm)
    report = Path(os.environ.get("GITHUB_WORKSPACE", root)) / "build-reports"
    report.mkdir(parents=True, exist_ok=True)
    (report / "host-toolchains.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Portable Go {manifest['go']['version']}; Rust {manifest['rust']['version']} ({manifest['rust']['target']}); host LLVM {llvm['version']}; host Python PGO checked")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--identity", action="store_true", help="Print Go/LLVM identity for the native-tool cache key")
    arguments = parser.parse_args()
    if arguments.identity:
        print(json.dumps(host_identity(), sort_keys=True))
    else:
        prepare(arguments.root, arguments.reuse)
