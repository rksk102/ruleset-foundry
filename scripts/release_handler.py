import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import zlib
from pathlib import Path

import manifest
import mrs_format
import publish_guard
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
    BEIJING_TZ,
    anchor_cwd_to_repo_root,
    atomic_write,
    beijing_now,
    combined_products_hash,
    file_sha256,
    load_last_hash,
    save_last_hash,
)

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if __name__ == "__main__":
    anchor_cwd_to_repo_root()

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
MAX_ZIP_ENTRIES = 10000
MAX_ZIP_ENTRY_SIZE = 64 * 1024 * 1024
MAX_ZIP_TOTAL_SIZE = 512 * 1024 * 1024
MAX_ZIP_ASSET_BYTES = 128 * 1024 * 1024
MAX_SUMS_ASSET_BYTES = 16 * 1024 * 1024
ZIP_READ_CHUNK = 1024 * 1024
WORKFLOW_PATH = ".github/workflows/pipeline.yml"
BUILD_JOB_NAME = "1. 同步/合并/转换"
PROVENANCE_PREFIX = "ruleset-foundry-provenance: "
GITHUB_GENERATION_RE = re.compile(r"github-([1-9][0-9]*)-attempt-([1-9][0-9]*)")


class ReleaseError(Exception):
    pass


def expected_assets(asset_id):
    return {f"merged-rules-{asset_id}.zip", SHA256SUMS_NAME}


def release_assets(release_tag):
    raw = run_gh(
        [
            "api",
            f"repos/{{owner}}/{{repo}}/releases/tags/{release_tag}",
            "--jq",
            '.assets[] | [.name, (.digest // "")] | @tsv',
        ],
        strip=False,
    )
    if raw is None:
        return None
    assets = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            warning(f"  无法解析远端资产行: {line!r}")
            return None
        assets[parts[0]] = parts[1].strip().lower()
    return assets


def release_asset_bytes(release_tag, asset_name, max_bytes=None, *, asset_id=None):
    if asset_id is not None:
        return run_gh_bytes([
            "api", f"repos/{{owner}}/{{repo}}/releases/assets/{asset_id}",
            "-H", "Accept: application/octet-stream", "--allow-escape-sequences",
        ], max_bytes=max_bytes)
    return run_gh_bytes(["release", "download", release_tag, "-p", asset_name, "-O", "-"], max_bytes=max_bytes)


def release_sha256sums(release_tag):
    return release_asset_bytes(release_tag, SHA256SUMS_NAME, max_bytes=MAX_SUMS_ASSET_BYTES)


def parse_sha256sums(text):
    entries = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            raise ValueError(f"非法 SHA256SUMS 行: {line!r}")
        rel = parts[1].strip().replace("\\", "/")
        if rel in entries:
            raise ValueError(f"重复的 SHA256SUMS 路径: {rel!r}")
        entries[rel] = parts[0].lower()
    return entries


def local_product_hashes():
    entries = {}
    for folder, ext in TARGET_CONFIG.items():
        if not os.path.exists(folder):
            continue
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                if file.startswith(".") or not file.endswith(ext):
                    continue
                path = os.path.join(root, file)
                rel = os.path.relpath(path, REPO_ROOT).replace("\\", "/")
                entries[rel] = file_sha256(path)
    return entries


def safe_zip_member_name(name):
    if not name or name.startswith("/") or "\\" in name or re.match(r"^[A-Za-z]:", name):
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def check_zip_central_directory(zip_bytes):
    eocd = zip_bytes.rfind(b"PK\x05\x06", max(0, len(zip_bytes) - 65557))
    if eocd < 0 or eocd + 22 > len(zip_bytes):
        return "ZIP 中央目录结束记录缺失"
    try:
        _sig, disk, cd_disk, entries_disk, entries_total, cd_size, cd_offset, comment_size = struct.unpack_from(
            "<4s4H2LH", zip_bytes, eocd
        )
    except struct.error as e:
        return f"ZIP 中央目录结束记录不完整: {e}"
    if eocd + 22 + comment_size != len(zip_bytes):
        return "ZIP 注释长度与文件长度不一致"
    if disk != 0 or cd_disk != 0 or entries_disk != entries_total:
        return "不支持多磁盘 ZIP"
    if entries_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        return "ZIP64 目录超出本项目复用校验范围"
    if entries_total > MAX_ZIP_ENTRIES:
        return f"ZIP 条目数超过限制 {MAX_ZIP_ENTRIES}"
    cd_end = cd_offset + cd_size
    if cd_end > eocd or cd_end > len(zip_bytes):
        return "ZIP 中央目录范围越界"

    pos = cd_offset
    count = 0
    while pos < cd_end:
        count += 1
        if count > MAX_ZIP_ENTRIES:
            return f"ZIP 条目数超过限制 {MAX_ZIP_ENTRIES}"
        if pos + 46 > cd_end or zip_bytes[pos : pos + 4] != b"PK\x01\x02":
            return "ZIP 中央目录条目损坏"
        name_size, extra_size, entry_comment_size = struct.unpack_from("<HHH", zip_bytes, pos + 28)
        pos += 46 + name_size + extra_size + entry_comment_size
        if pos > cd_end:
            return "ZIP 中央目录条目长度越界"
    if pos != cd_end or count != entries_total:
        return "ZIP 中央目录条目计数不一致"
    return None


