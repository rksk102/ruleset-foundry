import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import manifest
from config_loader import ConfigError, get
from logger import (
    error,
    gh_error,
    group_end,
    group_start,
    info,
    section,
    success,
    warning,
)
from utils import (
    anchor_cwd_to_repo_root,
    atomic_write,
    beijing_now,
    combined_products_hash,
    file_sha256,
    load_last_hash,
    save_last_hash,
)

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
try:
    RULESETS_DIR = get("paths", "rulesets_dir", default="rulesets")
    MERGED_DIR = get("paths", "merged_output_dir", default="merged-rules")
    MRS_DIR = get("paths", "mrs_output_dir", default="merged-rules-mrs")
    KEEP_DAYS = get("behavior", "release_keep_days", default=3)
    CHANGE_DETECTION = get("behavior", "release_change_detection", default=True)
    ALLOW_PARTIAL = get("behavior", "allow_partial", default=False)
except ConfigError as _cfg_err:
    gh_error(f"配置校验失败: {_cfg_err}")
    sys.exit(1)

TARGET_CONFIG = {
    MERGED_DIR: ".txt",
    MRS_DIR: ".mrs",
}
BASELINE_MISSING = "清单基线缺失"
GH_TIMEOUT = 120
ASSET_CONFIRM_ATTEMPTS = 3
ASSET_CONFIRM_DELAY = 3
SHA256SUMS_NAME = "SHA256SUMS"


def expected_assets(asset_id):
    return {f"merged-rules-{asset_id}.zip", SHA256SUMS_NAME}


def release_asset_names(release_tag):
    raw = run_gh(
        [
            "release",
            "view",
            release_tag,
            "--json",
            "assets",
            "--jq",
            '.assets[] | select(.state == "uploaded") | .name',
        ]
    )
    if raw is None:
        return None
    return {line.strip() for line in raw.splitlines() if line.strip()}


RULES_TAG_RE = re.compile(r"^rules-(?:[0-9a-f]{12}|\d{8}-\d{4})$")


def release_id_for_hash(product_hash):
    return hashlib.sha256(product_hash.encode("utf-8")).hexdigest()[:12]


def latest_rules_release_tag():
    raw = run_gh(["release", "list", "--limit", "100", "--json", "tagName,isLatest"])
    if raw is None:
        return None
    try:
        releases = json.loads(raw)
    except (TypeError, ValueError) as e:
        warning(f"  gh release list 返回无法解析的 JSON: {e}")
        return None
    if not isinstance(releases, list):
        warning("  gh release list 返回的不是列表，按查询失败处理")
        return None
    fallback = None
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        tag = rel.get("tagName")
        if isinstance(tag, str) and RULES_TAG_RE.fullmatch(tag):
            if fallback is None:
                fallback = tag
            if rel.get("isLatest") is True:
                return tag
    return fallback


def needs_release_repair():
    """无内容变化时，最近一次 rules-* Release 缺失或资产不全则需要补发。"""
    tag = latest_rules_release_tag()
    if tag is None:
        return True
    names = release_asset_names(tag)
    if names is None:
        return True
    return not expected_assets(tag[len("rules-") :]) <= names


def confirm_release_assets(release_tag, expected):
    last_names = None
    for attempt in range(ASSET_CONFIRM_ATTEMPTS):
        names = release_asset_names(release_tag)
        if names is not None:
            last_names = names
            if expected <= names:
                return names
        if attempt + 1 < ASSET_CONFIRM_ATTEMPTS:
            time.sleep(ASSET_CONFIRM_DELAY)
    return last_names


def product_dirs():
    return MERGED_DIR, MRS_DIR


def baseline_files():
    return (os.path.join(RULESETS_DIR, manifest.MANIFEST_NAME),)


def baseline_required():
    """同步产物目录存在 = 同轮流水线工作区，此时清单基线必须存在。"""
    return os.path.isdir(RULESETS_DIR)


def merge_product_entries(merge_tasks=None, baseline=None):
    tasks = get("merges") if merge_tasks is None else merge_tasks
    if ALLOW_PARTIAL and baseline is not None:
        baseline_set = set(baseline)
        tasks = [
            task
            for task in tasks or []
            if isinstance(task, dict) and any(rel in baseline_set for rel in task.get("inputs") or [])
        ]
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
            expected = sorted(set(entries) | merge_product_entries(baseline=entries))
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
                f"{BASELINE_MISSING}: {manifest.MANIFEST_NAME} 不存在或为空（已检查 {' / '.join(baseline_files())}）"
            )
        if not txt_files and not mrs_files:
            raise manifest.ManifestError(
                f"没有任何产物可校验: {txt_dir} 与 {mrs_dir} 均不存在或为空"
                "（通常在非仓库根目录运行时出现，拒绝空跑通过）"
            )
        warning(f"  {BASELINE_MISSING}: {manifest.MANIFEST_NAME} 不存在或为空，跳过绝对基准校验")
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
        gh_error(f"产物校验失败: {e}")
        group_end()
        sys.exit(1)
    if verified is not None:
        info(f"  产物校验通过（{verified} 项）")
    return verified


