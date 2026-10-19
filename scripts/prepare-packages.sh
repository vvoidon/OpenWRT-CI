#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 VIKINGYFY
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/common.sh"

UPDATE_PACKAGE() {
	local name=$1 repo=$2 branch=$3 aliases=${4:-}
	local directory="./package/${repo#*/}" match
	local -a names=("$name") extra=()
	read -r -a extra <<< "$aliases"
	names+=("${extra[@]}")

	# Leave the feed copy in place until its replacement is fetched successfully.
	checkout_locked "$repo" "$branch" package "$directory"
	for name in "${names[@]}"; do
		while IFS= read -r -d '' match; do
			rm -rf -- "$match"
			echo "Replaced feed package: $match"
		done < <(find ./feeds/luci ./feeds/packages -maxdepth 3 \
			-type d -iname "*$name*" -print0)
	done
}

# These declarations also define the external source lock inputs.
UPDATE_PACKAGE "aurora" "eamonxg/luci-theme-aurora" "master"
UPDATE_PACKAGE "aurora-config" "eamonxg/luci-app-aurora-config" "master"
UPDATE_PACKAGE "diskmanager" "4IceG/luci-app-mini-diskmanager" "main"
UPDATE_PACKAGE "viking" "VIKINGYFY/packages" "main" "axonhub gecoosac sing-box luci-app-homeproxy luci-app-timewol luci-app-wolplus luci-app-wolultra"
UPDATE_PACKAGE "partexp" "sirpdboy/luci-app-partexp" "main"
UPDATE_PACKAGE "natmapt" "muink/openwrt-natmapt" "master"
UPDATE_PACKAGE "stuntman" "muink/openwrt-stuntman" "master"
UPDATE_PACKAGE "luci-app-natmapt" "muink/luci-app-natmapt" "master"