def verify_zip_content(zip_bytes, local_hashes):
    try:
        directory_error = check_zip_central_directory(zip_bytes)
        if directory_error:
            return False, directory_error
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zipf:
            infos = zipf.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                return False, f"ZIP 条目数超过限制 {MAX_ZIP_ENTRIES}"
            seen = set()
            declared_total = 0
            for entry in infos:
                name = entry.filename
                if not safe_zip_member_name(name):
                    return False, f"ZIP 条目路径越界或非法: {name!r}"
                if entry.is_dir():
                    return False, f"ZIP 含有额外目录条目: {name!r}"
                unix_mode = entry.external_attr >> 16
                file_type = stat.S_IFMT(unix_mode)
                if entry.create_system == 3 and file_type not in (0, stat.S_IFREG):
                    return False, f"ZIP 含非普通文件条目: {name!r}"
                if name in seen:
                    return False, f"ZIP 含重复条目: {name!r}"
                seen.add(name)
                if entry.file_size > MAX_ZIP_ENTRY_SIZE:
                    return False, f"ZIP 单文件解压量超过限制 {MAX_ZIP_ENTRY_SIZE}: {name!r}"
                declared_total += entry.file_size
                if declared_total > MAX_ZIP_TOTAL_SIZE:
                    return False, f"ZIP 总解压量超过限制 {MAX_ZIP_TOTAL_SIZE}"
            if seen != set(local_hashes):
                missing = sorted(set(local_hashes) - seen)
                extra = sorted(seen - set(local_hashes))
                return False, f"ZIP 条目路径集合与本地不一致（缺失 {missing[:5]}，多出 {extra[:5]}）"

            total_size = 0
            for entry in infos:
                digest = hashlib.sha256()
                entry_size = 0
                with zipf.open(entry, "r") as source:
                    while True:
                        read_size = min(
                            ZIP_READ_CHUNK,
                            MAX_ZIP_ENTRY_SIZE - entry_size + 1,
                            MAX_ZIP_TOTAL_SIZE - total_size + 1,
                        )
                        chunk = source.read(read_size)
                        if not chunk:
                            break
                        entry_size += len(chunk)
                        total_size += len(chunk)
                        if entry_size > MAX_ZIP_ENTRY_SIZE:
                            return False, f"ZIP 单文件实际解压量超过限制 {MAX_ZIP_ENTRY_SIZE}"
                        if total_size > MAX_ZIP_TOTAL_SIZE:
                            return False, f"ZIP 实际总解压量超过限制 {MAX_ZIP_TOTAL_SIZE}"
                        digest.update(chunk)
                if entry_size != entry.file_size:
                    return False, f"ZIP 条目实际长度与元数据不一致: {entry.filename!r}"
                if digest.hexdigest() != local_hashes[entry.filename]:
                    return False, f"ZIP 条目内容与本地哈希不一致: {entry.filename!r}"
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError, EOFError, zlib.error, struct.error) as e:
        return False, f"ZIP 读取失败: {e}"
    return True, "ZIP 路径与逐文件内容一致"


def verify_release_reusable(release_tag, tag_id, *, records=None):
    assets = (
        release_assets(release_tag) if records is None
        else {name: item["digest"] or "" for name, item in records.items() if item["state"] == "uploaded"}
    )
    if assets is None:
        return False, "无法读取远端资产列表"
    expected = expected_assets(tag_id)
    missing = sorted(expected - set(assets))
    if missing:
        return False, f"缺少资产 {missing}"
    if records is not None and (extra := sorted(set(records) - expected)):
        return False, f"多出资产 {extra}"

    zip_name = f"merged-rules-{tag_id}.zip"
    zip_digest = assets.get(zip_name, "").removeprefix("sha256:")
    sums_digest = assets.get(SHA256SUMS_NAME, "").removeprefix("sha256:")

    sums_bytes = (
        release_sha256sums(release_tag) if records is None
        else release_asset_bytes(
            release_tag, SHA256SUMS_NAME, MAX_SUMS_ASSET_BYTES, asset_id=records[SHA256SUMS_NAME]["id"]
        )
    )
    if sums_bytes is None:
        return False, f"无法下载 {SHA256SUMS_NAME}"
    if sums_digest and hashlib.sha256(sums_bytes).hexdigest() != sums_digest:
        return False, f"{SHA256SUMS_NAME} 自身 digest 不匹配"
    try:
        sums_text = sums_bytes.decode("utf-8", errors="strict")
    except UnicodeError:
        return False, f"{SHA256SUMS_NAME} 不是有效的 UTF-8"
    try:
        sums_entries = parse_sha256sums(sums_text)
    except ValueError as e:
        return False, str(e)

    lines = sums_text.splitlines()
    first_parts = lines[0].strip().split(None, 1) if lines else []
    first_digest = first_parts[0].lower() if len(first_parts) == 2 else ""
    first_path = first_parts[1].strip().replace("\\", "/") if len(first_parts) == 2 else ""
    if first_path != zip_name or first_digest != sums_entries.get(zip_name):
        return False, f"{SHA256SUMS_NAME} 首行必须对应当前 ZIP"

    zip_bytes = release_asset_bytes(
        release_tag, zip_name, MAX_ZIP_ASSET_BYTES, asset_id=records[zip_name]["id"] if records is not None else None
    )
    if zip_bytes is None:
        return False, f"无法下载 {zip_name}"
    actual_zip_digest = hashlib.sha256(zip_bytes).hexdigest()
    if zip_digest and first_digest != zip_digest:
        return False, "zip 的远端 digest 与 SHA256SUMS 首行不一致"
    if first_digest != actual_zip_digest:
        return False, "ZIP 原始字节与 SHA256SUMS 首行 digest 不匹配"
    if zip_digest and zip_digest != actual_zip_digest:
        return False, "ZIP 原始字节与远端 digest 不匹配"

    local_hashes = local_product_hashes()
    if not local_hashes:
        return False, "本地没有可校验产物"
    if sums_entries != {zip_name: actual_zip_digest, **local_hashes}:
        return False, "SHA256SUMS 的逐文件哈希与本地产物不一致"
    reusable, why = verify_zip_content(zip_bytes, local_hashes)
    if not reusable:
        return False, why
    return True, "资产与当前内容一致"


def release_repair_reason(current_hash):
    expected_tag = current_release_tag()
    tag = latest_rules_release_tag()
    if tag is None:
        return "未找到 isLatest 的 rules-* Release"
    if tag != expected_tag:
        return f"latest 指向 {tag}，当前内容应为 {expected_tag}"
    reusable, why = verify_release_reusable(tag, expected_tag[len("rules-") :])
    if not reusable:
        return why
    return None


def release_asset_names(release_tag):
    raw = run_gh([
        "release", "view", release_tag, "--json", "assets", "--jq",
        '.assets[] | select(.state == "uploaded") | .name',
    ])
    if raw is None:
        return None
    return {line.strip() for line in raw.splitlines() if line.strip()}


def release_asset_records(release_id):
    raw = run_gh([
        "api", f"repos/{{owner}}/{{repo}}/releases/{release_id}/assets", "--paginate",
        "--jq", '.[] | {name,id,state,size,digest}',
    ])
    if raw is None:
        raise ReleaseError("无法读取目标 Release 的资产列表")
    records = {}
    ids = set()
    for line in raw.splitlines():
        asset = _json_record(line, "Release 资产")
        name = asset.get("name")
        asset_id = asset.get("id")
        digest = asset.get("digest")
        if (
            not isinstance(name, str) or not name or name in records
            or type(asset_id) is not int or asset_id <= 0 or asset_id in ids
            or type(asset.get("size")) is not int or asset["size"] < 0
            or asset.get("state") not in ("uploaded", "open", "starter")
            or (digest is not None and (
                not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            ))
        ):
            raise ReleaseError("无法确认目标 Release 的资产状态或摘要")
        ids.add(asset_id)
        records[name] = asset
    return records


