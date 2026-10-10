# 安全策略

## 范围

本项目维护一条 CI 流水线：从公开上游下载规则，清洗合并后发布 `.txt` / `.mrs`。

在范围内：

- `scripts/`、`.github/`、`sources.urls`、`config.yaml`
- 产物生成逻辑、路径约束、清单一致性和发布前校验

不在范围内：

- `MetaCubeX/mihomo` 内核本身
- 上游规则列表的内容准确性或是否被投毒
- 代理客户端行为
- 使用产物造成的直接或间接后果

## 信任模型

- 流水线会下载并执行 mihomo 内核，仅用于把 `.txt` 编译成 `.mrs`。内核按架构选择
  amd64 或 arm64，校验 GitHub asset digest、解压后 SHA-256、ELF 架构和可运行性。
- `kernel-bump.yml` 会自动把新版本及两个架构哈希提交到 `main`，没有 PR 评审。
  如果不接受该模型，请禁用该 workflow 的 commit job。
- 上游规则是不可信文本，不会被当作代码执行，但内容会原样进入发布产物。
- 第三方 GitHub Actions 均按 commit SHA 固定。

## 报告漏洞

请优先通过 GitHub Security 的 **Report a vulnerability** 私密入口提交。
如果该入口不可用，请创建不含利用细节的 Issue 请求私下沟通。

报告请包含受影响的 commit 或 Release、复现步骤和影响面。我们确认后会协调修复与披露。

## 不在范围内

- 上游规则的误报、漏报或内容变更
- mihomo 内核漏洞与崩溃
- 通用网络攻击、第三方镜像或代理服务问题
- 由使用本仓库产物产生的后果