def verify_only():
    group_start("产物校验（发布前预检）")
    enforce_products()
    group_end()


def run_gh(cmd_list):
    try:
        result = subprocess.run(["gh", *cmd_list], capture_output=True, text=True, check=True, timeout=GH_TIMEOUT)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        warning(f"  GH CLI 超时（>{GH_TIMEOUT}s）: {' '.join(cmd_list)}")
        return None
    except subprocess.CalledProcessError as e:
        warning(f"  GH CLI 警告: {e.stderr.strip()}")
        return None
    except OSError as e:
        error(f"  无法执行 gh CLI: {e}")
        gh_error(f"无法执行 gh CLI: {e}")
        sys.exit(1)


def should_publish(current_hash, last_hash, enabled, force=False):
    if force:
        return True, "强制发布（artifacts 分支缺失等场景，由流水线显式要求）"
    if not enabled:
        return True, "变更检测已关闭"
    if not last_hash:
        return True, "首次发布"
    if last_hash != current_hash:
        return True, "检测到变化"
    return False, "内容无变化"


def zip_target_files(asset_id):
    zip_name = f"merged-rules-{asset_id}.zip"
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

            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for file in files:
                    if file.startswith("."):
                        continue
                    if file.endswith(ext):
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, REPO_ROOT)
                        if arcname.startswith(("..", "/", "\\")) or os.path.isabs(arcname):
                            error(f"  zip 条目越出仓库范围，拒绝打包: {arcname}")
                            gh_error(f"zip 条目越出仓库范围: {arcname}（REPO_ROOT={REPO_ROOT}）")
                            sys.exit(1)
                        zipf.write(file_path, arcname)
                        file_manifest[folder].append(arcname)
                        total_files += 1

    if total_files == 0:
        error("没有找到匹配的文件！")
        sys.exit(1)

    return zip_name, file_manifest


def write_sha256sums(zip_name, file_map):
    """SHA256SUMS：zip 一行 + 各产物文件逐行，供用户 sha256sum -c 校验。"""
    lines = [f"{file_sha256(zip_name)}  {zip_name}"]
    for folder in TARGET_CONFIG:
        for arcname in sorted(file_map.get(folder, [])):
            rel = arcname.replace("\\", "/")
            lines.append(f"{file_sha256(rel)}  {rel}")
    atomic_write("SHA256SUMS", lines)
    return "SHA256SUMS"


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


def publish_release(release_tag, zip_file, title, notes, exists, extra_assets=None):
    assets = [zip_file, *(extra_assets or [])]
    if exists:
        if run_gh(["release", "upload", release_tag, *assets, "--clobber"]) is None:
            return None
        return run_gh(
            [
                "release",
                "edit",
                release_tag,
                "--title",
                title,
                "--notes",
                notes,
                "--latest",
            ]
        )

    return run_gh(
        [
            "release",
            "create",
            release_tag,
            *assets,
            "--title",
            title,
            "--notes",
            notes,
            "--latest",
        ]
    )


