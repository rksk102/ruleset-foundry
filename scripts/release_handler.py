import datetime
import json
import os
import subprocess
import sys
import zipfile

from config_loader import get
from logger import error, get_logger, group_end, group_start, info, section, success, warning
from utils import beijing_now, dir_hash, load_last_hash, save_last_hash

logger = get_logger()

REPO_ROOT = os.getcwd()
TARGET_CONFIG = {
    "merged-rules": ".txt",
    "merged-rules-mrs": ".mrs",
}
KEEP_DAYS = get("behavior", "release_keep_days", default=3)
CHANGE_DETECTION = get("behavior", "release_change_detection", default=True)


def run_gh(cmd_list, fail_fast=False):
    try:
        result = subprocess.run(["gh"] + cmd_list, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        if fail_fast:
            # 删除失败却继续重建会造成 Release 与 tag 不一致，必须显式失败
            error(f"  GH CLI 失败: {e.stderr.strip()}")
            sys.exit(1)
        warning(f"  GH CLI 警告: {e.stderr.strip()}")
        return None


def should_publish(current_hash, last_hash, enabled):
    if not enabled:
        return True, "变更检测已关闭"
    if not last_hash:
        return True, "首次发布"
    if last_hash != current_hash:
        return True, "检测到变化"
    return False, "内容无变化"


def zip_target_files(tag_date):
    zip_name = f"merged-rules-{tag_date}.zip"
    info(f"打包文件到 {zip_name}...")

    file_manifest = {}
    total_files = 0

    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zipf:
        for folder, ext in TARGET_CONFIG.items():
            if not os.path.exists(folder):
                warning(f"  目录 '{folder}' 不存在，跳过")
                continue

            file_manifest[folder] = []
            info(f"  -> 扫描 '{folder}' (*{ext} 文件)...")

            for root, _, files in os.walk(folder):
                for file in files:
                    if file.endswith(ext):
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, REPO_ROOT)
                        zipf.write(file_path, arcname)
                        file_manifest[folder].append(arcname)
                        total_files += 1

    if total_files == 0:
        error("没有找到匹配的文件！")
        sys.exit(1)

    return zip_name, file_manifest


def generate_release_notes(tag_date, tag_time, manifest):
    txt_count = len(manifest.get("merged-rules", []))
    mrs_count = len(manifest.get("merged-rules-mrs", []))
    total_count = txt_count + mrs_count

    details_md = ""
    for folder, files in manifest.items():
        if files:
            ext = TARGET_CONFIG.get(folder, "")
            icon = "[TXT]" if "txt" in ext else "[MRS]"
            details_md += f"#### {icon} {folder} ({len(files)})\n"
            files.sort()
            for f in files:
                details_md += f"- `{f}`\n"
            details_md += "\n"

    commit_sha = os.getenv("GITHUB_SHA", "unknown")[:7]
    repo = os.getenv("GITHUB_REPOSITORY", "owner/repo")
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")

    notes = f"""
## 规则集合自动构建 (Auto Build)

> **更新时间**: `{tag_date} {tag_time}` (北京时间)<br>
> **触发提交**: `{commit_sha}`

### 概览统计

| 规则类型 | 来源目录 | 文件数量 | 格式 |
| :--- | :--- | :---: | :---: |
| 文本规则 | `merged-rules` | **{txt_count}** | `.txt` |
| MRS 规则 | `merged-rules-mrs` | **{mrs_count}** | `.mrs` |
| **总计** | - | **{total_count}** | - |

<details>
<summary><b>点击查看详细文件列表</b></summary>

{details_md}

</details>

---
*由 GitHub Actions 自动生成 - [查看构建日志]({server}/{repo}/actions)*
"""
    return notes


def main():
    group_start("处理发布")

    utc_now = datetime.datetime.now(datetime.timezone.utc)
    now_bj = beijing_now()
    tag_date = now_bj.strftime("%Y-%m-%d")
    tag_time = now_bj.strftime("%H:%M:%S")
    release_tag = f"rules-{tag_date}"

    info(f"目标发布标签: {release_tag}")

    if CHANGE_DETECTION:
        section("内容变更检测")
        # .txt 按正文哈希（忽略 # Date: 等元数据）；.mrs 由正文派生，整文件哈希
        h1, c1 = dir_hash("merged-rules", "*.txt", skip_comments=True)
        h2, c2 = dir_hash("merged-rules-mrs", "*.mrs")
        combined_hash = f"{h1}|{h2}|{c1}|{c2}"

        if c1 != c2:
            error(f"  产物数量不一致: .txt={c1} 与 .mrs={c2}，可能存在空产物漂移")

        publish, why = should_publish(combined_hash, load_last_hash(), True)
        info(f"  {why} ({c1 + c2} 个文件)")
        if not publish:
            group_end()
            return

    zip_file, manifest = zip_target_files(tag_date)

    if run_gh(["release", "view", release_tag]):
        info(f"已存在 Release {release_tag}，删除以更新...")
        run_gh(["release", "delete", release_tag, "--yes"], fail_fast=True)
        run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{release_tag}"], fail_fast=True)

    info("生成发布说明...")
    notes = generate_release_notes(tag_date, tag_time, manifest)

    info(f"上传 Release {release_tag}...")
    create_result = run_gh([
        "release", "create", release_tag, zip_file,
        "--title", f"Merged Rules - {tag_date}",
        "--notes", notes,
        "--latest",
    ])
    if create_result is None:
        error("  Release 创建失败，不保存哈希，下次运行将重试")
        if os.path.exists(zip_file):
            os.unlink(zip_file)
        sys.exit(1)

    if CHANGE_DETECTION:
        save_last_hash(combined_hash)
        info("  已保存当前内容哈希")

    info(f"清理 {KEEP_DAYS} 天前的旧 Release...")
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
                info(f"  删除旧 Release: {tag}")
                run_gh(["release", "delete", tag, "--yes"], fail_fast=True)
                run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{tag}"], fail_fast=True)
                cleaned += 1
        if cleaned == 0:
            info("  无需清理")

    if os.path.exists(zip_file):
        os.unlink(zip_file)

    group_end()
    success("发布完成")

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n### 发布报告\n\n")
            f.write("| 项目 | 值 |\n| :--- | :--- |\n")
            f.write(f"| 发布标签 | `{release_tag}` |\n")
            f.write(f"| 文本规则 | **{len(manifest.get('merged-rules', []))}** |\n")
            f.write(f"| MRS 规则 | **{len(manifest.get('merged-rules-mrs', []))}** |\n")


if __name__ == "__main__":
    main()
