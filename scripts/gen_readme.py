"""
阶段④ README 生成器 — 扫描 merged-rules + merged-rules-mrs 生成美化版 README。

主要改进：
- 统一日志模块
"""
import os
import sys
import time
import urllib.parse

from logger import info, success, error, group_start, group_end, get_logger
from config_loader import get

logger = get_logger()

REPO_ROOT = os.getcwd()
DIR_RULES_wb = os.path.join(REPO_ROOT, "merged-rules")
DIR_RULES_MRS = os.path.join(REPO_ROOT, "merged-rules-mrs")
README_FILE = os.path.join(REPO_ROOT, "README.md")
REPO_NAME = os.getenv("GITHUB_REPOSITORY", "Owner/Repo")
BRANCH_NAME = os.getenv("GITHUB_REF_NAME", "main")
BASE_RAW = f"https://raw.githubusercontent.com/{REPO_NAME}/{BRANCH_NAME}"
BASE_GHPROXY = f"https://ghproxy.net/{BASE_RAW}"
BASE_JSDELIVR = f"https://cdn.jsdelivr.net/gh/{REPO_NAME}@{BRANCH_NAME}"
SHIELDS_STYLE = "flat-square"

HEADER_NAME = "File (Category / Name)" + "&nbsp;" * 35
HEADER_DL = "Fast Download (CDN)" + "&nbsp;" * 25
HEADER_SRC = "Source" + "&nbsp;" * 10


def format_size(size_bytes):
    """格式化文件大小"""
    if size_bytes == 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB")
    i = 0
    p = size_bytes
    while p >= 1024 and i < len(units) - 1:
        p /= 1024
        i += 1
    return f"{p:.2f} {units[i]}"


def get_time_badge():
    """生成更新时间徽章 (URL safe)"""
    now = time.strftime("%Y--%m--%d %H:%M")
    enc_now = urllib.parse.quote(now)
    return f"https://img.shields.io/badge/Updated-{enc_now}-blue?style={SHIELDS_STYLE}&logo=github"


def scan_files(target_dir):
    """通用：扫描指定目录并排序"""
    files_list = []
    if not os.path.exists(target_dir):
        return []
    for root, _, files in os.walk(target_dir):
        for file in files:
            if not file.startswith("."):
                files_list.append(os.path.join(root, file))
    return sorted(files_list)


def generate_table_rows(files, root_dir, f_handle):
    """通用：生成表格行数据"""
    if not files:
        f_handle.write("| ❌ 没有找到文件 | - | - | - |\n")
        return 0

    count = 0
    for filepath in files:
        filename = os.path.basename(filepath)
        filesize = format_size(os.path.getsize(filepath))
        rel_path = os.path.relpath(filepath, root_dir)
        url_path = rel_path.replace(os.sep, "/")
        root_name = os.path.basename(root_dir)
        category = os.path.dirname(url_path)
        if not category:
            category = "Root"
        full_rel_path = f"{root_name}/{url_path}"
        link_ghproxy = f"{BASE_GHPROXY}/{full_rel_path}"
        link_jsd = f"{BASE_JSDELIVR}/{full_rel_path}"
        link_raw = f"{BASE_RAW}/{full_rel_path}"
        name_column = f"<sub>📂 {category}</sub><br>**{filename}**"
        badge_color = "009688"
        cdn_column = (
            f'<a href="{link_ghproxy}"><img src="https://img.shields.io/badge/🚀_GhProxy-{badge_color}?style={SHIELDS_STYLE}&logo=rocket" alt="GhProxy"></a> '
            f'<a href="{link_jsd}"><img src="https://img.shields.io/badge/⚡_jsDelivr-E34F26?style={SHIELDS_STYLE}&logo=jsdelivr" alt="jsDelivr"></a>'
        )
        src_column = f'<a href="{link_raw}"><img src="https://img.shields.io/badge/Raw_Source-181717?style={SHIELDS_STYLE}&logo=github" alt="GitHub Raw"></a>'
        f_handle.write(
            f"| {name_column} | `{filesize}` | {cdn_column} | {src_column} |\n"
        )
        count += 1
    return count


PAGE_HEADER = f"""<div align="center">

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
  <strong>🚀 全自动构建</strong> · <strong>🌍 全球 CDN 加速</strong> · <strong>📦 每日同步更新</strong>
</p>

</div>

---

### 📖 使用说明 (Usage)

<div class="markdown-alert markdown-alert-tip">
<p class="markdown-alert-title">Tip</p>
<p>推荐优先使用 <strong>GhProxy</strong> 通道，可显著提升国内网络环境下的下载速度。</p>
<p><strong>通用引用链接模板：</strong> <code>https://ghproxy.net/{BASE_RAW}/[文件夹]/{{分类}}/{{文件名}}</code></p>
</div>

"""

TABLE_HEADER = f"""
| {HEADER_NAME} | Size | {HEADER_DL} | {HEADER_SRC} |
| :--- | :--- | :--- | :--- |
"""

FOOTER_TEMPLATE = """
<div align="center">
<br>
<p><sub><strong>Total Files:</strong> {total_count}</sub></p>
<p><sub>Powered by <a href="https://github.com/actions">GitHub Actions</a></sub></p>
</div>
"""


def main():
    group_start("✨ 生成 README")

    files_std = scan_files(DIR_RULES_wb)
    files_mrs = scan_files(DIR_RULES_MRS)

    info(f"  标准规则文件: {len(files_std)}")
    info(f"  MRS 规则文件: {len(files_mrs)}")

    total_files = 0

    try:
        with open(README_FILE, "w", encoding="utf-8") as f:
            f.write(PAGE_HEADER)

            f.write("### 📥 基础规则集合 (Standard Rules)\n")
            f.write(
                '<div class="markdown-alert markdown-alert-note">'
                '<p class="markdown-alert-title">Note</p>'
                '<p>适用于 Clash Premium, Clash Verge, Sing-box 等通用格式。</p></div>\n\n'
            )
            f.write(TABLE_HEADER)
            count_std = generate_table_rows(files_std, DIR_RULES_wb, f)
            total_files += count_std
            f.write("\n<br>\n\n")

            f.write("### 🧩 Mihomo 专用集合 (Binary/MRS)\n")
            f.write(
                '<div class="markdown-alert markdown-alert-important">'
                '<p class="markdown-alert-title">Important</p>'
                '<p>仅适用于 <strong>Mihomo (Clash.Meta)</strong> 内核，性能更好，加载更快。</p></div>\n\n'
            )
            f.write(TABLE_HEADER)
            count_mrs = generate_table_rows(files_mrs, DIR_RULES_MRS, f)
            total_files += count_mrs

            f.write(FOOTER_TEMPLATE.format(total_count=total_files))

    except Exception as e:
        error(f"生成 README 失败: {e}")
        sys.exit(1)

    group_end()
    success(f"✅ README.md 已更新 (Std: {count_std}, MRS: {count_mrs}, 总计: {total_files})")


if __name__ == "__main__":
    main()
