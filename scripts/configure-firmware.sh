#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 VIKINGYFY
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(dirname "$SCRIPT_DIR")

# Board-only configuration; no kernel/driver source patches.
[[ ${WRT_CONFIG:?} == re-cs-08 ]]
KERNEL_CONFIG=target/linux/qualcommbe/ipq53xx/config-default
test -f "$KERNEL_CONFIG"
KERNEL_CONFIG_TMP=$(mktemp "${KERNEL_CONFIG}.XXXXXX")
./scripts/kconfig.pl '+' "$KERNEL_CONFIG" "$PROJECT_DIR/config/re-cs-08-kernel.config" > "$KERNEL_CONFIG_TMP"
chmod --reference="$KERNEL_CONFIG" "$KERNEL_CONFIG_TMP"
mv "$KERNEL_CONFIG_TMP" "$KERNEL_CONFIG"

#移除luci-app-attendedsysupgrade
find ./feeds/luci/collections/ -type f -name "Makefile" -exec sed -i "/attendedsysupgrade/d" {} +
#修改默认主题
find ./feeds/luci/collections/ -type f -name "Makefile" -exec sed -i "s/luci-theme-bootstrap/luci-theme-$WRT_THEME/g" {} +
#修改immortalwrt.lan关联IP
find ./feeds/luci/modules/luci-mod-system/ -type f -name "flash.js" -exec sed -i "s/192\.168\.[0-9]*\.[0-9]*/$WRT_IP/g" {} +
#添加编译日期标识
find ./feeds/luci/modules/luci-mod-status/ -type f -name "10_system.js" -exec sed -i "s/(\(luciversion || ''\))/(\1) + (' \/ $WRT_MARK-$WRT_DATE')/g" {} +

CFG_FILE="./package/base-files/files/bin/config_generate"
#修改默认IP地址
sed -i "s/192\.168\.[0-9]*\.[0-9]*/$WRT_IP/g" $CFG_FILE
#修改默认主机名
sed -i "s/hostname='.*'/hostname='$WRT_NAME'/g" $CFG_FILE

# 合并设备、软件包及 LuCI 默认配置
{
	echo "CONFIG_PACKAGE_luci=y"
	echo "CONFIG_LUCI_LANG_zh_Hans=y"
	echo "CONFIG_PACKAGE_luci-theme-$WRT_THEME=y"
	echo "CONFIG_PACKAGE_luci-app-$WRT_THEME-config=y"
} >> ./.config

# 将 target 软件源替换为本项目 Pages，并添加匹配 ABI 的 kmods 源和补充源；保留官方用户态源。
APK_FEEDS_FILE="./include/feeds.mk"
TARGET_FEED="${WRT_APK_REPO%/}/targets/%S/packages/packages.adb"
KMOD_FEED="${WRT_APK_REPO%/}/targets/%S/kmods/\$(LINUX_VERSION)-\$(LINUX_RELEASE)-\$(LINUX_VERMAGIC)/packages.adb"
CUSTOM_FEED="${WRT_APK_REPO%/}/packages/%A/custom/packages.adb"

sed -i "s|%U/targets/%S/packages/packages.adb|$TARGET_FEED|" "$APK_FEEDS_FILE"
if ! grep -Fq "$KMOD_FEED" "$APK_FEEDS_FILE"; then
	APK_FEEDS_TMP=$(mktemp "${APK_FEEDS_FILE}.XXXXXX")
	APK_FEED_INSERTED=false
	while IFS= read -r LINE || [ -n "$LINE" ]; do
		printf '%s\n' "$LINE" >> "$APK_FEEDS_TMP"
		if [[ "$LINE" == *"$TARGET_FEED"* ]]; then
			printf "  echo '%s'; %s\n" "$KMOD_FEED" "\\" >> "$APK_FEEDS_TMP"
			printf "  echo '%s'; %s\n" "$CUSTOM_FEED" "\\" >> "$APK_FEEDS_TMP"
			APK_FEED_INSERTED=true
		fi
	done < "$APK_FEEDS_FILE"
	if [[ "$APK_FEED_INSERTED" != true ]]; then
		rm -f "$APK_FEEDS_TMP"
		echo "target APK feed entry not found" >&2
		exit 1
	fi
	chmod --reference="$APK_FEEDS_FILE" "$APK_FEEDS_TMP"
	mv -f "$APK_FEEDS_TMP" "$APK_FEEDS_FILE"
fi

test "$(grep -Fc "$TARGET_FEED" "$APK_FEEDS_FILE")" -eq 1
test "$(grep -Fc "$KMOD_FEED" "$APK_FEEDS_FILE")" -eq 1
test "$(grep -Fc "$CUSTOM_FEED" "$APK_FEEDS_FILE")" -eq 1
