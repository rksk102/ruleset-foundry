<div align="center">

<h1>ruleset-foundry</h1>

<p><strong>从上游开源规则自动合并、去重并构建为 mihomo 规则集（.txt / .mrs）</strong></p>

<p><sub>每日两次自动构建（北京时间清晨与傍晚，具体时间以 Actions 运行记录为准） · 按 <code>block &gt; direct &gt; policy</code> 策略分组</sub></p>

<p>
  <a href="https://github.com/rksk102/ruleset-foundry/actions">
    <img src="https://img.shields.io/github/actions/workflow/status/rksk102/ruleset-foundry/pipeline.yml?style=flat-square&label=Build" alt="Build status">
  </a>
  <a href="https://github.com/rksk102/ruleset-foundry/releases">
    <img src="https://img.shields.io/badge/%E4%BA%A7%E7%89%A9%E7%94%9F%E6%88%90-2026--10--31%2018%3A24-blue?style=flat-square&logo=github" alt="产物生成时间">
  </a>
</p>

</div>

## 快速开始

推荐使用 `.mrs` 二进制规则集（加载快、内存占用低）；`.txt` 文本规则集同样提供，见下方产物清单。
把以下内容并入 mihomo 配置即可使用（规则书写顺序即匹配优先级 `block > direct > policy`）：

<details>
<summary><b>完整配置示例</b>（点击展开）</summary>

```yaml
rule-providers:
  block:
    type: http
    behavior: domain
    format: mrs
    url: "https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/block/domain/rksk102/all-adblock.mrs"
    path: ./ruleset/block.mrs
    interval: 86400
  direct:
    type: http
    behavior: ipcidr
    format: mrs
    url: "https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/direct/ipcidr/rksk102/all-cnip.mrs"
    path: ./ruleset/direct.mrs
    interval: 86400
  policy:
    type: http
    behavior: domain
    format: mrs
    url: "https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/policy/domain/rksk102/all-proxy.mrs"
    path: ./ruleset/policy.mrs
    interval: 86400

rules:
  - RULE-SET,block,REJECT
  - RULE-SET,direct,DIRECT
  - RULE-SET,policy,PROXY
```

</details>

> [!NOTE]
> 以上链接走 jsDelivr CDN，内容更新最长滞后 **12 小时**。需要最新内容时，把 URL 中的
> `cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts` 换成
> `raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts`（约 5 分钟缓存）。

## 产物清单

共 **42** 个产物文件，合计 **26.41 MB**（.txt 18.75 MB + .mrs 7.66 MB）。引用时必须声明正确的 `behavior` 和 `format`，否则规则可能被静默解析为空：`.txt` 使用 `format: text`，`.mrs` 使用 `format: mrs`；domain 产物使用 `behavior: domain`，IP 产物使用 `behavior: ipcidr`。

| 产物 | `behavior` | `format` |
| :--- | :--- | :--- |
| `merged-rules/<策略>/domain/**/*.txt` | `domain` | `text` |
| `merged-rules/<策略>/ipcidr/**/*.txt` | `ipcidr` | `text` |
| `merged-rules-mrs/**/*.mrs` | 与同名 `.txt` 相同 | `mrs` |

### 基础规则集合（.txt）

*mihomo 文本规则集*

<details>
<summary><b>21 个文件</b> | 总大小 <b>18.75 MB</b></summary>

