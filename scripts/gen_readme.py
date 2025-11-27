#!/usr/bin/env python3
import os
import time
import urllib.parse

# =================================================
# 配置参数
# =================================================
REPO_NAME = os.getenv("GITHUB_REPOSITORY", "rksk102/singbox-rules")
BRANCH = os.getenv("GITHUB_REF_NAME", "main")
RULES_DIR = "merged-rules"
README_PATH = "README.md"

# CDN加速前缀
CDN_GHPROXY = "https://ghproxy.net/https://raw.githubusercontent.com"
CDN_JSDELIVR = "https://fastly.jsdelivr.net/gh"
CDN_MIRROR = "https://raw.gitmirror.com"

# 基础 URL
BASE_URL_RAW = f"https://raw.githubusercontent.com/{REPO_NAME}/{BRANCH}"

# =================================================
# 辅助函数
# =================================================

def get_file_size(filepath):
    """将文件大小转换为人类可读格式 (KB, MB)"""
    size = os.path.getsize(filepath)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}".replace(".0 ", " ")
        size /= 1024
    return f"{size:.1f} TB"

def generate_badges():
    """生成顶部的 Shields.io 徽章"""
    date_str = time.strftime("%Y--%m--%d%%20%H:%M")
    badges = [
        f"[![Build Status](https://img.shields.io/github/actions/workflow/status/{REPO_NAME}/sync.yml?style=flat-square&logo=github&label=Build)](https://github.com/{REPO_NAME}/actions)",
        f"[![Repo Size](https://img.shields.io/github/repo-size/{REPO_NAME}?style=flat-square&label=Rules&color=orange)](https://github.com/{REPO_NAME})",
        f"[![Updated](https://img.shields.io/badge/Updated-{date_str}-blue?style=flat-square&logo=time)](https://github.com/{REPO_NAME}/commits/{BRANCH})"
    ]
    return "\n".join(badges)

# =================================================
# 主生成逻辑
# =================================================

def main():
    print(f"::group::📝 Generating README for {REPO_NAME}...")
    
    if not os.path.exists(RULES_DIR):
        print(f"::warning::Directory {RULES_DIR} not found. Skipping.")
        return

    # 1. 收集文件信息
    file_list = []
    for root, _, files in os.walk(RULES_DIR):
        for file in files:
            if not file.endswith(".txt"): continue
            
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, RULES_DIR)
            
            # 路径结构: block/domain/Loyalsoldier/reject.txt
            parts = rel_path.split(os.sep)
            
            # 提取元数据 (防止路径过短报错)
            policy = parts[0] if len(parts) > 0 else "unknown"
            rule_type = parts[1] if len(parts) > 1 else "mixed"
            owner = parts[2] if len(parts) > 2 else "general"
            
            file_info = {
                "name": file,
                "size": get_file_size(full_path),
                "path_display": f"📂 {os.path.dirname(rel_path)} /",
                "rel_path": rel_path, # 用于生成链接
                "type": rule_type.upper(),
                "policy": policy,
                "owner": owner
            }
            file_list.append(file_info)

    # 按名称排序
    file_list.sort(key=lambda x: x["rel_path"])

    # 2. 构建 Markdown 内容
    content = []
    
    # --- Header ---
    content.append(f"""
<div align="center">
<a href="https://github.com/{REPO_NAME}">
<img src="https://sing-box.sagernet.org/assets/icon.svg" width="100" height="100" alt="Logo">
</a>

# Sing-box Rule Sets

{generate_badges()}

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
<td align="center">每日定时同步上游规则<br>自动清洗去重 / IP合并</td>
<td align="center">集成 GhProxy/GitMirror<br>国内环境极速拉取</td>
<td align="center">标准化目录结构<br>可直接用于 Sing-box</td>
</tr>
</tbody>
</table>

---

## ⚙️ 配置指南 (Tips)

<div class="markdown-alert markdown-alert-tip">
<p class="markdown-alert-title">Tip</p>
<p>推荐优先使用 <strong>GhProxy</strong> 通道，能够显著提升国内拉取速度。</p>
</div>

<details>
<summary><strong>📝 点击展开 <code>config.json</code> 配置示例</strong></summary>

```json
{{
  "route": {{
    "rule_set": [
      {{
        "type": "remote",
        "tag": "geosite-google",
        "format": "source",
        "url": "{CDN_GHPROXY}/{REPO_NAME}/{BRANCH}/{RULES_DIR}/block/domain/example.txt",
        "download_detour": "proxy-out" 
      }}
    ]
  }}
}}
