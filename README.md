# 京东云 RE-CS-08 固件

基于 [VIKINGYFY/OpenWRT-CI](https://github.com/VIKINGYFY/OpenWRT-CI)，使用 ImmortalWrt `owrt` 分支，提供 RE-CS-08 固件和配套 APK 软件源。

[固件下载](https://github.com/vvoidon/OpenWRT-CI/releases) · [软件源](https://vvoidon.github.io/OpenWRT-CI/)

默认管理地址：`192.168.10.1`，默认无密码，首次登录后请设置密码。

## 使用

1. 配置 Actions Secret `APK_SIGNING_KEY`，内容为固定 EC 私钥的 PEM 文本，勿将私钥提交到仓库。
2. 将仓库 Pages 的 Source 设置为 **GitHub Actions**。
3. 在 **Actions → JDCloud RE-CS-08 → Run workflow** 启动构建。`force=true` 强制完整构建，`false` 按源码变化决定是否构建。

每天北京时间 05:21 自动检查更新，GitHub 调度可能延迟。用户态包符合条件时增量更新软件源，失败则回退完整构建。可选软件源包失败时跳过，固件必需包和发布检查仍须成功；跳过清单见 Release 附件 `package-failures.json`。

软件源更新不会自动更新设备上的包。软件源与固件仅保留最新版本，旧固件的软件包可用性不作保证，kmod 必须匹配内核 ABI。`build-baseline*` 仅供 CI 使用，不是刷机文件。

## 配置

- `config/re-cs-08.config`：设备配置
- `config/re-cs-08-kernel.config`：内核配置
- `config/packages.config`：软件包选择
- `scripts/prepare-packages.sh`：外部插件来源

本地检查：`python3 -B -m unittest discover -s tests -v`。构建日志和失败详情见 Actions 及其报告附件。