RULES_TAG_RE = re.compile(r"^rules-(?:[0-9a-f]{12}|\d{8}-\d{4})$")
RULES_LEGACY_TAG_RE = re.compile(r"^rules-(\d{8})-(\d{4})$")
TAG_DEREF_LIMIT = 3
TAG_OBJECT_JQ = '[.object.type, .object.sha] | @tsv'
GIT_SHA_RE = re.compile(r"[0-9a-f]{40,64}")


def _parse_tag_object(raw):
    """解析 git ref/tag 对象的 type 与 sha；格式不符返回 None。"""
    if not raw:
        return None
    parts = raw.split("\t")
    if len(parts) != 2:
        return None
    obj_type, obj_sha = (part.strip().lower() for part in parts)
    if obj_type not in ("commit", "tag", "tree", "blob") or not GIT_SHA_RE.fullmatch(obj_sha):
        return None
    return obj_type, obj_sha


def _deref_tag_commit(target):
    for _ in range(TAG_DEREF_LIMIT):
        if target is None:
            return None
        obj_type, obj_sha = target
        if obj_type == "commit":
            return obj_sha
        if obj_type != "tag":
            return None
        target = _parse_tag_object(
            run_gh(["api", f"repos/{{owner}}/{{repo}}/git/tags/{obj_sha}", "--jq", TAG_OBJECT_JQ])
        )
    return None


def tag_commit_sha(tag):
    """tag 最终指向的提交 SHA；annotated tag 逐层解引用，无法确认时返回 None。"""
    return _deref_tag_commit(
        _parse_tag_object(
            run_gh(["api", f"repos/{{owner}}/{{repo}}/git/ref/tags/{tag}", "--jq", TAG_OBJECT_JQ])
        )
    )


def new_release_tag_target_valid(tag, expected_sha):
    """仅供旧兼容调用；新发布流程必须使用带归属的 annotated tag。"""
    actual_sha = tag_commit_sha(tag)
    return actual_sha == expected_sha


def orphan_tag_created_at(tag):
    """孤立自动 tag 的创建时间；无法确认时返回 None，调用方必须保留该 tag。"""
    legacy = RULES_LEGACY_TAG_RE.fullmatch(tag)
    if legacy is not None:
        try:
            return datetime.datetime.strptime(
                f"{legacy.group(1)}{legacy.group(2)}", "%Y%m%d%H%M"
            ).replace(tzinfo=BEIJING_TZ)
        except ValueError as e:
            warning(f"  孤立 tag 的旧格式时间无法解析，保留: {tag}（{e}）")
            return None
    repo = os.getenv("GITHUB_REPOSITORY", "")
    ref = os.getenv("GITHUB_REF", "")
    if not repo or not ref.startswith("refs/heads/"):
        return None
    try:
        object_sha = _release_tag_ref(tag)
        if object_sha is None:
            return None
        record, obj = _read_tag_object(tag, object_sha)
        if record["repo"] != repo or record["ref"] != ref or not record["generation"]:
            return None
        date = datetime.datetime.fromisoformat(obj["tagger"]["date"])
        return date if date.tzinfo is not None else None
    except (ReleaseError, KeyError, TypeError, ValueError):
        return None


