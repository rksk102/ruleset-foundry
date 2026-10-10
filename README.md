<div align="center">

<h1>mihomo-rule-library</h1>

<p>
  <a href="https://github.com/rksk102/mihomo-rule-library/actions">
    <img src="https://img.shields.io/github/actions/workflow/status/rksk102/mihomo-rule-library/pipeline.yml?style=flat-square&label=Build&color=2ea44f" alt="Build">
  </a>
  <a href="https://github.com/rksk102/mihomo-rule-library">
    <img src="https://img.shields.io/github/repo-size/rksk102/mihomo-rule-library?style=flat-square&label=Size&color=orange" alt="Size">
  </a>
  <a href="#">
    <img src="https://img.shields.io/badge/Updated-2026--10--10%2022%3A08-blue?style=flat-square&logo=github" alt="Updated">
  </a>
</p>

<p><strong>自动生成</strong> &middot; <strong>每日同步</strong></p>

</div>

---

## 规则列表

### 基础规则集合

## 使用方式

`.txt` 是 mihomo 文本规则集，`.mrs` 是对应的二进制规则集。引用时必须声明正确的
`behavior` 和 `format`，否则规则可能被静默解析为空。
`.txt` 使用 `format: text`，`.mrs` 使用 `format: mrs`。
domain 产物使用 `behavior: domain`，IP 产物使用 `behavior: ipcidr`。

| 产物 | `behavior` | `format` |
| :--- | :--- | :--- |
| `merged-rules/<策略>/domain/**/*.txt` | `domain` | `text` |
| `merged-rules/<策略>/ipcidr/**/*.txt` | `ipcidr` | `text` |
| `merged-rules-mrs/**/*.mrs` | 与同名 `.txt` 相同 | `mrs` |

```yaml
rule-providers:
  block:
    type: http
    behavior: domain
    format: text
    url: "https://raw.githubusercontent.com/rksk102/mihomo-rule-library/artifacts/merged-rules/block/domain/Loyalsoldier/reject.txt"
    path: ./ruleset/block.txt
    interval: 86400
```

下载表中的 `Download` 是 jsDelivr 链接，`Source` 是 GitHub raw 链接。产物发布在
`artifacts` 分支，不进入 `main` 的历史。

## 规则语义

策略顺序固定为 `block > direct > policy`。`domain` 规则中的写法含义如下：

| 写法 | `example.com` | `www.example.com` | `a.b.example.com` |
| :--- | :---: | :---: | :---: |
| `+.example.com` | ✅ | ✅ | ✅ |
| `.example.com` | ❌ | ✅ | ✅ |
| `example.com` | ✅ | ❌ | ❌ |
| `*.example.com` | ❌ | ✅ | ❌ |
| `*.*.example.com` | ❌ | ❌ | ✅ |

`sources.urls` 可用 `[policy:...]`、`[type:...]` 和 `[domain-kind:exact|suffix]`
标记来源语义；未显式标记的裸域名按精确匹配处理。

## 配置与维护

`config.yaml` 是运行参数来源，`sources.urls` 是上游来源清单。常用命令：

```bash
python scripts/gen_readme.py
python -m pytest tests/ -q --cov=scripts --cov-branch --cov-fail-under=70
ruff check scripts tests
```

内核由 `mihomo.pinned_version` 和两个架构哈希钉扎；`kernel-bump.yml` 自动更新，
提交前会验证 amd64/arm64 内核。详细信任模型见 [SECURITY.md](SECURITY.md)。

## 安全与许可

脚本和工作流使用 MIT 许可；规则内容来自上游仓库，使用前请自行确认其许可和准确性。
漏洞请通过 GitHub Security 的私密报告入口提交，详见 [SECURITY.md](SECURITY.md)。

`README.md` 由 `scripts/gen_readme.py` 生成，修改说明文字请改 `make_static_sections()`。
