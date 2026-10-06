# 安全策略

## 项目做什么、执行什么

`mihomo-rule-library` 是一条 CI 流水线：从公开上游拉取规则列表，清洗合并后发布为
`.txt` / `.mrs` 规则集（`artifacts` 分支与 GitHub Releases）。

需要明确的两条执行/信任事实：

1. **流水线会下载并执行 mihomo 内核二进制。** `scripts/convert_mrs.py` 从
   `MetaCubeX/mihomo` 的 Release 下载 `config.yaml` 中 `mihomo.asset_name` 指定的资产，
   校验解压后二进制的 SHA-256 是否等于 `mihomo.kernel_sha256`（缺失该字段时**拒绝**降级使用），
   随后 `chmod +x` 并在 GitHub Actions 的 `ubuntu-latest` runner 上执行，用于把 `.txt` 编译成 `.mrs`。
   该二进制**在运行脚本的机器上执行**：CI 里是临时 runner，本地手动运行
   `scripts/convert_mrs.py`（含 `--print-kernel-hash`、`--bump-config`）时则是你自己的机器。
   校验最多四重（GitHub 资产摘要、解压后 SHA-256、ELF magic、`-v` 可运行性）：上游资产**未提供
   `digest`** 时拒绝下载该资产并回落到已校验的缓存内核（`--bump-config` 直接失败），此时实际生效
   的是后三重；这些命令需要执行 Linux 版内核，请在 Linux/WSL 上运行或交给 CI。
   本地运行前请自行确认你信任该版本。
2. **上游规则列表是不可信数据。** 它们只被当作文本解析为域名/IP 规则，不参与脚本拼接、
   不作为代码执行；但它们的内容会**原样进入发布产物**。若某个上游列表被投毒，
   产物中的规则也会被污染（这属于上游仓库的问题，见下）。

## 信任边界

| 组件 | 是否在本项目范围内 | 说明 |
| :--- | :--- | :--- |
| `scripts/`、`.github/`、`sources.urls`、`config.yaml` | ✅ 在范围内 | 本仓库维护，欢迎报告问题 |
| 发布产物（`.txt` / `.mrs`）的**生成逻辑**与清单完整性 | ✅ 在范围内 | 缺失、串源、语义错误、哈希不一致等 |
| `MetaCubeX/mihomo` 内核本身 | ❌ 上游 | 请报告给 [MetaCubeX/mihomo](https://github.com/MetaCubeX/mihomo/security) |
| 上游规则列表内容（MetaCubeX / Loyalsoldier / DustinWin 等） | ❌ 上游 | 请报告给对应仓库 |
| GitHub Actions 市场中的第三方 action | ❌ 上游 | 本仓库已把全部 action 钉扎到 commit SHA |
| 使用产物的代理客户端（mihomo / Clash 系 GUI 等） | ❌ 不在范围内 | 请报告给对应客户端项目 |

## 报告漏洞

- **首选**：通过本仓库 **Security → Report a vulnerability** 提交私密报告
  （GitHub Private Vulnerability Reporting）。
- 若该入口不可用，请先开一个**不含利用细节**的 Issue，说明需要私下沟通渠道。
- 请尽量包含：受影响的 commit 或 Release 版本、复现步骤、影响面
  （例如产物被污染、CI 权限提升、供应链投毒、密钥泄漏）。
- 我们会在确认后尽快回复（通常 7 天内），并在修复发布后再公开细节。

## 不在范围内（示例）

- 上游规则列表本身的误报/漏报（例如某域名被错误拦截）——请向上游仓库反馈。
- mihomo 内核的漏洞与崩溃。
- 单纯"规则集可以被 MITM 替换"这类不针对本仓库托管渠道的通用网络风险。
- 使用本仓库产物所产生的任何直接或间接后果（见 README 的许可与免责声明）。

## 维护者自查

- 内核哈希：`python scripts/convert_mrs.py --print-kernel-hash` 打印解压后二进制的 SHA-256，
  与上游 Release 资产核对后写入 `config.yaml`。
- 产物完整性：CI 的 `pipeline.yml` 末尾有"校验制品下载完整性"步骤，产物下载失败或哈希不匹配
  会使发布作业失败，不会静默发布残缺产物。
- 依赖与工作流：Dependabot 维护 `pip` 与 `github-actions` 依赖，CI 对 workflow 文件做静态检查；
  所有 action 以完整 commit SHA 钉扎。