def release_content_digest():
    entries = json.dumps(sorted(local_product_hashes().items()), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(f"ruleset-foundry release v1\0{entries}".encode()).hexdigest()


def current_release_tag():
    return f"rules-{release_content_digest()[:12]}"


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
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        tag = rel.get("tagName")
        if isinstance(tag, str) and RULES_TAG_RE.fullmatch(tag) and rel.get("isLatest") is True:
            return tag
    return None


def cleanup_orphan_rules_tags(release_tags, current_tag, cutoff_time):
    raw = run_gh(
        [
            "api",
            "repos/{owner}/{repo}/git/matching-refs/tags/rules-",
            "--paginate",
            "--jq",
            ".[].ref",
        ]
    )
    if raw is None:
        warning("  无法读取自动 tag 列表，跳过孤立 tag 对账")
        return 0

    cleaned = 0
    skipped = []
    for line in raw.splitlines():
        ref = line.strip()
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref.removeprefix("refs/tags/")
        if not RULES_TAG_RE.fullmatch(tag) or tag == current_tag or tag in release_tags:
            continue
        created_at = orphan_tag_created_at(tag)
        if created_at is None:
            # 目标 commit 的时间无法证明 tag 对象的年龄。
            skipped.append(tag)
            continue
        if created_at >= cutoff_time:
            continue
        info(f"  删除孤立自动 tag: {tag}")
        if run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{tag}"]) is None:
            warning(f"  删除孤立 tag 失败，留待下轮重试: {tag}")
            continue
        cleaned += 1
    if skipped:
        warning(
            f"  跳过 {len(skipped)} 个无法确认创建时间的孤立 tag: "
            f"{', '.join(skipped[:5])}{' 等' if len(skipped) > 5 else ''}"
        )
    return cleaned


def needs_release_repair(current_hash=None):
    """无内容变化时，latest 必须指向当前内容且资产可校验，否则需要补发。"""
    if current_hash is None:
        tag = latest_rules_release_tag()
        if tag is None:
            return True
        names = release_asset_names(tag)
        if names is None:
            return True
        return not expected_assets(tag[len("rules-") :]) <= names
    return release_repair_reason(current_hash) is not None


def confirm_release_assets(release_tag, expected, content_check=None):
    last_names = None
    content_failed = False
    for attempt in range(ASSET_CONFIRM_ATTEMPTS):
        names = release_asset_names(release_tag)
        if names is not None:
            last_names = names
            if expected <= names:
                if content_check is None:
                    return names
                reusable, why = content_check()
                if reusable:
                    return names
                content_failed = True
                warning(f"  Release 资产内容校验未通过: {why}")
        if attempt + 1 < ASSET_CONFIRM_ATTEMPTS:
            time.sleep(ASSET_CONFIRM_DELAY)
    return None if content_failed else last_names


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
        baseline_path = Path(path)
        if baseline_path.is_symlink() or baseline_path.parent.is_symlink():
            raise manifest.ManifestError(f"输入清单或其目录不能是符号链接: {path}")
        baseline = manifest.load_manifest(path)
        if baseline is not None and baseline.entries:
            entries = set(baseline.entries)
            expected = sorted(entries | merge_product_entries(baseline=entries))
            info(f"  清单基线 {path}: {len(entries)} 项，含合并产物共 {len(expected)} 项")
            return baseline, expected
    return None, None


def check_expected(expected, actual, context):
    missing, extra = manifest.diff_against(expected, actual)
    for item in missing[:5]:
        error(f"    缺失: {item}")
    for item in extra[:5]:
        error(f"    多出: {item}")
    return manifest.verify_matches(expected, actual, context)


def validate_product_files(txt_dir, mrs_dir):
    for folder, suffix in ((txt_dir, ".txt"), (mrs_dir, ".mrs")):
        root = Path(folder)
        if not root.exists():
            continue
        for path in root.rglob(f"*{suffix}"):
            rel = path.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if path.is_symlink() or not path.is_file():
                raise manifest.ManifestError(f"非普通文件产物: {path}")
            if suffix == ".mrs" and path.stat().st_size == 0:
                raise manifest.ManifestError(f"零字节 MRS 产物: {path}")
            if suffix == ".mrs":
                behavior = rel.parts[1] if len(rel.parts) > 2 else ""
                try:
                    mrs_format.verify_product_pair(
                        Path(txt_dir) / rel.with_suffix(".txt"), path, behavior, forbid_default_route=True,
                    )
                except mrs_format.MrsFormatError as exc:
                    raise manifest.ManifestError(f"产物内容校验失败: {exc}") from exc


def verify_products(txt_dir=None, mrs_dir=None, require_baseline=None):
    default_txt, default_mrs = product_dirs()
    txt_dir = txt_dir or default_txt
    mrs_dir = mrs_dir or default_mrs
    if require_baseline is None:
        require_baseline = baseline_required()

    txt_files = sorted(manifest.collect_files(txt_dir, ".txt"))
    mrs_files = sorted(manifest.collect_files(mrs_dir, ".mrs"))
    merged_manifest_path = Path(txt_dir) / manifest.MANIFEST_NAME
    txt_upload = [*txt_files, manifest.MANIFEST_NAME] if merged_manifest_path.exists() else txt_files
    manifest.verify_directory_set(txt_dir, txt_upload, f"{txt_dir} 制品文件集合")
    manifest.verify_directory_set(mrs_dir, mrs_files, f"{mrs_dir} 制品文件集合")
    validate_product_files(txt_dir, mrs_dir)
    baseline, expected = load_baseline()
    merged_manifest = manifest.load_manifest(merged_manifest_path)

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
        expected_generation = manifest.expected_publish_generation()
        if expected_generation and baseline.generation != expected_generation:
            raise manifest.ManifestError(
                f"清单 generation 与指定构建不一致: {baseline.generation} != {expected_generation}"
                "（疑似跨轮制品，请重新运行完整流水线或本地构建步骤）"
            )
        if merged_manifest is None:
            raise manifest.ManifestError(
                f"合并完成清单缺失: {os.path.join(txt_dir, manifest.MANIFEST_NAME)}（需重新运行 merger.py）"
            )
        if merged_manifest.generation != baseline.generation:
            raise manifest.ManifestError(
                f"合并清单 generation 与输入清单不一致: {merged_manifest.generation} != {baseline.generation}"
                "（疑似跨轮混合产物，拒绝发布）"
            )
        if sorted(merged_manifest.entries) != expected:
            missing, extra = manifest.diff_against(expected, merged_manifest.entries)
            raise manifest.ManifestError(
                f"{txt_dir} 合并产物清单不一致（缺失 {missing[:5]}，多出 {extra[:5]}）"
            )
        manifest.verify_manifest(merged_manifest, txt_dir, f"{txt_dir} 合并产物哈希校验")
        if manifest.collect_files(RULESETS_DIR, ".txt"):
            manifest.verify_manifest(baseline, RULESETS_DIR, f"{RULESETS_DIR} 输入产物哈希校验")

    check_expected(
        [path[: -len(".txt")] for path in txt_files],
        [path[: -len(".mrs")] for path in mrs_files],
        f"{txt_dir} 与 {mrs_dir} 产物逐一对应校验",
    )
    return len(expected) if baseline is not None else None


def enforce_products(txt_dir=None, mrs_dir=None, require_baseline=None):
    try:
        verified = verify_products(txt_dir, mrs_dir, require_baseline)
    except manifest.ManifestError as e:
        error(f"  {e}")
        gh_error(f"产物校验失败: {e}")
        group_end()
        sys.exit(1)
    if verified is not None:
        info(f"  产物校验通过（{verified} 项）")
    return verified


def verify_only(require_baseline=False):
    """发布前预检；--require-baseline 时缺少清单基线目录或文件一律硬失败。"""
    group_start("产物校验（发布前预检）")
    enforce_products(require_baseline=True if require_baseline else None)
    group_end()


def run_gh(cmd_list, strip=True):
    try:
        result = subprocess.run(["gh", *cmd_list], capture_output=True, text=True, check=True, timeout=GH_TIMEOUT)
        return result.stdout.strip() if strip else result.stdout
    except subprocess.TimeoutExpired:
        warning(f"  GH CLI 超时（>{GH_TIMEOUT}s）: {' '.join(cmd_list)}")
        return None
    except subprocess.CalledProcessError as e:
        warning(f"  GH CLI 警告: {e.stderr.strip()}")
        return None
    except UnicodeError as e:
        # text=True 的解码在 subprocess.run 内完成，非 UTF-8 输出会以 UnicodeDecodeError 逃逸
        warning(f"  GH CLI 输出解码失败（非 UTF-8），按失败处理: {' '.join(cmd_list)}（{e}）")
        return None
    except OSError as e:
        error(f"  无法执行 gh CLI: {e}")
        gh_error(f"无法执行 gh CLI: {e}")
        sys.exit(1)


def _kill_process(process):
    with contextlib.suppress(OSError):
        process.kill()


def _read_bounded_stdout(process, output, max_bytes, deadline, state):
    total = 0
    try:
        while True:
            if time.monotonic() >= deadline:
                state.append("timeout")
                _kill_process(process)
                return
            chunk = process.stdout.read(ZIP_READ_CHUNK)
            if not chunk:
                return
            if max_bytes is not None and total + len(chunk) > max_bytes:
                state.append("oversize")
                _kill_process(process)
                return
            output.write(chunk)
            total += len(chunk)
    except Exception as e:
        state.append(f"read-error: {e}")
        _kill_process(process)


def run_gh_bytes(cmd_list, max_bytes=None):
    with tempfile.TemporaryFile() as stderr_file, tempfile.TemporaryFile() as output:
        process = None
        try:
            process = subprocess.Popen(
                ["gh", *cmd_list],
                stdout=subprocess.PIPE,
                stderr=stderr_file,
            )
            deadline = time.monotonic() + GH_TIMEOUT
            state = []
            reader = threading.Thread(
                target=_read_bounded_stdout,
                args=(process, output, max_bytes, deadline, state),
                daemon=True,
            )
            reader.start()
            try:
                returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                state.append("timeout")
                _kill_process(process)
                returncode = process.wait()
            reader.join(timeout=GH_TIMEOUT)
            if reader.is_alive():
                state.append("read-stuck")
                _kill_process(process)
                reader.join(timeout=5)
            if "oversize" in state:
                warning(f"  资产超过下载上限 {max_bytes} 字节: {' '.join(cmd_list)}")
                return None
            if "timeout" in state:
                warning(f"  GH CLI 超时（>{GH_TIMEOUT}s）: {' '.join(cmd_list)}")
                return None
            if state:
                warning(f"  GH CLI 输出读取失败: {'; '.join(state)}")
                return None
            if returncode != 0:
                stderr_file.seek(0)
                stderr = stderr_file.read().decode("utf-8", errors="replace")
                warning(f"  GH CLI 警告: {stderr.strip()}")
                return None
            output.seek(0)
            return output.read()
        except OSError as e:
            error(f"  无法执行 gh CLI: {e}")
            gh_error(f"无法执行 gh CLI: {e}")
            sys.exit(1)
        finally:
            if process is not None:
                if process.poll() is None:
                    _kill_process(process)
                    process.wait()
                if process.stdout is not None:
                    process.stdout.close()


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


def validate_zip_inputs():
    entries = 0
    total_size = 0
    for folder, ext in TARGET_CONFIG.items():
        if not os.path.exists(folder):
            continue
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                if file.startswith(".") or not file.endswith(ext):
                    continue
                path = os.path.join(root, file)
                size = os.path.getsize(path)
                entries += 1
                total_size += size
                if size > MAX_ZIP_ENTRY_SIZE:
                    return f"产物单文件超过 ZIP 条目上限 {MAX_ZIP_ENTRY_SIZE}: {path}（{size} 字节）"
                if entries > MAX_ZIP_ENTRIES:
                    return f"产物条目数超过 ZIP 上限 {MAX_ZIP_ENTRIES}"
                if total_size > MAX_ZIP_TOTAL_SIZE:
                    return f"产物总大小超过 ZIP 解压上限 {MAX_ZIP_TOTAL_SIZE}"
    return None


def zip_target_files(asset_id):
    zip_name = f"merged-rules-{asset_id}.zip"
    limit_error = validate_zip_inputs()
    if limit_error:
        error(f"  拒绝打包: {limit_error}")
        gh_error(f"发布产物超过 ZIP 限制: {limit_error}")
        sys.exit(1)
    info(f"打包文件到 {zip_name}...")

    file_manifest = {}
    total_files = 0

    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zipf:
        for folder, ext in sorted(TARGET_CONFIG.items()):
            if not os.path.exists(folder):
                warning(f"  目录 '{folder}' 不存在，跳过")
                continue

            file_manifest[folder] = []
            info(f"  -> 扫描 '{folder}' (*{ext} 文件)...")

            for root, dirs, files in os.walk(folder):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for file in sorted(files):
                    if file.startswith("."):
                        continue
                    if file.endswith(ext):
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, REPO_ROOT)
                        if arcname.startswith(("..", "/", "\\")) or os.path.isabs(arcname):
                            error(f"  zip 条目越出仓库范围，拒绝打包: {arcname}")
                            gh_error(f"zip 条目越出仓库范围: {arcname}（REPO_ROOT={REPO_ROOT}）")
                            sys.exit(1)
                        entry = zipfile.ZipInfo(arcname)
                        entry.compress_type = zipfile.ZIP_DEFLATED
                        entry.create_system = 3
                        entry.external_attr = (stat.S_IFREG | 0o644) << 16
                        with open(file_path, "rb") as source, zipf.open(entry, "w") as destination:
                            shutil.copyfileobj(source, destination)
                        file_manifest[folder].append(arcname)
                        total_files += 1

    if total_files == 0:
        error("没有找到匹配的文件！")
        sys.exit(1)

    zip_size = os.path.getsize(zip_name)
    if zip_size > MAX_ZIP_ASSET_BYTES:
        os.unlink(zip_name)
        error(
            f"  拒绝发布: ZIP 资产 {zip_size} 字节超过上限 {MAX_ZIP_ASSET_BYTES} 字节"
        )
        gh_error(f"发布 ZIP 超过大小上限: {zip_size} > {MAX_ZIP_ASSET_BYTES}")
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