| 文件名称 | 大小 | 下载 | GitHub raw |
| :--- | :--- | :--- | :--- |
| <sub>block/domain/DustinWin</sub><br><b>ads.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `2.03 MB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/block/domain/DustinWin/ads.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download ads.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/block/domain/DustinWin/ads.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw ads.txt"></a> |
| <sub>block/domain/Loyalsoldier</sub><br><b>reject-list.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `4.26 MB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/block/domain/Loyalsoldier/reject-list.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download reject-list.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/block/domain/Loyalsoldier/reject-list.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw reject-list.txt"></a> |
| <sub>block/domain/Loyalsoldier</sub><br><b>reject.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `4.26 MB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/block/domain/Loyalsoldier/reject.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download reject.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/block/domain/Loyalsoldier/reject.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw reject.txt"></a> |
| <sub>block/domain/Loyalsoldier</sub><br><b>win-extra.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `11.67 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/block/domain/Loyalsoldier/win-extra.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download win-extra.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/block/domain/Loyalsoldier/win-extra.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw win-extra.txt"></a> |
| <sub>block/domain/Loyalsoldier</sub><br><b>win-spy.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `9.61 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/block/domain/Loyalsoldier/win-spy.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download win-spy.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/block/domain/Loyalsoldier/win-spy.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw win-spy.txt"></a> |
| <sub>block/domain/MetaCubeX</sub><br><b>category-ads-all.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `17.25 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/block/domain/MetaCubeX/category-ads-all.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download category-ads-all.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/block/domain/MetaCubeX/category-ads-all.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw category-ads-all.txt"></a> |
| <sub>block/domain/rksk102</sub><br><b>all-adblock.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `4.70 MB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/block/domain/rksk102/all-adblock.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download all-adblock.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/block/domain/rksk102/all-adblock.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw all-adblock.txt"></a> |
| <sub>direct/domain/DustinWin</sub><br><b>microsoft-cn.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `4.48 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/direct/domain/DustinWin/microsoft-cn.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download microsoft-cn.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/direct/domain/DustinWin/microsoft-cn.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw microsoft-cn.txt"></a> |
| <sub>direct/domain/Loyalsoldier</sub><br><b>direct-list.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `1.54 MB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/direct/domain/Loyalsoldier/direct-list.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download direct-list.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/direct/domain/Loyalsoldier/direct-list.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw direct-list.txt"></a> |
| <sub>direct/domain/Loyalsoldier</sub><br><b>private.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `3.00 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/direct/domain/Loyalsoldier/private.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download private.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/direct/domain/Loyalsoldier/private.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw private.txt"></a> |
| <sub>direct/domain/MetaCubeX</sub><br><b>geolocation-cn.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `75.79 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/direct/domain/MetaCubeX/geolocation-cn.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download geolocation-cn.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/direct/domain/MetaCubeX/geolocation-cn.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw geolocation-cn.txt"></a> |
| <sub>direct/ipcidr/Loyalsoldier</sub><br><b>cncidr.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `155.91 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/direct/ipcidr/Loyalsoldier/cncidr.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download cncidr.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/direct/ipcidr/Loyalsoldier/cncidr.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw cncidr.txt"></a> |
| <sub>direct/ipcidr/Loyalsoldier</sub><br><b>lancidr.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `688.00 B` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/direct/ipcidr/Loyalsoldier/lancidr.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download lancidr.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/direct/ipcidr/Loyalsoldier/lancidr.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw lancidr.txt"></a> |
| <sub>direct/ipcidr/MetaCubeX</sub><br><b>cn.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `155.90 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/direct/ipcidr/MetaCubeX/cn.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download cn.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/direct/ipcidr/MetaCubeX/cn.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw cn.txt"></a> |
| <sub>direct/ipcidr/rksk102</sub><br><b>all-cnip.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `155.95 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/direct/ipcidr/rksk102/all-cnip.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download all-cnip.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/direct/ipcidr/rksk102/all-cnip.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw all-cnip.txt"></a> |
| <sub>policy/domain/Loyalsoldier</sub><br><b>proxy-list.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `430.17 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/policy/domain/Loyalsoldier/proxy-list.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download proxy-list.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/policy/domain/Loyalsoldier/proxy-list.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw proxy-list.txt"></a> |
| <sub>policy/domain/Loyalsoldier</sub><br><b>proxy.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `430.16 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/policy/domain/Loyalsoldier/proxy.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download proxy.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/policy/domain/Loyalsoldier/proxy.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw proxy.txt"></a> |
| <sub>policy/domain/Loyalsoldier__clash-rules</sub><br><b>gfw.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `66.91 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/policy/domain/Loyalsoldier__clash-rules/gfw.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download gfw.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/policy/domain/Loyalsoldier__clash-rules/gfw.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw gfw.txt"></a> |
| <sub>policy/domain/Loyalsoldier__v2ray-rules-dat</sub><br><b>gfw.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `66.92 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/policy/domain/Loyalsoldier__v2ray-rules-dat/gfw.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download gfw.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/policy/domain/Loyalsoldier__v2ray-rules-dat/gfw.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw gfw.txt"></a> |
| <sub>policy/domain/MetaCubeX</sub><br><b>category-ai-!cn.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `4.34 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/policy/domain/MetaCubeX/category-ai-!cn.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download category-ai-!cn.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/policy/domain/MetaCubeX/category-ai-!cn.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw category-ai-!cn.txt"></a> |
| <sub>policy/domain/rksk102</sub><br><b>all-proxy.txt</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `430.21 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules/policy/domain/rksk102/all-proxy.txt"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download all-proxy.txt"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules/policy/domain/rksk102/all-proxy.txt"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw all-proxy.txt"></a> |

