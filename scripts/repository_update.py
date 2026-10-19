#!/usr/bin/env python3
"""Verify and unpack the immutable released firmware for repository-only validation."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile


def unpack_baseline(downloads, output, unsquashfs):
    images = list(downloads.glob("*re-cs-08*sysupgrade*.bin"))
    if len(images) != 1:
        raise ValueError("Expected exactly one released sysupgrade image")
    image = images[0]
    sums = {}
    for line in (downloads / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        sums[name.lstrip("*")] = digest
    if hashlib.sha256(image.read_bytes()).hexdigest() != sums.get(image.name):
        raise ValueError("Released firmware checksum mismatch")
    output.mkdir(parents=True, exist_ok=False)
    with tarfile.open(image) as archive:
        roots = [item for item in archive.getmembers() if item.name.endswith("/root")]
        if len(roots) != 1 or not roots[0].isfile():
            raise ValueError("Malformed released sysupgrade filesystem")
        with (output / "root.squashfs").open("wb") as target:
            shutil.copyfileobj(archive.extractfile(roots[0]), target)
    subprocess.run([str(unsquashfs), "-no-progress", "-d", str(output / "firmware"),
                    str(output / "root.squashfs"), "etc/apk", "lib/apk/db"], check=True)
    return image


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    unpack = subparsers.add_parser("unpack")
    unpack.add_argument("downloads", type=Path)
    unpack.add_argument("output", type=Path)
    unpack.add_argument("unsquashfs", type=Path)
    args = parser.parse_args()
    unpack_baseline(args.downloads, args.output, args.unsquashfs)