def main():
    if "--verify-only" in sys.argv[1:]:
        verify_only()
        return

    if "--print-hash" in sys.argv[1:]:
        print(combined_products_hash(*product_dirs())[0])
        return

    if "--needs-publish" in sys.argv[1:]:
        current, _c1, _c2 = combined_products_hash(*product_dirs())
        force = os.getenv("FORCE_RELEASE", "").lower() in ("1", "true", "yes")
        publish, _why = should_publish(current, load_last_hash(), CHANGE_DETECTION, force=force)
        if not publish:
            publish = needs_release_repair()
        print("true" if publish else "false")
        return

    group_start("处理发布")

    utc_now = datetime.datetime.now(datetime.UTC)
    now_bj = beijing_now()
    tag_date = now_bj.strftime("%Y-%m-%d")
    tag_time = now_bj.strftime("%H:%M:%S")

    section("产物校验")
    txt_dir, mrs_dir = product_dirs()
    combined_hash, c1, c2 = combined_products_hash(txt_dir, mrs_dir)
    tag_id = release_id_for_hash(combined_hash)
    release_tag = f"rules-{tag_id}"
    info(f"目标发布标签: {release_tag}")

    enforce_products(txt_dir, mrs_dir)

    if c1 != c2:
        error(f"  产物数量不一致: .txt={c1} 与 .mrs={c2}，可能存在空产物漂移")
        gh_error(f"产物数量不一致: .txt={c1} 与 .mrs={c2}")
        group_end()
        sys.exit(1)

    if CHANGE_DETECTION:
        force_release = os.getenv("FORCE_RELEASE", "").lower() in ("1", "true", "yes")
        publish, why = should_publish(combined_hash, load_last_hash(), True, force=force_release)
        info(f"  {why} ({c1 + c2} 个文件)")
        if not publish and needs_release_repair():
            publish, why = True, "最近一次 Release 资产不全或不存在，补发"
            info(f"  {why}")
        if not publish:
            group_end()
            return
    else:
        info(f"  变更检测已关闭，直接发布 ({c1 + c2} 个文件)")

    expected = expected_assets(tag_id)
    exists = bool(run_gh(["release", "view", release_tag]))
    asset_names = release_asset_names(release_tag) if exists else None
    zip_file = None
    sums_file = None
    if asset_names is not None and expected <= asset_names:
        info(f"  Release {release_tag} 已存在且资产完整，复用")
        if run_gh(["release", "edit", release_tag, "--latest"]) is None:
            error(f"  无法将 {release_tag} 标记为 latest，本轮发布失败")
            gh_error(f"无法将 {release_tag} 标记为 latest，下次运行将重试")
            sys.exit(1)
    else:
        zip_file, file_map = zip_target_files(tag_id)
        sums_file = write_sha256sums(zip_file, file_map)

        info("生成发布说明...")
        notes = generate_release_notes(tag_date, tag_time, file_map)
        info(f"{'更新' if exists else '创建'} Release {release_tag}...")

        title = f"Merged Rules - {tag_date} {tag_time}"
        if publish_release(release_tag, zip_file, title, notes, exists, extra_assets=[sums_file]) is None:
            error("  Release 发布失败，不保存哈希，下次运行将重试")
            gh_error(f"Release {release_tag} 发布失败（不保存哈希，下次运行重试）")
            for artifact in (zip_file, sums_file):
                if os.path.exists(artifact):
                    os.unlink(artifact)
            sys.exit(1)

        asset_names = confirm_release_assets(release_tag, expected)
        if asset_names is None:
            error(f"  Release {release_tag} 的资产连续 {ASSET_CONFIRM_ATTEMPTS} 次无法确认，判定失败")
            gh_error(f"Release {release_tag} 资产连续 {ASSET_CONFIRM_ATTEMPTS} 次无法确认")
            for artifact in (zip_file, sums_file):
                if os.path.exists(artifact):
                    os.unlink(artifact)
            sys.exit(1)
        if not expected <= asset_names:
            missing = sorted(expected - asset_names)
            error(f"  Release {release_tag} 缺少资产 {missing}（应含 zip 与 {SHA256SUMS_NAME}），判定失败")
            gh_error(f"Release {release_tag} 缺少资产 {missing}")
            for artifact in (zip_file, sums_file):
                if os.path.exists(artifact):
                    os.unlink(artifact)
            sys.exit(1)
        info(f"  已确认 Release 资产: {sorted(asset_names)}")

    if CHANGE_DETECTION:
        save_last_hash(combined_hash)
        info("  已保存当前内容哈希")

    info(f"清理 {KEEP_DAYS} 天前的旧 Release...")
    releases_json = run_gh(["release", "list", "--limit", "200", "--json", "tagName,createdAt"])

    if releases_json:
        try:
            releases = json.loads(releases_json)
        except (TypeError, ValueError) as e:
            # 发布与哈希保存已完成，清理失败只告警不标红（下轮运行自愈）
            warning(f"  旧 Release 清理失败: gh 返回的列表无法解析（{e}），本轮跳过清理")
            releases = []

        cutoff_time = utc_now - datetime.timedelta(days=KEEP_DAYS)

        cleaned = 0
        for rel in releases:
            try:
                created_at = datetime.datetime.fromisoformat(rel["createdAt"].replace("Z", "+00:00"))
                tag = rel["tagName"]
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                warning(f"  跳过无法解析的 Release 记录: {rel!r}（{e}）")
                continue
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

    if zip_file and os.path.exists(zip_file):
        os.unlink(zip_file)
    if sums_file and os.path.exists(sums_file):
        os.unlink(sums_file)

    group_end()
    success("发布完成")

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n### 发布报告\n\n")
            f.write("| 项目 | 值 |\n| :--- | :--- |\n")
            f.write(f"| 发布标签 | `{release_tag}` |\n")
            f.write(f"| 文本规则 | **{c1}** |\n")
            f.write(f"| MRS 规则 | **{c2}** |\n")


if __name__ == "__main__":
    anchor_cwd_to_repo_root()
    main()