</details>

### Mihomo 专用集合（.mrs）

*mihomo 二进制规则集*

<details>
<summary><b>21 个文件</b> | 总大小 <b>7.66 MB</b></summary>

| 文件名称 | 大小 | 下载 | GitHub raw |
| :--- | :--- | :--- | :--- |
| <sub>block/domain/DustinWin</sub><br><b>ads.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `844.66 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/block/domain/DustinWin/ads.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download ads.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/block/domain/DustinWin/ads.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw ads.mrs"></a> |
| <sub>block/domain/Loyalsoldier</sub><br><b>reject-list.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `1.79 MB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/block/domain/Loyalsoldier/reject-list.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download reject-list.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/block/domain/Loyalsoldier/reject-list.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw reject-list.mrs"></a> |
| <sub>block/domain/Loyalsoldier</sub><br><b>reject.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `1.79 MB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/block/domain/Loyalsoldier/reject.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download reject.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/block/domain/Loyalsoldier/reject.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw reject.mrs"></a> |
| <sub>block/domain/Loyalsoldier</sub><br><b>win-extra.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `3.41 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/block/domain/Loyalsoldier/win-extra.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download win-extra.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/block/domain/Loyalsoldier/win-extra.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw win-extra.mrs"></a> |
| <sub>block/domain/Loyalsoldier</sub><br><b>win-spy.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `2.60 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/block/domain/Loyalsoldier/win-spy.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download win-spy.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/block/domain/Loyalsoldier/win-spy.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw win-spy.mrs"></a> |
| <sub>block/domain/MetaCubeX</sub><br><b>category-ads-all.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `7.97 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/block/domain/MetaCubeX/category-ads-all.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download category-ads-all.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/block/domain/MetaCubeX/category-ads-all.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw category-ads-all.mrs"></a> |
| <sub>block/domain/rksk102</sub><br><b>all-adblock.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `1.96 MB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/block/domain/rksk102/all-adblock.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download all-adblock.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/block/domain/rksk102/all-adblock.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw all-adblock.mrs"></a> |
| <sub>direct/domain/DustinWin</sub><br><b>microsoft-cn.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `1.94 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/direct/domain/DustinWin/microsoft-cn.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download microsoft-cn.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/direct/domain/DustinWin/microsoft-cn.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw microsoft-cn.mrs"></a> |
| <sub>direct/domain/Loyalsoldier</sub><br><b>direct-list.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `517.87 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/direct/domain/Loyalsoldier/direct-list.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download direct-list.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/direct/domain/Loyalsoldier/direct-list.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw direct-list.mrs"></a> |
| <sub>direct/domain/Loyalsoldier</sub><br><b>private.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `781.00 B` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/direct/domain/Loyalsoldier/private.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download private.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/direct/domain/Loyalsoldier/private.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw private.mrs"></a> |
| <sub>direct/domain/MetaCubeX</sub><br><b>geolocation-cn.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `33.47 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/direct/domain/MetaCubeX/geolocation-cn.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download geolocation-cn.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/direct/domain/MetaCubeX/geolocation-cn.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw geolocation-cn.mrs"></a> |
| <sub>direct/ipcidr/Loyalsoldier</sub><br><b>cncidr.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `37.67 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/direct/ipcidr/Loyalsoldier/cncidr.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download cncidr.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/direct/ipcidr/Loyalsoldier/cncidr.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw cncidr.mrs"></a> |
| <sub>direct/ipcidr/Loyalsoldier</sub><br><b>lancidr.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `236.00 B` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/direct/ipcidr/Loyalsoldier/lancidr.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download lancidr.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/direct/ipcidr/Loyalsoldier/lancidr.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw lancidr.mrs"></a> |
| <sub>direct/ipcidr/MetaCubeX</sub><br><b>cn.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `37.67 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/direct/ipcidr/MetaCubeX/cn.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download cn.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/direct/ipcidr/MetaCubeX/cn.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw cn.mrs"></a> |
| <sub>direct/ipcidr/rksk102</sub><br><b>all-cnip.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `37.67 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/direct/ipcidr/rksk102/all-cnip.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download all-cnip.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/direct/ipcidr/rksk102/all-cnip.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw all-cnip.mrs"></a> |
| <sub>policy/domain/Loyalsoldier</sub><br><b>proxy-list.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `189.31 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/policy/domain/Loyalsoldier/proxy-list.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download proxy-list.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/policy/domain/Loyalsoldier/proxy-list.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw proxy-list.mrs"></a> |
| <sub>policy/domain/Loyalsoldier</sub><br><b>proxy.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `189.31 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/policy/domain/Loyalsoldier/proxy.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download proxy.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/policy/domain/Loyalsoldier/proxy.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw proxy.mrs"></a> |
| <sub>policy/domain/Loyalsoldier__clash-rules</sub><br><b>gfw.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `33.91 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/policy/domain/Loyalsoldier__clash-rules/gfw.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download gfw.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/policy/domain/Loyalsoldier__clash-rules/gfw.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw gfw.mrs"></a> |
| <sub>policy/domain/Loyalsoldier__v2ray-rules-dat</sub><br><b>gfw.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `33.91 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/policy/domain/Loyalsoldier__v2ray-rules-dat/gfw.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download gfw.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/policy/domain/Loyalsoldier__v2ray-rules-dat/gfw.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw gfw.mrs"></a> |
| <sub>policy/domain/MetaCubeX</sub><br><b>category-ai-!cn.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `2.23 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/policy/domain/MetaCubeX/category-ai-!cn.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download category-ai-!cn.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/policy/domain/MetaCubeX/category-ai-!cn.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw category-ai-!cn.mrs"></a> |
| <sub>policy/domain/rksk102</sub><br><b>all-proxy.mrs</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | `189.31 KB` | <a href="https://cdn.jsdelivr.net/gh/rksk102/ruleset-foundry@artifacts/merged-rules-mrs/policy/domain/rksk102/all-proxy.mrs"><img src="https://img.shields.io/badge/Download-34D399?style=flat-square" alt="Download all-proxy.mrs"></a> | <a href="https://raw.githubusercontent.com/rksk102/ruleset-foundry/artifacts/merged-rules-mrs/policy/domain/rksk102/all-proxy.mrs"><img src="https://img.shields.io/badge/Source-181717?style=flat-square&logo=github" alt="GitHub raw all-proxy.mrs"></a> |

