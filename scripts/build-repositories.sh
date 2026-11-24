#!/usr/bin/env bash

set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

OPENWRT_ROOT=${1:-$(pwd)}
PAGES_ROOT=${2:-$OPENWRT_ROOT/pages}
# 后续 make 和索引生成会切换目录，先按调用位置解析所有路径。
OPENWRT_ROOT=$(realpath -e -- "$OPENWRT_ROOT")
PAGES_ROOT=$(realpath -m -- "$PAGES_ROOT")
: "${WRT_TARGET:?WRT_TARGET is required}"
: "${WRT_SUBTARGET:?WRT_SUBTARGET is required}"
: "${WRT_APK_REPO:?WRT_APK_REPO is required}"
: "${SOURCE_FINGERPRINT:?SOURCE_FINGERPRINT is required}"
WRT_ARCH=$(sed -n 's/^CONFIG_TARGET_ARCH_PACKAGES="\([^"]*\)"/\1/p' "$OPENWRT_ROOT/.config")
[[ $WRT_ARCH =~ ^[a-zA-Z0-9_-]+$ ]] || { echo "Invalid package architecture" >&2; exit 1; }

PACKAGE_SOURCE="$OPENWRT_ROOT/bin/targets/$WRT_TARGET/$WRT_SUBTARGET/packages"
APK_BIN="$OPENWRT_ROOT/staging_dir/host/bin/apk"
PRIVATE_KEY="$OPENWRT_ROOT/private-key.pem"

[[ -d $PACKAGE_SOURCE ]] || { echo "Missing package directory: $PACKAGE_SOURCE" >&2; exit 1; }
[[ -x $APK_BIN ]] || { echo "Missing host apk tool: $APK_BIN" >&2; exit 1; }
[[ -s $PRIVATE_KEY ]] || { echo "Missing APK signing key: $PRIVATE_KEY" >&2; exit 1; }
[[ -s $OPENWRT_ROOT/public-key.pem ]] || { echo "Missing APK public key" >&2; exit 1; }

