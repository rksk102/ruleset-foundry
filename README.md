# Merged Rules Index

- Build date: 2025-09-19
- Build time: 2025-09-19 14:48:50 CST
- Repo: rksk102/rules-Repository
- Ref: main
- CDN: jsdelivr

本索引仅针对 merged-rules/。根目录为合并产物（建议优先引用），子目录中为未参与任何合并的镜像原文件（保留原始的 policy/type/owner 结构）。

## 1) 合并产物（merged-rules 根目录，推荐引用）

_No merged files at merged-rules/ root_

## 2) 未合并的镜像原文件（merged-rules/<policy>/<type>/<owner>/...）

| Policy | Type | Owner | File | URL |
|---|---|---|---|---|
| block | domain | Loyalsoldier | reject.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/block/domain/Loyalsoldier/reject.txt |
| block | domain | Loyalsoldier | win-extra.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/block/domain/Loyalsoldier/win-extra.txt |
| block | domain | Loyalsoldier | win-spy.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/block/domain/Loyalsoldier/win-spy.txt |
| block | domain | rksk102 | all-adblock.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/block/domain/rksk102/all-adblock.txt |
| direct | domain | Loyalsoldier | apple-cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/domain/Loyalsoldier/apple-cn.txt |
| direct | domain | Loyalsoldier | china-list.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/domain/Loyalsoldier/china-list.txt |
| direct | domain | Loyalsoldier | direct-list.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/domain/Loyalsoldier/direct-list.txt |
| direct | domain | Loyalsoldier | private.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/domain/Loyalsoldier/private.txt |
| direct | domain | MetaCubeX | geolocation-cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/domain/MetaCubeX/geolocation-cn.txt |
| direct | domain | github.com | microsoft-cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/domain/github.com/microsoft-cn.txt |
| direct | ipcidr | Loyalsoldier | lancidr.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/ipcidr/Loyalsoldier/lancidr.txt |
| direct | ipcidr | rksk102 | cnip.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/direct/ipcidr/rksk102/cnip.txt |
| proxy | domain | Loyalsoldier | gfw.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/proxy/domain/Loyalsoldier/gfw.txt |
| proxy | domain | Loyalsoldier | tld-not-cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/proxy/domain/Loyalsoldier/tld-not-cn.txt |
| proxy | domain | gh-proxy.com | category-ai-!cn.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/proxy/domain/gh-proxy.com/category-ai-!cn.txt |
| proxy | domain | rksk102 | all-proxy.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/proxy/domain/rksk102/all-proxy.txt |
| proxy | ipcidr | Loyalsoldier | telegramcidr.txt | https://cdn.jsdelivr.net/gh/rksk102/rules-Repository@main/merged-rules/proxy/ipcidr/Loyalsoldier/telegramcidr.txt |

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