</details>


<details>
<summary><b>规则语义：domain 写法对照</b>（点击展开）</summary>

策略顺序固定为 `block > direct > policy`。`domain` 规则中的写法含义如下：

| 写法 | `example.com` | `www.example.com` | `a.b.example.com` |
| :--- | :---: | :---: | :---: |
| `+.example.com` | ✅ | ✅ | ✅ |
| `.example.com` | ❌ | ✅ | ✅ |
| `example.com` | ✅ | ❌ | ❌ |
| `*.example.com` | ❌ | ✅ | ❌ |
| `*.*.example.com` | ❌ | ❌ | ✅ |

</details>

## 版本与完整性

> [!WARNING]
> 产物每次以孤立提交**强制推送**到 `artifacts` 分支，历史会被重写，**不适合固定 commit**。
> 需要固定版本或整包下载请使用 [Releases](https://github.com/rksk102/ruleset-foundry/releases)
> （含 `merged-rules-*.zip` 与 `SHA256SUMS`；校验前先解压 zip，再执行 `sha256sum -c SHA256SUMS`）。

Releases 仅适合**短期固定**：默认只保留最近 30 天，过期的 Release 与 tag 会在**后续成功发布**
的收尾阶段清理（不是独立的每日定时任务）；需要长期使用时请跟随
[`latest`](https://github.com/rksk102/ruleset-foundry/releases/latest) 或自行镜像产物。

## 安全与许可

脚本与工作流以 MIT 许可发布；规则内容来自上游项目，使用前请自行确认其许可与准确性。
漏洞请通过 [SECURITY.md](SECURITY.md) 的私密入口报告。

---

<sub>本文件由 `scripts/gen_readme.py` 自动生成，请勿直接编辑。</sub>
