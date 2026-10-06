import datetime
import json
import os
import subprocess
import sys
import time
import zipfile

import manifest
from config_loader import get
from logger import error, group_end, group_start, info, section, success, warning
from utils import beijing_now, combined_products_hash, load_last_hash, save_last_hash

REPO_ROOT = os.getcwd()
RULESETS_DIR = get("paths", "rulesets_dir", default="rulesets")
MERGED_DIR = get("paths", "merged_output_dir", default="merged-rules")
MRS_DIR = get("paths", "mrs_output_dir", default="merged-rules-mrs")
TARGET_CONFIG = {
    MERGED_DIR: ".txt",
    MRS_DIR: ".mrs",
}
KEEP_DAYS = get("behavior", "release_keep_days", default=3)
CHANGE_DETECTION = get("behavior", "release_change_detection", default=True)
MANIFEST_NAME = "products.manifest"
BASELINE_MISSING = "清单基线缺失"
GH_TIMEOUT = 120
ASSET_CONFIRM_ATTEMPTS = 3
ASSET_CONFIRM_DELAY = 3


def release_asset_count(release_tag):
    raw = run_gh(["release", "view", release_tag, "--json", "assets",
                  "--jq", '[.assets[] | select(.state == "uploaded")] | length'])
    if raw is None or not raw.strip().isdigit():
        return None
    return int(raw)


def confirm_release_assets(release_tag):
    for attempt in range(ASSET_CONFIRM_ATTEMPTS):
        count = release_asset_count(release_tag)
        if count is not None:
            return count
        if attempt + 1 < ASSET_CONFIRM_ATTEMPTS:
            time.sleep(ASSET_CONFIRM_DELAY)
    return None


def product_dirs():
    return MERGED_DIR, MRS_DIR


def baseline_files():
    return (
        os.path.join(RULESETS_DIR, MANIFEST_NAME),
        os.path.join(MERGED_DIR, MANIFEST_NAME),
    )


def baseline_required():
    """同步产物目录存在 = 同轮流水线工作区，此时清单基线必须存在。"""
    return os.path.isdir(RULESETS_DIR)


def merge_product_entries(merge_tasks=None):
    tasks = get("merges") if merge_tasks is None else merge_tasks
    entries = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        parts = [task.get(key) for key in ("strategy", "type", "owner", "filename")]
        if all(isinstance(part, str) and part.strip() for part in parts):
            entries.add("/".join(part.strip().strip("/").replace("\\", "/") for part in parts))
    return entries


def load_baseline(baseline_paths=None):
    for path in baseline_paths or baseline_files():
        entries = manifest.load_manifest(path)
        if entries:
            expected = sorted(set(entries) | merge_product_entries())
            info(f"  清单基线 {path}: {len(entries)} 项，含合并产物共 {len(expected)} 项")
            return expected
    return None


def check_expected(expected, actual, context):
    missing, extra = manifest.diff_against(expected, actual)
    for item in missing[:5]:
        error(f"    缺失: {item}")
    for item in extra[:5]:
        error(f"    多出: {item}")
    return manifest.verify_matches(expected, actual, context)


def verify_products(txt_dir=None, mrs_dir=None, require_baseline=None):
    default_txt, default_mrs = product_dirs()
    txt_dir = txt_dir or default_txt
    mrs_dir = mrs_dir or default_mrs
    if require_baseline is None:
        require_baseline = baseline_required()

    baseline = load_baseline()
    txt_files = sorted(manifest.collect_files(txt_dir, ".txt"))
    mrs_files = sorted(manifest.collect_files(mrs_dir, ".mrs"))

    if baseline is None:
        if require_baseline:
            raise manifest.ManifestError(
                f"{BASELINE_MISSING}: {MANIFEST_NAME} 不存在或为空"
                f"（已检查 {' / '.join(baseline_files())}）"
            )
        warning(f"  {BASELINE_MISSING}: {MANIFEST_NAME} 不存在或为空，跳过绝对基准校验")
    else:
        check_expected(baseline, txt_files, f"{txt_dir} 绝对基准校验")

    check_expected(
        [path[: -len(".txt")] for path in txt_files],
        [path[: -len(".mrs")] for path in mrs_files],
        f"{txt_dir} 与 {mrs_dir} 产物逐一对应校验",
    )
    return len(baseline) if baseline else None


def enforce_products(txt_dir=None, mrs_dir=None):
    try:
        verified = verify_products(txt_dir, mrs_dir)
    except manifest.ManifestError as e:
        error(f"  {e}")
        group_end()
        sys.exit(1)
    if verified is not None:
        info(f"  产物校验通过（{verified} 项）")
    return verified