def _build_commit_sha():
    sha = os.getenv("GITHUB_SHA")
    if sha is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT or None,
                capture_output=True, text=True, check=True, timeout=GH_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            warning(f"  无法确认构建提交，拒绝创建 Release: {exc}")
            return None
        sha = result.stdout.strip()
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", sha):
        warning("  构建提交必须为完整 SHA，拒绝创建 Release")
        return None
    return sha.lower()


def ensure_publish_current():
    try:
        publish_guard.ensure_current()
    except publish_guard.PublishGuardError as exc:
        if not os.getenv("PUBLISH_GUARD"):
            raise ReleaseError(f"本地发布门控失败: {exc}") from exc
        raise ReleaseError(f"发布新鲜度检查失败: {exc}") from exc


def _json_record(raw, description):
    try:
        result = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReleaseError(f"无法确认{description}") from exc
    if not isinstance(result, dict):
        raise ReleaseError(f"无法确认{description}")
    return result


_RELEASE_RECORD_JQ = "{id,tag_name,draft,body,author,target_commitish,upload_url}"


def _release_record(raw, tag, description):
    record = _json_record(raw, description)
    if (
        type(record.get("id")) is not int or record["id"] <= 0
        or type(record.get("draft")) is not bool or record.get("tag_name") != tag
    ):
        raise ReleaseError(f"无法确认{description}或 ID")
    return record


def _find_release_by_id(release_id, tag):
    raw = run_gh([
        "api", f"repos/{{owner}}/{{repo}}/releases/{release_id}", "--jq", _RELEASE_RECORD_JQ,
    ])
    return _release_record(raw, tag, "目标 Release 状态")


def find_release(tag):
    # 查询完整集合，只有已成功查询且找不到匹配项才表示不存在。
    raw = run_gh([
        "api", "repos/{owner}/{repo}/releases", "--paginate", "--jq",
        f'.[] | select(.tag_name == "{tag}") | {_RELEASE_RECORD_JQ}',
    ])
    if raw == "":
        return None
    return _release_record(raw, tag, "目标 Release 状态")


def _provenance_message(record):
    return PROVENANCE_PREFIX + json.dumps(record, sort_keys=True, separators=(",", ":"))


