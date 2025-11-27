#!/usr/bin/env python3
import os
import sys
import time
import urllib.parse

# =================================================
# 1. 配置区域
# =================================================
REPO_ROOT = os.getcwd()
MERGED_DIR = os.path.join(REPO_ROOT, "merged-rules")
README_FILE = os.path.join(REPO_ROOT, "README.md")

REPO_NAME = os.getenv("GITHUB_REPOSITORY", "Owner/Repo")
BRANCH_NAME = os.getenv("GITHUB_REF_NAME", "main")

# URL 构建
BASE_RAW = f"https://raw.githubusercontent.com/{REPO_NAME}/{BRANCH_NAME}"
BASE_GHPROXY = f"https://ghproxy.net/{BASE_RAW}"
BASE_JSDELIVR = f"https://cdn.jsdelivr.net/gh/{REPO_NAME}@{BRANCH_NAME}"

# 样式配置
SHIELDS_STYLE = "flat-square"

# =================================================
# 2. 辅助函数
# =================================================

def format_size(size_bytes):
    if size_bytes == 0: return "0 B"
    units = ("B", "KB", "MB", "GB")
    i = 0
    p = size_bytes
    while p >= 1024 and i < len(units) - 1:
        p /= 1024
        i += 1
    return f"{p:.2f} {units[i]}"

def get_time_badge():
    now = time.strftime("%Y--%m--%d %H:%M")
    enc_now = urllib.parse.quote(now)
    return f"https://img.shields.io/badge/Updated-{enc_now}-blue?style={SHIELDS_STYLE}&logo=github"

def scan_files():
    files_list = []
    if not os.path.exists(MERGED_DIR):
        return []
    for root, _, files in os.walk(MERGED_DIR):
        for file in files:
            if not file.startswith("."): # 忽略隐藏文件
                files_list.append(os.path.join(root, file))
    return sorted(files_list)

# =================================================
# 3. 模板设计
# =================================================

HEADER = f"""<div align="center">

<h1>📂 {REPO_NAME.split('/')[-1]}</h1>

<p>
  <a href="https://github.com/{REPO_NAME}/actions">
    <img src="https://img.shields.io/github/actions/workflow/status/{REPO_NAME}/sync-rules.yml?style={SHIELDS_STYLE}&label=Build&color=2ea44f" alt="Build">
  </a>
  <a href="https://github.com/{REPO_NAME}">
    <img src="https://img.shields.io/github/repo-size/{REPO_NAME}?style={SHIELDS_STYLE}&label=Size&color=orange" alt="Size">
  </a>
  <a href="#">
    <img src="{get_time_badge()}" alt="Updated">
  </a>
</p>

<p>
  <strong>🚀 每日全自动同步构建</strong> · <strong>🌍 多线路 CDN 加速</strong> · <strong>📦 标准化目录分类</strong>
</p>

</div>

---

### 📖 使用说明 (Usage)

<div class="markdown-alert markdown-alert-tip">
<p class="markdown-alert-title">Tip</p>
<p>推荐使用 <strong>GhProxy</strong> 加速通道，对于中国大陆地区网络更友好。</p>
<p><strong>基础链接模板：</strong> <code>https://ghproxy.net/{BASE_RAW}/merged-rules/{{分类}}/{{文件名}}</code></p>
</div>

### 📥 文件列表 (Files)

<div class="markdown-alert markdown-alert-note">
<p class="markdown-alert-title">Note</p>
<p>点击表格中的 <img src="https://img.shields.io/badge/🚀_CDN-009688?style=flat-square" height="14"> 徽章即可快速下载。</p>
</div>

| File (Category / Name) | Size | Fast Download (CDN) | Source |
| :--- | :--- | :--- | :--- |
"""

FOOTER = """
<div align="center">
<br>
<p><sub><strong>Total Files:</strong> {count}</sub></p>
<p><sub>Powered by <a href="https://github.com/actions">GitHub Actions</a></sub></p>
</div>
"""

# =================================================
# 4. 生成逻辑
# =================================================

def main():
    print("::group::✨ Beautifying README...")
    
    files = scan_files()
    
    try:
        with open(README_FILE, 'w', encoding='utf-8') as f:
            f.write(HEADER)
            
            if not files:
                f.write("| ❌ No files found | - | - | - |\n")
            else:
                for filepath in files:
                    filename = os.path.basename(filepath)
                    filesize = format_size(os.path.getsize(filepath))
                    
                    # 路径处理
                    rel_path = os.path.relpath(filepath, MERGED_DIR)
                    # 生成 URL (强制 /)
                    url_path = rel_path.replace(os.sep, '/')
                    
                    # 提取目录作为分类标签
                    category = os.path.dirname(url_path)
                    if not category: category = "Root"
                    
                    # 链接
                    link_ghproxy = f"{BASE_GHPROXY}/merged-rules/{url_path}"
                    link_jsd = f"{BASE_JSDELIVR}/merged-rules/{url_path}"
                    link_raw = f"{BASE_RAW}/merged-rules/{url_path}"
                    
                    # 这里的表格设计：
                    # 第一列：用灰色小字显示目录，下面加粗显示文件名，视觉层次分明
                    # 第三列：放置显眼的 🚀 按钮
                    # 第四列：放置不起眼的 Raw 链接
                    row = (
                        f"| <sub>📂 {category}</sub><br>**{filename}** | "
                        f"`{filesize}` | "
                        f'<a href="{link_ghproxy}"><img src="https://img.shields.io/badge/🚀_GhProxy-009688?style={SHIELDS_STYLE}&logo=rocket" alt="GhProxy"></a> '
                        f'<a href="{link_jsd}"><img src="https://img.shields.io/badge/⚡_jsDelivr-E34F26?style={SHIELDS_STYLE}&logo=jsdelivr" alt="jsDelivr"></a> | '
                        f"[Raw]({link_raw}) |\n"
                    )
                    f.write(row)

            f.write(FOOTER.format(count=len(files)))
    
    except Exception as e:
        print(f"::error::Error: {e}")
        sys.exit(1)
        
    print("::endgroup::")
    print("✅ README.md updated successfully.")

if __name__ == "__main__":
    main()
