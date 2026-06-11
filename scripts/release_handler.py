"""
阶段⑤ 发布处理器 — 仅在规则内容有实际变化时创建 GitHub Release。

主要改进：
- 变更检测：对比上次发布哈希，无变化则跳过
- 统一日志模块
"""
import os
import sys
import shutil
import subprocess
import json
import datetime
import zipfile
from pathlib import Path

from logger import info, success, warning, error, group_start, group_end, section, get_logger
from config_loader import get
from utils import dir_hash, load_last_hash, save_last_hash

logger = get_logger()

TARGET_CONFIG = {
    "merged-rules": ".txt",
    "merged-rules-mrs": ".mrs",
}
KEEP_DAYS = get("behavior", "release_keep_days", default=3)
CHANGE_DETECTION = get("behavior", "release_change_detection", default=True)


def run_gh(cmd_list):
    """调用 GitHub CLI"""
    try:
        result = subprocess.run(["gh"] + cmd_list, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        warning(f"  GH CLI 警告: {e.stderr.strip()}")
        return None


def zip_target_files(tag_date):
    """压缩文件并返回 (zip_name, manifest)"""
    zip_name = f"merged-rules-{tag_date}.zip"
    info(f"📦 打包文件到 {zip_name}...")

    file_manifest = {}
    total_files = 0

    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zipf:
        for folder, ext in TARGET_CONFIG.items():
            if not os.path.exists(folder):
                warning(f"  目录 '{folder}' 不存在，跳过")
                continue

            file_manifest[folder] = []
            info(f"  → 扫描 '{folder}' (*{ext} 文件)...")

            for root, _, files in os.walk(folder):
                for file in files:
                    if file.endswith(ext):
                        file_path = os.path.join(root, file)
                        arcname = os.path.join(folder, file)
                        zipf.write(file_path, arcname)
                        file_manifest[folder].append(file)
                        total_files += 1

    if total_files == 0:
        error("❌ 没有找到匹配的文件！")
        sys.exit(1)

    return zip_name, file_manifest


def generate_release_notes(tag_date, tag_time, manifest):
    """生成 Markdown 发布说明"""
    txt_count = len(manifest.get("merged-rules", []))
    mrs_count = len(manifest.get("merged-rules-mrs", []))
    total_count = txt_count + mrs_count

    details_md = ""
    for folder, files in manifest.items():
        if files:
            icon = "📝" if "txt" in TARGET_CONFIG.get(folder, "") else "🧩"
            details_md += f"#### {icon} {folder} ({len(files)})\n"
            files.sort()
            for f in files:
                details_md += f"- `{f}`\n"
            details_md += "\n"

    commit_sha = os.getenv("GITHUB_SHA", "unknown")[:7]
    repo = os.getenv("GITHUB_REPOSITORY", "owner/repo")
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")

    notes = f"""
## 🚀 规则集合自动构建 (Auto Build)

> **更新时间**: `{tag_date} {tag_time}` (北京时间)  
> **触发提交**: `{commit_sha}`

### 📊 概览统计

| 规则类型 | 来源目录 | 文件数量 | 格式 |
| :--- | :--- | :---: | :---: |
| 📝 文本规则 | `merged-rules` | **{txt_count}** | `.txt` |
| 🧩 MRS 规则 | `merged-rules-mrs` | **{mrs_count}** | `.mrs` |
| **总计** | - | **{total_count}** | - |

<details>
<summary>🔍 <b>点击查看详细文件列表</b></summary>

{details_md}

</details>

---
*由 GitHub Actions 自动生成 • [查看构建日志]({server}/{repo}/actions)*
"""
    return notes


def main():
    group_start("🚀 处理发布")

    utc_now = datetime.datetime.now(datetime.timezone.utc)
    beijing_now = utc_now + datetime.timedelta(hours=8)
    tag_date = beijing_now.strftime("%Y-%m-%d")
    tag_time = beijing_now.strftime("%H:%M:%S")
    release_tag = f"rules-{tag_date}"

    info(f"📅 目标发布标签: {release_tag}")

    # ---- 变更检测 ----
    if CHANGE_DETECTION:
        section("🔍 内容变更检测")
        # 分别计算两个目录的哈希
        h1, c1 = dir_hash("merged-rules", "*.txt")
        h2, c2 = dir_hash("merged-rules-mrs", "*.mrs")
        combined_hash = f"{h1}|{h2}|{c1}|{c2}"

        last_hash = load_last_hash()
        if last_hash and last_hash == combined_hash:
            info("  ✅ 规则内容无变化，跳过发布")
            group_end()
            return
        elif last_hash and last_hash != combined_hash:
            info(f"  🆕 检测到变化 (文件数: {c1 + c2})，继续发布...")
        else:
            info(f"  🆕 首次发布 (文件数: {c1 + c2})")

    # ---- 打包 ----
    zip_file, manifest = zip_target_files(tag_date)

    # ---- 删除旧 Release ----
    if run_gh(["release", "view", release_tag]):
        info(f"🔄 已存在 Release {release_tag}，删除以更新...")
        run_gh(["release", "delete", release_tag, "--yes"])
        run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{release_tag}"])

    # ---- 生成 Release Notes ----
    info("📝 生成发布说明...")
    notes = generate_release_notes(tag_date, tag_time, manifest)

    # ---- 创建新 Release ----
    info(f"🚀 上传 Release {release_tag}...")
    run_gh([
        "release", "create", release_tag, zip_file,
        "--title", f"Merged Rules - {tag_date}",
        "--notes", notes,
        "--latest",
    ])

    # ---- 保存哈希 ----
    if CHANGE_DETECTION:
        save_last_hash(combined_hash)
        info("  📌 已保存当前内容哈希")

    # ---- 清理旧 Release ----
    info(f"🧹 清理 {KEEP_DAYS} 天前的旧 Release...")
    releases_json = run_gh(["release", "list", "--limit", "50", "--json", "tagName,createdAt"])

    if releases_json:
        releases = json.loads(releases_json)
        cutoff_time = utc_now - datetime.timedelta(days=KEEP_DAYS)

        cleaned = 0
        for rel in releases:
            created_at = datetime.datetime.fromisoformat(
                rel["createdAt"].replace("Z", "+00:00")
            )
            tag = rel["tagName"]
            if created_at < cutoff_time and tag != release_tag:
                info(f"  🗑 删除旧 Release: {tag}")
                run_gh(["release", "delete", tag, "--yes"])
                run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{tag}"])
                cleaned += 1
        if cleaned == 0:
            info("  无需清理")

    # ---- 清理临时 zip ----
    if os.path.exists(zip_file):
        os.unlink(zip_file)

    group_end()
    success("✅ 发布完成")


if __name__ == "__main__":
    main()