def _current_provenance(digest):
    sha = _build_commit_sha()
    if sha is None:
        raise ReleaseError("无法确认构建提交，拒绝发布")
    generation = os.getenv("PUBLISH_BUILD_GENERATION", "")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    ref = os.getenv("GITHUB_REF", "")
    if os.getenv("GITHUB_ACTIONS") == "true" and not generation:
        raise ReleaseError("缺少可核验的构建提交或 generation，拒绝创建发布对象")
    if generation and (not GITHUB_GENERATION_RE.fullmatch(generation) or not repo or not ref.startswith("refs/heads/")):
        raise ReleaseError("构建归属环境无效，拒绝创建发布对象")
    return {"sha": sha, "digest": digest, "generation": generation, "repo": repo, "ref": ref, "path": WORKFLOW_PATH}


def verify_original_build(record, current):
    if record == current and not record["generation"]:
        return
    match = GITHUB_GENERATION_RE.fullmatch(record["generation"])
    if match is None or not current["generation"]:
        raise ReleaseError("没有可核验的首次构建 generation，拒绝接续旧发布对象")
    run_id, attempt = map(int, match.groups())
    endpoint = f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/attempts/{attempt}"
    run = _json_record(run_gh(["api", endpoint]), "原始 Actions attempt")
    repository = run.get("repository")
    head_repository = run.get("head_repository")
    if (
        run.get("id") != run_id or run.get("run_attempt") != attempt
        or run.get("head_branch") != record["ref"].removeprefix("refs/heads/")
        or run.get("head_sha") != record["sha"]
        or run.get("path") not in (record["path"], f"{record['path']}@{record['ref']}")
        or run.get("event") not in ("workflow_dispatch", "schedule")
        or any(
            not isinstance(item, dict) or not isinstance(item.get("full_name"), str)
            or item["full_name"].casefold() != record["repo"].casefold()
            for item in (repository, head_repository)
        )
    ):
        raise ReleaseError("原始 Actions attempt 的归属与发布记录不一致")
    jobs = run_gh([
        "api", f"{endpoint}/jobs", "--paginate", "--jq",
        f'.jobs[] | select(.name == "{BUILD_JOB_NAME}") | [.head_sha, .conclusion] | @tsv',
    ])
    if jobs != f"{record['sha']}\tsuccess":
        raise ReleaseError("无法核验同一 Actions attempt 的成功 build job")


def _read_tag_ref(tag):
    return _parse_tag_object(run_gh([
        "api", f"repos/{{owner}}/{{repo}}/git/matching-refs/tags/{tag}", "--paginate",
        "--jq", f'.[] | select(.ref == "refs/tags/{tag}") | {TAG_OBJECT_JQ}',
    ]))


def _release_tag_ref(tag):
    target = _read_tag_ref(tag)
    if target is None:
        return None
    if target[0] != "tag":
        raise ReleaseError(f"无法确认 {tag} 的首次构建归属")
    return target[1]


def _legacy_published_commit(tag, release):
    target = _read_tag_ref(tag)
    if release is None or release["draft"]:
        raise ReleaseError("旧轻量 tag 没有已公开 Release，不能据此创建或补写草稿")
    author = release.get("author")
    if (
        target is None or target[0] != "commit"
        or release.get("target_commitish") != target[1]
        or not isinstance(author, dict) or author.get("login") != "github-actions[bot]"
    ):
        raise ReleaseError("旧版已公开 Release 的轻量 tag 目标或作者无法核验")
    return target[1]


def _read_tag_object(tag, object_sha):
    obj = _json_record(run_gh(["api", f"repos/{{owner}}/{{repo}}/git/tags/{object_sha}"]), "tag 对象")
    message = obj.get("message")
    if not isinstance(message, str) or not message.startswith(PROVENANCE_PREFIX):
        raise ReleaseError("同名 tag 缺少首次构建归属记录")
    record = _json_record(message[len(PROVENANCE_PREFIX) :], "tag 构建归属记录")
    if (
        set(record) != {"sha", "digest", "generation", "repo", "ref", "path"}
        or not all(isinstance(value, str) for value in record.values())
        or not GIT_SHA_RE.fullmatch(record["sha"])
        or not re.fullmatch(r"[0-9a-f]{64}", record["digest"])
        or tag != f"rules-{record['digest'][:12]}"
        or record["path"] != WORKFLOW_PATH
        or (record["generation"] and not GITHUB_GENERATION_RE.fullmatch(record["generation"]))
        or obj.get("sha") != object_sha or obj.get("tag") != tag
        or not isinstance(obj.get("object"), dict) or obj["object"].get("type") != "commit"
        or obj["object"].get("sha") != record["sha"]
    ):
        raise ReleaseError("同名 tag 的归属记录与实际目标不一致")
    return record, obj


def _create_release_tag(tag, current):
    if current["generation"] and os.getenv("PUBLISH_GUARD"):
        ensure_publish_current()
    response = run_gh([
        "api", "repos/{owner}/{repo}/git/tags", "--method", "POST",
        "-f", f"tag={tag}", "-f", f"message={_provenance_message(current)}",
        "-f", f"object={current['sha']}", "-f", "type=commit",
    ])
    if response is None:
        raise ReleaseError("无法创建发布 tag 对象")
    object_sha = _json_record(response, "新建 tag 对象").get("sha")
    if not isinstance(object_sha, str) or not GIT_SHA_RE.fullmatch(object_sha):
        raise ReleaseError("新建 tag 对象 SHA 无法确认")
    if run_gh([
        "api", "repos/{owner}/{repo}/git/refs", "--method", "POST",
        "-f", f"ref=refs/tags/{tag}", "-f", f"sha={object_sha}",
    ]) is None:
        warning("  tag 引用创建未成功，回读实际目标并核验归属")
    actual_sha = _release_tag_ref(tag)
    if actual_sha is None:
        raise ReleaseError("新建 tag 引用无法确认")
    return actual_sha


def draft_provenance(tag, digest, release, *, create=False):
    current = _current_provenance(digest)
    object_sha = _release_tag_ref(tag)
    if object_sha is None:
        if release is not None:
            raise ReleaseError("已存在的草稿缺少可核验的 tag")
        if not create:
            return None, None
        object_sha = _create_release_tag(tag, current)
    record, _ = _read_tag_object(tag, object_sha)
    if any(record[key] != current[key] for key in ("repo", "ref", "digest")):
        raise ReleaseError("发布归属记录与当前仓库、分支或产品不一致")
    if release is None or release["draft"]:
        verify_original_build(record, current)
    if release is not None:
        body = release.get("body")
        author = release.get("author")
        if (
            not isinstance(body, str) or body.splitlines()[:1] != [_provenance_message(record)]
            or release.get("target_commitish") != record["sha"]
            or (record["generation"] and (
                not isinstance(author, dict) or author.get("login") != "github-actions[bot]"
            ))
        ):
            raise ReleaseError("草稿的首次构建归属与 tag 不一致")
    return record, object_sha


