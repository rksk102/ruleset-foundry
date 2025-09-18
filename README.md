# Merged Rules Index

- Build date: 2025-09-18
- Build time: 2025-09-18 18:28:31 CST
- Repo: rksk102/rules-Repository
- Ref: main
- CDN: jsdelivr

本索引仅针对 merged-rules/。根目录为合并产物（建议优先引用），子目录中为未参与任何合并的镜像原文件（保留原始的 policy/type/owner 结构）。

## 1) 合并产物（merged-rules 根目录，推荐引用）

| File | Behavior | URL |
|---|---|---|
| all-adblock.txt | classical | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/all-adblock.txt |
| direct-all.txt | classical | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct-all.txt |
| direct-cnip.txt | ipcidr | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct-cnip.txt |

示例（mihomo rule-providers）：
```yaml
# 将 <URL> 替换为上表对应链接
rule-providers:
  Merged-Domain:
    type: http
    behavior: domain
    format: text
    url: <URL>
    interval: 86400

  Merged-IPCidr:
    type: http
    behavior: ipcidr
    format: text
    url: <URL>
    interval: 86400

  Merged-Classical:
    type: http
    behavior: classical
    format: text
    url: <URL>
    interval: 86400
```

## 2) 未合并的镜像原文件（merged-rules/<policy>/<type>/<owner>/...）

| Policy | Type | Owner | File | URL |
|---|---|---|---|---|
| block | domain | Loyalsoldier | reject.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/block/domain/Loyalsoldier/reject.txt |
| block | domain | Loyalsoldier | win-extra.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/block/domain/Loyalsoldier/win-extra.txt |
| block | domain | Loyalsoldier | win-spy.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/block/domain/Loyalsoldier/win-spy.txt |
| direct | domain | Loyalsoldier | apple-cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/domain/Loyalsoldier/apple-cn.txt |
| direct | domain | Loyalsoldier | china-list.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/domain/Loyalsoldier/china-list.txt |
| direct | domain | Loyalsoldier | private.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/domain/Loyalsoldier/private.txt |
| direct | domain | MetaCubeX | geolocation-cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/domain/MetaCubeX/geolocation-cn.txt |
| direct | ipcidr | Loyalsoldier | lancidr.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/ipcidr/Loyalsoldier/lancidr.txt |
| proxy | domain | Loyalsoldier | gfw.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/proxy/domain/Loyalsoldier/gfw.txt |
| proxy | domain | Loyalsoldier | proxy-list.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/proxy/domain/Loyalsoldier/proxy-list.txt |
| proxy | domain | Loyalsoldier | proxy.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/proxy/domain/Loyalsoldier/proxy.txt |
| proxy | domain | Loyalsoldier | telegramcidr.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/proxy/domain/Loyalsoldier/telegramcidr.txt |
| proxy | domain | Loyalsoldier | tld-not-cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/proxy/domain/Loyalsoldier/tld-not-cn.txt |
| proxy | domain | gh-proxy.com | category-ai-!cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/proxy/domain/gh-proxy.com/category-ai-!cn.txt |

示例（mihomo rule-providers，按路径中的 type 选择 behavior）：
```yaml
# type=domain -> behavior: domain
# type=ipcidr -> behavior: ipcidr
# type=classical -> behavior: classical
rule-providers:
  Example-From-Mirrored:
    type: http
    behavior: domain   # 替换为对应类型
    format: text
    url: <URL>
    interval: 86400
```

---
_This README is auto-generated from merged-rules. Do not edit manually._
