# Rule Sets Index

- Build date: 2025-09-18
- Build time: 2025-09-18 14:27:57 CST
- Repo: rksk102/rules-Repository
- Ref: main
- CDN: jsdelivr

说明：下表列出了每个规则文件的拉取直链，可直接用于 mihomo 的 rule-providers。目录结构为 rulesets/<policy>/<type>/<owner>/<file>。

## Text Rule Sets (rulesets/)

| Policy | Type | Owner | File | URL |
|---|---|---|---|---|
| block | domain | Loyalsoldier | reject-list.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/block/domain/Loyalsoldier/reject-list.txt |
| block | domain | Loyalsoldier | reject.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/block/domain/Loyalsoldier/reject.txt |
| block | domain | Loyalsoldier | win-extra.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/block/domain/Loyalsoldier/win-extra.txt |
| block | domain | Loyalsoldier | win-spy.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/block/domain/Loyalsoldier/win-spy.txt |
| block | domain | github.com | ads.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/block/domain/github.com/ads.txt |
| block | domain | github.com | category-ads-all.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/block/domain/github.com/category-ads-all.txt |
| direct | domain | Loyalsoldier | apple-cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/direct/domain/Loyalsoldier/apple-cn.txt |
| direct | domain | Loyalsoldier | china-list.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/direct/domain/Loyalsoldier/china-list.txt |
| direct | domain | Loyalsoldier | direct-list.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/direct/domain/Loyalsoldier/direct-list.txt |
| direct | domain | Loyalsoldier | direct.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/direct/domain/Loyalsoldier/direct.txt |
| direct | domain | Loyalsoldier | private.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/direct/domain/Loyalsoldier/private.txt |
| direct | domain | MetaCubeX | geolocation-cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/direct/domain/MetaCubeX/geolocation-cn.txt |
| direct | ipcidr | Loyalsoldier | cncidr.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/direct/ipcidr/Loyalsoldier/cncidr.txt |
| direct | ipcidr | Loyalsoldier | lancidr.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/direct/ipcidr/Loyalsoldier/lancidr.txt |
| direct | ipcidr | MetaCubeX | cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/direct/ipcidr/MetaCubeX/cn.txt |
| proxy | domain | Loyalsoldier | gfw.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/proxy/domain/Loyalsoldier/gfw.txt |
| proxy | domain | Loyalsoldier | proxy-list.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/proxy/domain/Loyalsoldier/proxy-list.txt |
| proxy | domain | Loyalsoldier | proxy.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/proxy/domain/Loyalsoldier/proxy.txt |
| proxy | domain | Loyalsoldier | telegramcidr.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/proxy/domain/Loyalsoldier/telegramcidr.txt |
| proxy | domain | Loyalsoldier | tld-not-cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/proxy/domain/Loyalsoldier/tld-not-cn.txt |
| proxy | domain | gh-proxy.com | category-ai-!cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/rulesets/proxy/domain/gh-proxy.com/category-ai-!cn.txt |

示例（mihomo rule-providers）：
```yaml
# 选取表格中的某个 URL 替换 <URL>
rule-providers:
  Example-Domain:
    type: http
    behavior: domain      # 对应 type=domain
    format: text
    url: <URL>
    interval: 86400

  Example-IPCidr:
    type: http
    behavior: ipcidr      # 对应 type=ipcidr
    format: text
    url: <URL>
    interval: 86400

  Example-Classical:
    type: http
    behavior: classical   # 对应 type=classical
    format: text
    url: <URL>
    interval: 86400
```

---
_This README is auto-generated. Do not edit manually._