def _create_release_draft(tag, title, notes, record):
    if record["generation"] and os.getenv("PUBLISH_GUARD"):
        ensure_publish_current()
    result = run_gh([
        "api", "repos/{owner}/{repo}/releases", "--method", "POST",
        "-f", f"tag_name={tag}", "-f", f"target_commitish={record['sha']}",
        "-f", f"name={title}", "-f", f"body={_provenance_message(record)}\n\n{notes}",
        "-F", "draft=true", "-f", "make_latest=false",
        "--jq", _RELEASE_RECORD_JQ,
    ])
    if result is None:
        raise ReleaseError("无法创建 Release 草稿，保持 latest 和发布状态")
    draft = _release_record(result, tag, "新建 Release 状态")
    if not draft["draft"]:
        raise ReleaseError("新建 Release 未处于可核验的草稿状态")
    return draft


def cleanup_old_releases(current_tag, now_utc=None):
    """清理超过 KEEP_DAYS 的自动 Release 与孤立 tag；失败只告警，不影响已完成的发布。"""
    info(f"清理 {KEEP_DAYS} 天前的旧 Release...")
    releases_json = run_gh(["release", "list", "--limit", "200", "--json", "tagName,createdAt"])
    if not releases_json:
        return 0

    listing_valid = True
    try:
        releases = json.loads(releases_json)
    except (TypeError, ValueError) as e:
        # 清理失败只告警不标红（下轮运行自愈）
        warning(f"  旧 Release 清理失败: gh 返回的列表无法解析（{e}），本轮跳过清理")
        releases = []
        listing_valid = False
    if not isinstance(releases, list):
        warning("  旧 Release 清理失败: gh 返回的不是列表，本轮跳过清理")
        releases = []
        listing_valid = False

    cutoff_time = (now_utc or datetime.datetime.now(datetime.UTC)) - datetime.timedelta(days=KEEP_DAYS)
    release_tags = {
        rel.get("tagName")
        for rel in releases
        if isinstance(rel, dict) and isinstance(rel.get("tagName"), str)
    }
    can_reconcile_orphans = (
        listing_valid
        and len(releases) < 200
        and all(isinstance(rel, dict) and isinstance(rel.get("tagName"), str) for rel in releases)
    )

    cleaned = 0
    for rel in releases:
        try:
            created_at = datetime.datetime.fromisoformat(rel["createdAt"])
            if created_at.tzinfo is None:
                raise ValueError("Release 记录缺少时区")
            tag = rel["tagName"]
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            warning(f"  跳过无法解析的 Release 记录: {rel!r}（{e}）")
            continue
        if not isinstance(tag, str) or not RULES_TAG_RE.fullmatch(tag):
            continue
        if created_at < cutoff_time and tag != current_tag:
            info(f"  删除旧 Release: {tag}")
            if run_gh(["release", "delete", tag, "--yes"]) is None:
                warning(f"  删除 Release 失败，跳过: {tag}")
                continue
            if run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{tag}"]) is None:
                warning(f"  删除 tag 失败，跳过计数（Release 已删，tag 残留）: {tag}")
                continue
            cleaned += 1
    if can_reconcile_orphans:
        cleaned += cleanup_orphan_rules_tags(release_tags, current_tag, cutoff_time)
    elif listing_valid:
        warning("  Release 列表可能不完整，跳过孤立 tag 对账")
    if cleaned == 0:
        info("  无需清理")
    return cleaned


def _check_release_state(release, tag_object):
    ensure_publish_current()
    tag = release["tag_name"]
    if _find_release_by_id(release["id"], tag) != release:
        raise ReleaseError("目标 Release 已变化")
    if release["draft"]:
        actual = _release_tag_ref(tag)
    else:
        target = _read_tag_ref(tag)
        if target is None:
            raise ReleaseError("目标 tag 归属无法读取")
        actual = target[1] if target[0] == "tag" else _legacy_published_commit(tag, release)
        if target[0] == "tag":
            _read_tag_object(tag, actual)
    if actual != tag_object:
        raise ReleaseError("目标 tag 归属已变化")


def _upload_draft_asset(draft, tag_object, path):
    _check_release_state(draft, tag_object)
    upload_url = draft.get("upload_url")
    upload_pattern = rf"https://uploads\.github\.com/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/{draft['id']}/assets"
    if not isinstance(upload_url, str) or not re.fullmatch(upload_pattern + r"\{\?name,label\}", upload_url):
        raise ReleaseError("草稿的上传端点无法核验")
    endpoint = upload_url.removesuffix("{?name,label}")
    filename = os.path.basename(path)
    if run_gh([
        "api", f"{endpoint}?name={filename}", "--method", "POST",
        "-H", "Content-Type: application/octet-stream", "--input", path,
    ]) is None:
        raise ReleaseError("草稿资产上传失败，保留草稿供后续核验")


def prepare_release_plan(tag, digest):
    release = find_release(tag)
    if release is not None and not release["draft"]:
        try:
            record, tag_object = draft_provenance(tag, digest, release)
        except ReleaseError:
            # 旧流程没有 provenance；兼容仅限已公开对象，之后仍核全部实际字节。
            tag_object = _legacy_published_commit(tag, release)
            record = None
    else:
        record, tag_object = draft_provenance(tag, digest, release)
    assets = release_asset_records(release["id"]) if release is not None else {}
    if release is not None and not release["draft"]:
        reusable, why = verify_release_reusable(tag, tag.removeprefix("rules-"), records=assets)
        if not reusable:
            raise ReleaseError(f"已公开目标 Release 不可复用（{why}），禁止覆盖资产")
    return release, record, tag_object, assets


def _delete_incomplete_draft_asset(draft, tag_object, asset):
    _check_release_state(draft, tag_object)
    name = asset["name"]
    if release_asset_records(draft["id"]).get(name) != asset:
        raise ReleaseError(f"草稿资产 {name} 已变化，拒绝删除")
    asset_id = asset["id"]
    if run_gh(["api", f"repos/{{owner}}/{{repo}}/releases/assets/{asset_id}", "--method", "DELETE"]) is None:
        raise ReleaseError(f"无法清理未完成的草稿资产 {name}")


