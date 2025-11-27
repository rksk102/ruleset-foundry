#!/usr/bin/env python3
import os
import sys
import time
import urllib.parse

# =================================================
# 配置区域
# =================================================
REPO_ROOT = os.getcwd()
MERGED_DIR = os.path.join(REPO_ROOT, "merged-rules")
README_FILE = os.path.join(REPO_ROOT, "README.md")

# 从环境变量获取仓库信息，默认 fallback 方便本地调试
REPO_NAME = os.getenv("GITHUB_REPOSITORY", "rksk102/singbox-rules")
BRANCH_NAME = os.getenv("GITHUB_REF_NAME", "main")

# 基础链接构建
BASE_RAW = f"https://raw.githubusercontent.com/{REPO_NAME}/{BRANCH_NAME}"
BASE_MIRROR = f"https://raw.gitmirror.com/{REPO_NAME}/{BRANCH_NAME}"
BASE_GHPROXY = f"https://ghproxy.net/{BASE_RAW}"

# Badge 颜色和样式
SHIELDS_STYLE = "flat-square"

# =================================================
# 辅助函数
# =================================================

def format_size(size_bytes):
    """将字节转换为人类可读格式 (KB, MB)"""
    if size_bytes == 0: return "0 B"
    units = ("B", "KB", "MB", "GB")
    i = 0
    p = size_bytes
    while p >= 1024 and i < len(units) - 1:
        p /= 1024
        i += 1
    return f"{p:.2f} {units[i]}"

def get_current_time_str():
    """生成 URL 编码的时间字符串用于 Badge"""
    # 格式: YYYY-MM-DD HH:MM (URL encoded spaces)
    now = time.strftime("%Y-%m-%d %H:%M")
    return urllib.parse.quote(now) # 关键：处理空格为 %20

def scan_rules():
    """扫描 merged-rules 目录并返回排序后的文件列表"""
    rule_files = []
    if not os.path.exists(MERGED_DIR):
        return []
    
    for root, _, files in os.walk(MERGED_DIR):
        for file in files:
            if file.endswith(".txt"): # 假设是 .txt 规则
                full_path = os.path.join(root, file)
                rule_files.append(full_path)
    
    # 按路径排序，保证每次生成顺序一致
    return sorted(rule_files)

# =================================================
# 模板内容
# =================================================

HEADER_TEMPLATE = f"""<div align="center">
<a href="https://github.com/{REPO_NAME}">
<img src="https://sing-box.sagernet.org/assets/icon.svg" width="100" height="100" alt="Sing-box Logo">
</a>

# Sing-box Rule Sets

[![Build Status](https://img.shields.io/github/actions/workflow/status/{REPO_NAME}/sync-rules.yml?style={SHIELDS_STYLE}&logo=github&label=Build)](https://github.com/{REPO_NAME}/actions)
[![Repo Size](https://img.shields.io/github/repo-size/{REPO_NAME}?style={SHIELDS_STYLE}&label=Repo%20Size&color=orange)](https://github.com/{REPO_NAME})
[![Updated](https://img.shields.io/badge/Updated-{get_current_time_str()}-blue?style={SHIELDS_STYLE}&logo=time)](https://github.com/{REPO_NAME}/commits/{BRANCH_NAME})

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
{{
  "route": {{
    "rule_set": [
      {{
        "type": "remote",
        "tag": "geosite-google",
        "format": "source",
        "url": "https://ghproxy.net/{BASE_RAW}/merged-rules/block/domain/example.txt",
        "download_detour": "proxy-out" 
      }}
    ]
  }}
}}
</details>
📥 规则下载 (Downloads)
<div class="markdown-alert markdown-alert-note"> <p class="markdown-alert-title">Note</p> <p>使用 <code>Ctrl + F</code> 可快速查找规则。点击 <code>🚀 Fast Download</code> 按钮可直接复制加速链接。</p> </div>
规则名称 (Name)	类型 (Type)	大小 (Size)	下载通道 (Download)
"""			
FOOTER_TEMPLATE = """

<div align="center"> <br> <p><strong>Total Rule Sets:</strong> <code>{count}</code></p> <p><a href="#">🔼 Back to Top</a></p> <sub>Powered by <a href="https://github.com/actions">GitHub Actions</a></sub> </div> """
=================================================
主逻辑
=================================================
def main():
print("::group::📝 Generating README with Python...")

files = scan_rules()
print(f"::notice::Found {len(files)} rule files.")

with open(README_FILE, 'w', encoding='utf-8') as f:
    # 1. 写入头部
    f.write(HEADER_TEMPLATE)
    
    # 2. 遍历并写入每一行
    if not files:
        f.write("| ❌ Error | No rules found | - | - |\n")
    else:
        for filepath in files:
            filename = os.path.basename(filepath)
            filesize = os.path.getsize(filepath)
            human_size = format_size(filesize)
            
            # 计算相对路径: merged-rules/block/domain/Loyalsoldier/reject.txt
            # rel path mainly used for URLs
            rel_path = os.path.relpath(filepath, REPO_ROOT)
            # path inside merged-rules for display
            display_path_full = os.path.relpath(filepath, MERGED_DIR)
            
            # 解析路径结构：block/domain/Loyalsoldier/reject.txt
            # parts = ['block', 'domain', 'Loyalsoldier', 'reject.txt']
            parts = display_path_full.split(os.sep)
            
            if len(parts) >= 3:
                policy = parts[0]
                rule_type = parts[1] # domain or ipcidr
                owner = parts[2]
                # 目录展示: 📂 rulesets/block/domain/Loyalsoldier /
                dir_display = f"📂 merged-rules/{os.path.dirname(display_path_full)} /"
            else:
                rule_type = "unknown"
                dir_display = f"📂 {os.path.dirname(display_path_full)}"

            # 构建链接
            # 必须保证是正斜杠 / 即使在 Windows 上
            url_rel_path = rel_path.replace(os.sep, '/')
            
            link_raw = f"{BASE_RAW}/{url_rel_path}"
            link_ghproxy = f"{BASE_GHPROXY}/{url_rel_path}"
            link_mirror = f"{BASE_MIRROR}/{url_rel_path}"
            
            # 漂亮的表格行
            row = (
                f"| <sub>{dir_display}</sub><br>**{filename}** | "
                f"`{rule_type}` | "
                f"`{human_size}` | "
                f'<a href="{link_ghproxy}"><img src="https://img.shields.io/badge/🚀_Fast_Download-GhProxy-009688?style={SHIELDS_STYLE}&logo=rocket" alt="Fast Download"></a><br>'
                f"[CDN Mirror]({link_mirror}) • [Raw Source]({link_raw}) |\n"
            )
            f.write(row)

    # 3. 写入页脚
    f.write(FOOTER_TEMPLATE.format(count=len(files)))

print("::endgroup::")
print("✅ README.md created successfully.")
if name == "main":
main()
