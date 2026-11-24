#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Based on VIKINGYFY/OpenWRT-CI's Ubuntu build dependencies.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get -yqq update
apt-get install -y --no-install-recommends \
  bc bison build-essential bzip2 ca-certificates ccache clang cmake cpio curl \
  device-tree-compiler file flex g++-multilib gawk gcc-multilib genisoimage gettext \
  git jq libbpf-dev libclang-dev libdw-dev libelf-dev libfuse-dev libncurses-dev \
  libssl-dev lld llvm ninja-build patch perl pkgconf python3 python3-cryptography \
  python3-dev python3-packaging python3-pyelftools python3-setuptools qemu-utils \
  rsync swig unzip wget xz-utils zlib1g-dev zstd