# The built kernel APK is authoritative; do not recalculate ABI from mutable
# source configuration after the build.
shopt -s nullglob
kernel_packages=("$PACKAGE_SOURCE"/kernel-*.apk)
[[ ${#kernel_packages[@]} -eq 1 ]] || { echo "Expected one kernel APK" >&2; exit 1; }
kernel_version=$("$APK_BIN" adbdump --format json "${kernel_packages[0]}" | jq -er '.info.version')
[[ $kernel_version =~ ^([0-9.]+)~([0-9a-f]+)-r([0-9]+)$ ]] || {
	echo "Unexpected kernel APK version: $kernel_version" >&2
	exit 1
}
LINUX_VERSION=${BASH_REMATCH[1]}
LINUX_VERMAGIC=${BASH_REMATCH[2]}
LINUX_RELEASE=${BASH_REMATCH[3]}
KMOD_ABI="$LINUX_VERSION-$LINUX_RELEASE-$LINUX_VERMAGIC"
[[ $KMOD_ABI =~ ^[0-9.]+-[0-9]+-[0-9a-f]+$ ]] || { echo "Invalid kernel ABI" >&2; exit 1; }

TARGET_REPO="$PAGES_ROOT/snapshots/targets/$WRT_TARGET/$WRT_SUBTARGET/packages"
KMOD_REPO="$PAGES_ROOT/snapshots/targets/$WRT_TARGET/$WRT_SUBTARGET/kmods/$KMOD_ABI"
CUSTOM_REPO="$PAGES_ROOT/snapshots/packages/$WRT_ARCH/custom"
[[ ! -e $PAGES_ROOT ]] || { echo "Output must be a fresh directory: $PAGES_ROOT" >&2; exit 1; }
mkdir -p "$TARGET_REPO" "$KMOD_REPO" "$CUSTOM_REPO"
KEYS_DIR=$(mktemp -d)
trap 'rm -rf -- "$KEYS_DIR"' EXIT
cp "$OPENWRT_ROOT/public-key.pem" "$KEYS_DIR/"

find "$PACKAGE_SOURCE" -maxdepth 1 -type f -name 'kmod-*.apk' \
	-exec cp -f {} "$KMOD_REPO/" \;
find "$PACKAGE_SOURCE" -maxdepth 1 -type f -name '*.apk' ! -name 'kmod-*.apk' \
	-exec cp -f {} "$TARGET_REPO/" \;

# Stage built packages; repository_policy removes those that the enabled,
# signature-verified official repositories can safely supply.
while IFS= read -r -d '' package; do
	base=$(basename "$package")
	[[ -f $TARGET_REPO/$base || -f $KMOD_REPO/$base ]] && continue
	if [[ -f $CUSTOM_REPO/$base ]]; then
		cmp "$package" "$CUSTOM_REPO/$base"
	else
		cp "$package" "$CUSTOM_REPO/"
	fi
done < <(find -L "$OPENWRT_ROOT/bin/packages/$WRT_ARCH" -type f -name '*.apk' -print0)

build_index() {
	local repository=$1
	(
		cd "$repository"
		shopt -s nullglob
		local packages=(./*.apk)
		if ((${#packages[@]} == 0)); then
			echo "No APK packages in $repository" >&2
			exit 1
		fi
		"$APK_BIN" mkndx \
			--root "$OPENWRT_ROOT" \
			--keys-dir "$KEYS_DIR" \
			--sign-key "$PRIVATE_KEY" \
			--output packages.adb \
			"${packages[@]}"
		"$APK_BIN" adbdump --format json packages.adb >/dev/null
		"$APK_BIN" verify --keys-dir "$KEYS_DIR" packages.adb "${packages[@]}"
	)
}

build_index "$TARGET_REPO"
build_index "$KMOD_REPO"
build_index "$CUSTOM_REPO"

firmware_roots=("$OPENWRT_ROOT"/build_dir/target-*/root-"$WRT_TARGET")
[[ ${#firmware_roots[@]} -eq 1 && -d ${firmware_roots[0]} ]] || {
	echo "Expected one built firmware filesystem" >&2
	exit 1
}
python3 "$SCRIPT_DIR/repository_policy.py" "$OPENWRT_ROOT" "$PAGES_ROOT" "${firmware_roots[0]}"

cp -f "$OPENWRT_ROOT/public-key.pem" "$PAGES_ROOT/public-key.pem"
touch "$PAGES_ROOT/.nojekyll"

target_count=$(find "$TARGET_REPO" -maxdepth 1 -type f -name '*.apk' | wc -l)
kmod_count=$(find "$KMOD_REPO" -maxdepth 1 -type f -name '*.apk' | wc -l)
custom_count=$(find "$CUSTOM_REPO" -maxdepth 1 -type f -name '*.apk' | wc -l)
pages_size=$(du -sb "$PAGES_ROOT" | awk '{ print $1 }')

if ((pages_size >= 1000000000)); then
	echo "Pages repository is too large: $pages_size bytes" >&2
	exit 1
fi

cat > "$PAGES_ROOT/index.html" <<EOF
<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>京东云 RE-CS-08 APK 软件源</title>
<h1>京东云 RE-CS-08 APK 软件源</h1>
<p>此处仅托管官方源缺失或兼容性检查未通过的软件包，并非完整软件源镜像；请同时保留固件配置的官方源。</p>
<p>仅保留最新构建的软件源；旧固件的软件包可用性不作保证，内核模块必须匹配内核 ABI。</p>
<p>插件更新可单独发布到软件源，不会自动升级路由器上的预装插件；固件基线和发布模式见 <a href="repositories.json">构建元数据</a>。</p>
<p>内核 ABI：<code>$KMOD_ABI</code></p>
<p>构建输入指纹：<code>$SOURCE_FINGERPRINT</code></p>
<ul>
  <li><a href="$WRT_APK_REPO/targets/$WRT_TARGET/$WRT_SUBTARGET/packages/packages.adb">平台软件包索引（$target_count 个包）</a></li>
  <li><a href="$WRT_APK_REPO/targets/$WRT_TARGET/$WRT_SUBTARGET/kmods/$KMOD_ABI/packages.adb">内核模块索引（$kmod_count 个包）</a></li>
  <li><a href="$WRT_APK_REPO/packages/$WRT_ARCH/custom/packages.adb">补充软件包索引（$custom_count 个包）</a></li>
</ul>
<p>兼容性检查通过的软件包优先使用官方源。
<a href="repository-policy.json">查看官方包复用结果及兼容性例外</a></p>
</html>
EOF

python3 - "$PAGES_ROOT" "$WRT_APK_REPO" "$SOURCE_FINGERPRINT" "$KMOD_ABI" <<'PY'
import hashlib
import json
import sys
from pathlib import Path
root, base, fingerprint, abi = sys.argv[1:]
root = Path(root)
indices = []
for path in sorted(root.rglob("packages.adb")):
    relative = path.relative_to(root).as_posix()
    indices.append({"path": relative, "url": base.removesuffix("/snapshots") + "/" + relative,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
data = {"fingerprint": fingerprint, "kernel_abi": abi, "repositories": indices,
        "public_key_sha256": hashlib.sha256((root / "public-key.pem").read_bytes()).hexdigest(),
        "repository_policy_sha256":
            hashlib.sha256((root / "repository-policy.json").read_bytes()).hexdigest()}
(root / "repositories.json").write_text(json.dumps(data, indent=2) + "\n")
PY

if [[ -n ${GITHUB_ENV:-} ]]; then
	{
		echo "WRT_ABI=$KMOD_ABI"
		echo "WRT_TARGET_PACKAGE_COUNT=$target_count"
		echo "WRT_KMOD_PACKAGE_COUNT=$kmod_count"
		echo "WRT_CUSTOM_PACKAGE_COUNT=$custom_count"
		echo "WRT_ARCH=$WRT_ARCH"
	} >> "$GITHUB_ENV"
fi

echo "Target repository: $target_count packages"
echo "KMOD repository: $kmod_count packages ($KMOD_ABI)"
echo "Custom repository: $custom_count packages"
echo "Pages size: $pages_size bytes"
