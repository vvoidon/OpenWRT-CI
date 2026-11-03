# 京东云 RE-CS-08 固件与 APK 软件源

基于 [VIKINGYFY/OpenWRT-CI](https://github.com/VIKINGYFY/OpenWRT-CI) 的 RE-CS-08 专用构建项目，使用 [VIKINGYFY/immortalwrt](https://github.com/VIKINGYFY/immortalwrt) 的 `owrt` 分支，目标平台为 `qualcommbe/ipq53xx`。

固件下载见本仓库 **Releases**。`factory.bin` 与 `sysupgrade.bin` 的用途不同，请根据设备当前系统选择适用的刷写方式；`build-baseline*` 是 CI 编译基线，不是刷机文件。

默认管理地址为 `192.168.10.1`，主机名为 `OWRT`，默认无登录密码。首次登录后请设置密码。保留配置升级时，以设备已有配置为准。

## 构建与更新

唯一构建工作流为 [JDCloud RE-CS-08](.github/workflows/re-cs-08.yml)。每天北京时间 **05:21** 检查源码变化，GitHub 的定时调度可能延迟或漏触发，不能保证准时执行。推送提交不会立即启动构建。

手动构建：进入 **Actions → JDCloud RE-CS-08 → Run workflow**。

- `force=true`（默认）：完整编译并发布固件和软件源。
- `force=false`：检查相关源码变化；没有变化则结束，符合条件的用户态软件包变化尝试增量构建，其余变化完整构建。

增量构建只更新 APK 软件源，不生成新固件，也不会自动更新路由器上的软件包。工作流从最近固件 Release 恢复编译基线，重建受影响的软件包和反向依赖；基线缺失、工具版本变化、构建或兼容性验证失败时自动退回完整构建。LuCI 定制复用固件基线中的设置和日期标识。

固件、feeds 与外部插件的提交均记录于 `source-state.txt`；`source-coverage.json` 用于识别与本设备相关的变化。宿主 Go、Rust 保存在编译树中，LLVM 使用经过 BPF 编译检查的宿主工具，并校验其身份以避免增量恢复时混用版本。宿主 Python 关闭 PGO，i2c-tools 仅在选择 `python3-smbus` 时编译 Python 绑定。

## 仓库设置

1. 在仓库 **Settings → Actions → General** 启用 Actions，并允许工作流所用的 Actions。
2. 在 **Settings → Secrets and variables → Actions** 添加仓库 Secret `APK_SIGNING_KEY`，内容为持久保存的 EC 私钥 PEM。可在可信环境运行 `openssl ecparam -name prime256v1 -genkey -noout -out private-key.pem` 生成，再将文件内容保存为 Secret。妥善备份，勿提交私钥。
3. 在 **Settings → Pages → Build and deployment** 将 Source 设置为 **GitHub Actions**。若 `github-pages` 环境设有审批规则，部署时需满足相应规则。
4. 首次运行使用 `force=true`，生成固件、签名软件源和增量构建基线。

已安装固件信任构建时的签名公钥；更换私钥后需处理设备上的信任公钥，不能直接把换钥后的软件源当成原源继续使用。

## 软件源与发布策略

自建软件源地址由仓库所有者和仓库名自动生成：`https://<owner>.github.io/<repository>/snapshots`。固件保留官方用户态软件源，自建源提供平台包、匹配内核 ABI 的 kmod 和官方源无法兼容提供的补充包，并非完整镜像。

发布前检查签名、公钥、内核 ABI、预装包与依赖解析；Pages 部署后核对公开索引与构建元数据，再发布新固件。软件源按最新部署覆盖，固件 Release 仅保留最新的 `RE-CS-08-` 发布，其他前缀的 Release 不参与清理。旧固件的软件包可用性不作保证，kmod 必须匹配内核 ABI。

运行失败时查看 Actions 日志及 `full-build-reports-*` / `incremental-build-reports-*` 附件。新增提交后，原有基线会因 CI 输入变化而触发完整构建。

## 配置与验证

| 路径 | 用途 |
| --- | --- |
| `config/re-cs-08.config` | 设备和构建目标 |
| `config/re-cs-08-kernel.config` | 设备内核配置 |
| `config/packages.config` | 预装及仅供软件源安装的软件包 |
| `config/feeds.list` | feed 来源与分支 |
| `scripts/prepare-packages.sh` | 外部插件来源与替换规则 |
| `scripts/` | 源码锁定、构建准备、增量更新及发布验证 |
| `tests/` | 构建自动化回归测试 |

本地验证：`python3 -B -m unittest discover -s tests -v`。测试不替代完整固件编译或实机验证。
