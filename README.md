<div align="center">
<a href="https://github.com/rksk102/rules-Repository">
<img src="https://sing-box.sagernet.org/assets/icon.svg" width="100" height="100" alt="Sing-box Logo">
</a>

# Sing-box Rule Sets

[![Build Status](https://img.shields.io/github/actions/workflow/status/rksk102/rules-Repository/sync-rules.yml?style=flat-square&logo=github&label=Build)](https://github.com/rksk102/rules-Repository/actions)
[![Repo Size](https://img.shields.io/github/repo-size/rksk102/rules-Repository?style=flat-square&label=Repo%20Size&color=orange)](https://github.com/rksk102/rules-Repository)
[![Updated](https://img.shields.io/badge/Updated-2025-11-27%2010%3A02-blue?style=flat-square&logo=time)](https://github.com/rksk102/rules-Repository/commits/main)

<p>
🚀 <strong>全自动构建</strong> · 🌏 <strong>全球 CDN 加速</strong> · 🎯 <strong>精准分类</strong>
</p>
</div>

<table>
<thead>
<tr>
<th align="center">🤖 <strong>Automated</strong></th>
<th align="center">⚡ <strong>High Speed</strong></th>
<th align="center">📦 <strong>Standardized</strong></th>
</tr>
</thead>
<tbody>
<tr>
<td align="center">每日定时同步上游规则<br>自动清洗去重</td>
<td align="center">集成 GhProxy/GitMirror<br>国内环境极速拉取</td>
<td align="center">标准化目录结构<br>适配 Sing-box/Clash</td>
</tr>
</tbody>
</table>

---

## ⚙️ 配置指南 (Setup)

<div class="markdown-alert markdown-alert-tip">
<p class="markdown-alert-title">Tip</p>
<p>推荐优先使用 <strong>GhProxy</strong> 通道，能够显著提升国内拉取速度。</p>
</div>

<details>
<summary><strong>📝 点击展开 <code>config.json</code> (Remote 模式) 配置示例</strong></summary>

```json
{
  "route": {
    "rule_set": [
      {
        "type": "remote",
        "tag": "geosite-google",
        "format": "source",
        "url": "https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/block/domain/example.txt",
        "download_detour": "proxy-out" 
      }
    ]
  }
}

</details>

## 📥 规则下载 (Downloads)

<div class="markdown-alert markdown-alert-note"> 
<p class="markdown-alert-title">Note</p> 
<p>使用 <code>Ctrl + F</code> 可快速查找规则。点击 <code>🚀 Fast Download</code> 按钮可直接复制加速链接。</p> 
</div>

| 规则名称 (Name) | 类型 (Type) | 大小 (Size) | 下载通道 (Download) |
| --- | --- | --- | --- |
| <sub>📂 merged-rules/direct/domain/Loyalsoldier /</sub><br>**apple-cn.txt** | `domain` | `0 B` | <a href="https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/direct/domain/Loyalsoldier/apple-cn.txt"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style=flat-square&logo=rocket" alt="Fast Download"></a><br>[CDN Mirror](https://raw.gitmirror.com/rksk102/rules-Repository/main/merged-rules/direct/domain/Loyalsoldier/apple-cn.txt) • [Raw Source](https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/direct/domain/Loyalsoldier/apple-cn.txt) |
| <sub>📂 merged-rules/direct/domain/Loyalsoldier /</sub><br>**direct-list.txt** | `domain` | `1.39 MB` | <a href="https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/direct/domain/Loyalsoldier/direct-list.txt"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style=flat-square&logo=rocket" alt="Fast Download"></a><br>[CDN Mirror](https://raw.gitmirror.com/rksk102/rules-Repository/main/merged-rules/direct/domain/Loyalsoldier/direct-list.txt) • [Raw Source](https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/direct/domain/Loyalsoldier/direct-list.txt) |
| <sub>📂 merged-rules/direct/domain/Loyalsoldier /</sub><br>**private.txt** | `domain` | `2.40 KB` | <a href="https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/direct/domain/Loyalsoldier/private.txt"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style=flat-square&logo=rocket" alt="Fast Download"></a><br>[CDN Mirror](https://raw.gitmirror.com/rksk102/rules-Repository/main/merged-rules/direct/domain/Loyalsoldier/private.txt) • [Raw Source](https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/direct/domain/Loyalsoldier/private.txt) |
| <sub>📂 merged-rules/direct/ipcidr/Loyalsoldier /</sub><br>**lancidr.txt** | `ipcidr` | `224.00 B` | <a href="https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/direct/ipcidr/Loyalsoldier/lancidr.txt"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style=flat-square&logo=rocket" alt="Fast Download"></a><br>[CDN Mirror](https://raw.gitmirror.com/rksk102/rules-Repository/main/merged-rules/direct/ipcidr/Loyalsoldier/lancidr.txt) • [Raw Source](https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/direct/ipcidr/Loyalsoldier/lancidr.txt) |
| <sub>📂 merged-rules/direct/ipcidr/rksk102 /</sub><br>**all-cnip.txt** | `ipcidr` | `338.67 KB` | <a href="https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/direct/ipcidr/rksk102/all-cnip.txt"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style=flat-square&logo=rocket" alt="Fast Download"></a><br>[CDN Mirror](https://raw.gitmirror.com/rksk102/rules-Repository/main/merged-rules/direct/ipcidr/rksk102/all-cnip.txt) • [Raw Source](https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/direct/ipcidr/rksk102/all-cnip.txt) |
| <sub>📂 merged-rules/policy/domain/Loyalsoldier /</sub><br>**gfw.txt** | `domain` | `78.89 KB` | <a href="https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/policy/domain/Loyalsoldier/gfw.txt"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style=flat-square&logo=rocket" alt="Fast Download"></a><br>[CDN Mirror](https://raw.gitmirror.com/rksk102/rules-Repository/main/merged-rules/policy/domain/Loyalsoldier/gfw.txt) • [Raw Source](https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/policy/domain/Loyalsoldier/gfw.txt) |
| <sub>📂 merged-rules/policy/domain/Loyalsoldier /</sub><br>**proxy-list.txt** | `domain` | `365.54 KB` | <a href="https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/policy/domain/Loyalsoldier/proxy-list.txt"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style=flat-square&logo=rocket" alt="Fast Download"></a><br>[CDN Mirror](https://raw.gitmirror.com/rksk102/rules-Repository/main/merged-rules/policy/domain/Loyalsoldier/proxy-list.txt) • [Raw Source](https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/policy/domain/Loyalsoldier/proxy-list.txt) |
| <sub>📂 merged-rules/policy/domain/Loyalsoldier /</sub><br>**proxy.txt** | `domain` | `380.65 KB` | <a href="https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/policy/domain/Loyalsoldier/proxy.txt"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style=flat-square&logo=rocket" alt="Fast Download"></a><br>[CDN Mirror](https://raw.gitmirror.com/rksk102/rules-Repository/main/merged-rules/policy/domain/Loyalsoldier/proxy.txt) • [Raw Source](https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/policy/domain/Loyalsoldier/proxy.txt) |
| <sub>📂 merged-rules/reject/domain/Loyalsoldier /</sub><br>**reject-list.txt** | `domain` | `2.43 MB` | <a href="https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/reject/domain/Loyalsoldier/reject-list.txt"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style=flat-square&logo=rocket" alt="Fast Download"></a><br>[CDN Mirror](https://raw.gitmirror.com/rksk102/rules-Repository/main/merged-rules/reject/domain/Loyalsoldier/reject-list.txt) • [Raw Source](https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/reject/domain/Loyalsoldier/reject-list.txt) |
| <sub>📂 merged-rules/reject/domain/Loyalsoldier /</sub><br>**reject.txt** | `domain` | `2.43 MB` | <a href="https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/reject/domain/Loyalsoldier/reject.txt"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style=flat-square&logo=rocket" alt="Fast Download"></a><br>[CDN Mirror](https://raw.gitmirror.com/rksk102/rules-Repository/main/merged-rules/reject/domain/Loyalsoldier/reject.txt) • [Raw Source](https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/reject/domain/Loyalsoldier/reject.txt) |
| <sub>📂 merged-rules/reject/domain/Loyalsoldier /</sub><br>**win-extra.txt** | `domain` | `11.75 KB` | <a href="https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/reject/domain/Loyalsoldier/win-extra.txt"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style=flat-square&logo=rocket" alt="Fast Download"></a><br>[CDN Mirror](https://raw.gitmirror.com/rksk102/rules-Repository/main/merged-rules/reject/domain/Loyalsoldier/win-extra.txt) • [Raw Source](https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/reject/domain/Loyalsoldier/win-extra.txt) |
| <sub>📂 merged-rules/reject/domain/Loyalsoldier /</sub><br>**win-spy.txt** | `domain` | `9.15 KB` | <a href="https://ghproxy.net/https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/reject/domain/Loyalsoldier/win-spy.txt"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style=flat-square&logo=rocket" alt="Fast Download"></a><br>[CDN Mirror](https://raw.gitmirror.com/rksk102/rules-Repository/main/merged-rules/reject/domain/Loyalsoldier/win-spy.txt) • [Raw Source](https://raw.githubusercontent.com/rksk102/rules-Repository/main/merged-rules/reject/domain/Loyalsoldier/win-spy.txt) |


<div align="center"> 
<br> 
<p><strong>Total Rule Sets:</strong> <code>12</code></p> 
<p><a href="#">🔼 Back to Top</a></p> 
<sub>Powered by <a href="https://github.com/actions">GitHub Actions</a></sub> 
</div> 