def run_gh(cmd_list, fail_fast=False):
    try:
        result = subprocess.run(["gh", *cmd_list], capture_output=True, text=True,
                                check=True, timeout=GH_TIMEOUT)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        if fail_fast:
            error(f"  GH CLI 超时（>{GH_TIMEOUT}s）: {' '.join(cmd_list)}")
            sys.exit(1)
        warning(f"  GH CLI 超时（>{GH_TIMEOUT}s）: {' '.join(cmd_list)}")
        return None
    except subprocess.CalledProcessError as e:
        if fail_fast:
            error(f"  GH CLI 失败: {e.stderr.strip()}")
            sys.exit(1)
        warning(f"  GH CLI 警告: {e.stderr.strip()}")
        return None
    except OSError as e:
        error(f"  无法执行 gh CLI: {e}")
        sys.exit(1)


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


def generate_release_notes(tag_date, tag_time, file_map):
    txt_dir, mrs_dir = product_dirs()
    txt_count = len(file_map.get(txt_dir, []))
    mrs_count = len(file_map.get(mrs_dir, []))
    total_count = txt_count + mrs_count

    details_md = ""
    for folder, files in file_map.items():
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
| 文本规则 | `{txt_dir}` | **{txt_count}** | `.txt` |
| MRS 规则 | `{mrs_dir}` | **{mrs_count}** | `.mrs` |
| **总计** | - | **{total_count}** | - |

<details>
<summary><b>点击查看详细文件列表</b></summary>

{details_md}

</details>

---
*由 GitHub Actions 自动生成 - [查看构建日志]({server}/{repo}/actions)*
"""
    return notes


def publish_release(release_tag, zip_file, title, notes, exists):
    if exists:
        if run_gh(["release", "upload", release_tag, zip_file, "--clobber"]) is None:
            return None
        return run_gh([
            "release", "edit", release_tag,
            "--title", title,
            "--notes", notes,
            "--latest",
        ])

    return run_gh([
        "release", "create", release_tag, zip_file,
        "--title", title,
        "--notes", notes,
        "--latest",
    ])


def main():
    group_start("处理发布")

    utc_now = datetime.datetime.now(datetime.UTC)
    now_bj = beijing_now()
    tag_date = now_bj.strftime("%Y-%m-%d")
    tag_time = now_bj.strftime("%H:%M:%S")
    release_tag = f"rules-{tag_date}"

    info(f"目标发布标签: {release_tag}")

    section("产物校验")
    txt_dir, mrs_dir = product_dirs()
    combined_hash, c1, c2 = combined_products_hash(txt_dir, mrs_dir)

    enforce_products(txt_dir, mrs_dir)

    if c1 != c2:
        error(f"  产物数量不一致: .txt={c1} 与 .mrs={c2}，可能存在空产物漂移")
        group_end()
        sys.exit(1)

    if CHANGE_DETECTION:
        publish, why = should_publish(combined_hash, load_last_hash(), True)
        info(f"  {why} ({c1 + c2} 个文件)")
        if not publish:
            group_end()
            return
    else:
        info(f"  变更检测已关闭，直接发布 ({c1 + c2} 个文件)")

    zip_file, file_map = zip_target_files(tag_date)

    info("生成发布说明...")
    notes = generate_release_notes(tag_date, tag_time, file_map)
    exists = bool(run_gh(["release", "view", release_tag]))
    info(f"{'更新' if exists else '创建'} Release {release_tag}...")

    if publish_release(release_tag, zip_file, f"Merged Rules - {tag_date}", notes, exists) is None:
        error("  Release 发布失败，不保存哈希，下次运行将重试")
        if os.path.exists(zip_file):
            os.unlink(zip_file)
        sys.exit(1)

    asset_count = confirm_release_assets(release_tag)
    if asset_count is None:
        error(f"  Release {release_tag} 的资产数连续 {ASSET_CONFIRM_ATTEMPTS} 次无法确认，判定失败")
        if os.path.exists(zip_file):
            os.unlink(zip_file)
        sys.exit(1)
    if asset_count == 0:
        error(f"  Release {release_tag} 发布后没有任何资产（--clobber 会先删后传），判定失败")
        if os.path.exists(zip_file):
            os.unlink(zip_file)
        sys.exit(1)
    info(f"  已确认 Release 资产数: {asset_count}")

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
            if not tag.startswith("rules-"):
                continue
            if created_at < cutoff_time and tag != release_tag:
                info(f"  删除旧 Release: {tag}")
                if run_gh(["release", "delete", tag, "--yes"]) is None:
                    warning(f"  删除 Release 失败，跳过: {tag}")
                    continue
                if run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{tag}"]) is None:
                    warning(f"  删除 tag 失败，跳过计数（Release 已删，tag 残留）: {tag}")
                    continue
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
            summary_txt, summary_mrs = product_dirs()
            f.write(f"| 文本规则 | **{len(file_map.get(summary_txt, []))}** |\n")
            f.write(f"| MRS 规则 | **{len(file_map.get(summary_mrs, []))}** |\n")


if __name__ == "__main__":
    main()