def _complete_draft(tag, release, tag_object, assets, paths):
    if extra := sorted(set(assets) - expected_assets(tag.removeprefix("rules-"))):
        raise ReleaseError(f"草稿多出资产 {extra}，拒绝修改")
    pending = []
    for path in paths:
        asset = assets.get(path)
        if asset is None:
            pending.append((path, None))
        elif asset["state"] == "uploaded":
            limit = MAX_SUMS_ASSET_BYTES if path == SHA256SUMS_NAME else MAX_ZIP_ASSET_BYTES
            remote_bytes = release_asset_bytes(tag, path, limit, asset_id=asset["id"])
            if remote_bytes is None or hashlib.sha256(remote_bytes).hexdigest() != file_sha256(path):
                raise ReleaseError(f"草稿资产 {path} 无法和本轮字节对账，拒绝替换")
        elif asset["state"] == "starter" and asset["size"] == 0 and asset["digest"] is None:
            # 502 失败留下的 starter 空资产可删除；open 可能仍在接收字节。
            pending.append((path, asset))
        elif asset["state"] != "open":
            raise ReleaseError(f"草稿资产 {path} 状态未知，拒绝替换")
    for path, asset in pending:
        if asset is not None:
            _delete_incomplete_draft_asset(release, tag_object, asset)
        _upload_draft_asset(release, tag_object, path)

    for attempt in range(ASSET_CONFIRM_ATTEMPTS):
        try:
            assets = release_asset_records(release["id"])
            reusable, why = verify_release_reusable(tag, tag.removeprefix("rules-"), records=assets)
            if any(asset["state"] == "open" for asset in assets.values()):
                why = "草稿资产仍处于上传中"
        except ReleaseError as exc:
            reusable, why = False, str(exc)
        if reusable and not any(asset["state"] == "open" for asset in assets.values()):
            break
        if reusable:
            why = "草稿资产仍处于上传中"
        if attempt + 1 < ASSET_CONFIRM_ATTEMPTS:
            time.sleep(ASSET_CONFIRM_DELAY)
    else:
        raise ReleaseError(f"草稿资产校验失败（{why}），停止公开")
    _check_release_state(release, tag_object)
    if release_asset_records(release["id"]) != assets:
        raise ReleaseError("公开前草稿资产记录已变化")
    endpoint = f"repos/{{owner}}/{{repo}}/releases/{release['id']}"
    raw = run_gh(["api", endpoint, "--method", "PATCH", "-F", "draft=false", "-f", "make_latest=false"])
    result = _json_record(raw, "草稿公开响应")
    if result.get("id") != release["id"] or result.get("tag_name") != tag or result.get("draft") is not False:
        raise ReleaseError("无法确认已公开草稿，保持原 latest 与状态")
    return assets


def process_release(tag, digest, plan, tag_date, tag_time):
    release, record, tag_object, assets = plan
    tag_id = tag.removeprefix("rules-")
    if release is None or release["draft"]:
        paths = []
        try:
            uploaded = {name for name, asset in assets.items() if asset["state"] == "uploaded"}
            if release is None or not expected_assets(tag_id) <= uploaded:
                zip_file, file_map = zip_target_files(tag_id)
                paths.append(zip_file)
                paths.append(write_sha256sums(zip_file, file_map))
            if release is None:
                if record is None:
                    record, tag_object = draft_provenance(tag, digest, None, create=True)
                notes = generate_release_notes(tag_date, tag_time, file_map)
                release = _create_release_draft(tag, f"Merged Rules - {tag_date} {tag_time}", notes, record)
                confirmed_record, confirmed_ref = draft_provenance(tag, digest, release)
                if confirmed_record != record or confirmed_ref != tag_object:
                    raise ReleaseError("新建草稿与原目标 tag 的归属不一致")
            assets = _complete_draft(tag, release, tag_object, assets, paths)
            release = {**release, "draft": False}
        finally:
            for path in paths:
                Path(path).unlink(missing_ok=True)
    release_id = release["id"]
    if release_asset_records(release_id) != assets:
        raise ReleaseError(f"设置 latest 前 Release {release_id} 的资产记录已变化")
    # 草稿公开或复用后，列表元数据仍可能与实际下载内容不同步。
    reusable, why = verify_release_reusable(tag, tag_id, records=assets)
    if not reusable:
        raise ReleaseError(f"设置 latest 前 Release {release_id} 的资产内容复核失败（{why}）")
    _check_release_state(release, tag_object)
    if release_asset_records(release_id) != assets:
        raise ReleaseError(f"设置 latest 前 Release {release_id} 的资产记录已变化")
    endpoint = f"repos/{{owner}}/{{repo}}/releases/{release_id}"
    if run_gh(["api", endpoint, "--method", "PATCH", "-f", "make_latest=true"]) is None:
        raise ReleaseError("无法将已公开 Release 标记为 latest")


def main():
    args = sys.argv[1:]
    if "--verify-only" in args:
        verify_only(require_baseline="--require-baseline" in args)
        return

    if "--print-hash" in sys.argv[1:]:
        print(combined_products_hash(*product_dirs(), format_tag=manifest.MANIFEST_MAGIC)[0])
        return

    txt_dir, mrs_dir = product_dirs()
    combined_hash, c1, c2 = combined_products_hash(txt_dir, mrs_dir, format_tag=manifest.MANIFEST_MAGIC)
    planning = "--needs-publish" in args
    if not planning:
        group_start("处理发布")
        section("产物校验")
        enforce_products(txt_dir, mrs_dir)
    try:
        if c1 != c2 or not c1:
            raise ReleaseError(f"产物数量不一致或为空: .txt={c1} 与 .mrs={c2}")
        digest = release_content_digest()
        release_tag = current_release_tag()
        plan = prepare_release_plan(release_tag, digest)
        force = os.getenv("FORCE_RELEASE", "").lower() in ("1", "true", "yes")
        publish, why = should_publish(combined_hash, load_last_hash(), CHANGE_DETECTION, force=force)
        if not publish:
            release = plan[0]
            publish = release is None or release["draft"] or latest_rules_release_tag() != release_tag
            if publish:
                why = "当前字节版本尚未成为 latest"
        if planning:
            print("true" if publish else "false")
            return
        info(f"目标发布标签: {release_tag}，{why} ({c1 + c2} 个文件)")
        if publish:
            now_bj = beijing_now()
            process_release(release_tag, digest, plan, now_bj.strftime("%Y-%m-%d"), now_bj.strftime("%H:%M:%S"))
            ensure_publish_current()
            if CHANGE_DETECTION:
                save_last_hash(combined_hash)
                info("  已保存当前内容哈希")
        ensure_publish_current()
        cleanup_old_releases(release_tag)
    except ReleaseError as exc:
        error(f"  {exc}")
        gh_error(str(exc))
        sys.exit(1)
    finally:
        if not planning:
            group_end()
    if not publish:
        return
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
    main()
