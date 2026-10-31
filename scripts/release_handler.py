import contextlib
import datetime
import hashlib
import io
import json
import os
import re
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
MAX_ZIP_ENTRIES = 10000
MAX_ZIP_ENTRY_SIZE = 64 * 1024 * 1024
MAX_ZIP_TOTAL_SIZE = 512 * 1024 * 1024
MAX_ZIP_ASSET_BYTES = 128 * 1024 * 1024
MAX_SUMS_ASSET_BYTES = 16 * 1024 * 1024
ZIP_READ_CHUNK = 1024 * 1024


def expected_assets(asset_id):
    return {f"merged-rules-{asset_id}.zip", SHA256SUMS_NAME}


def release_assets(release_tag):
    raw = run_gh(
        [
            "api",
            f"repos/{{owner}}/{{repo}}/releases/tags/{release_tag}",
            "--jq",
            '.assets[] | [.name, (.digest // "")] | @tsv',
        ]
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


def release_asset_bytes(release_tag, asset_name, max_bytes=None):
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


def verify_release_reusable(release_tag, tag_id):
    assets = release_assets(release_tag)
    if assets is None:
        return False, "无法读取远端资产列表"
    expected = expected_assets(tag_id)
    missing = sorted(expected - set(assets))
    if missing:
        return False, f"缺少资产 {missing}"

    zip_name = f"merged-rules-{tag_id}.zip"
    zip_digest = assets.get(zip_name, "").removeprefix("sha256:")
    sums_digest = assets.get(SHA256SUMS_NAME, "").removeprefix("sha256:")

    sums_bytes = release_sha256sums(release_tag)
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

    zip_bytes = release_asset_bytes(release_tag, zip_name, max_bytes=MAX_ZIP_ASSET_BYTES)
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
    expected_tag = f"rules-{release_id_for_hash(current_hash)}"
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
    for line in raw.splitlines():
        ref = line.strip()
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref.removeprefix("refs/tags/")
        if not RULES_TAG_RE.fullmatch(tag) or tag == current_tag or tag in release_tags:
            continue
        created_raw = run_gh(
            ["api", f"repos/{{owner}}/{{repo}}/commits/{tag}", "--jq", ".commit.committer.date"]
        )
        if not created_raw:
            warning(f"  无法确认孤立 tag 的提交年龄，跳过: {tag}")
            continue
        try:
            created_at = datetime.datetime.fromisoformat(created_raw.strip().replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as e:
            warning(f"  孤立 tag 年龄无法解析，跳过: {tag}（{e}）")
            continue
        if created_at.tzinfo is None:
            warning(f"  孤立 tag 年龄缺少时区，跳过: {tag}")
            continue
        if created_at >= cutoff_time:
            continue
        info(f"  删除孤立自动 tag: {tag}")
        if run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{tag}"]) is None:
            warning(f"  删除孤立 tag 失败，留待下轮重试: {tag}")
            continue
        cleaned += 1
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


def verify_products(txt_dir=None, mrs_dir=None, require_baseline=None):
    default_txt, default_mrs = product_dirs()
    txt_dir = txt_dir or default_txt
    mrs_dir = mrs_dir or default_mrs
    if require_baseline is None:
        require_baseline = baseline_required()

    validate_product_files(txt_dir, mrs_dir)
    baseline, expected = load_baseline()
    txt_files = sorted(manifest.collect_files(txt_dir, ".txt"))
    mrs_files = sorted(manifest.collect_files(mrs_dir, ".mrs"))
    merged_manifest = manifest.load_manifest(os.path.join(txt_dir, manifest.MANIFEST_NAME))

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
        expected_generation = manifest.expected_generation()
        if expected_generation and baseline.generation != expected_generation:
            raise manifest.ManifestError(
                f"清单 generation 与当前运行不一致: {baseline.generation} != {expected_generation}"
                "（疑似上一轮中断产物，请重新运行 main.py / merger.py）"
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
        print(combined_products_hash(*product_dirs(), format_tag=manifest.MANIFEST_MAGIC)[0])
        return

    if "--needs-publish" in sys.argv[1:]:
        current, _c1, _c2 = combined_products_hash(*product_dirs(), format_tag=manifest.MANIFEST_MAGIC)
        force = os.getenv("FORCE_RELEASE", "").lower() in ("1", "true", "yes")
        publish, _why = should_publish(current, load_last_hash(), CHANGE_DETECTION, force=force)
        if not publish:
            publish = needs_release_repair(current)
        print("true" if publish else "false")
        return

    group_start("处理发布")

    utc_now = datetime.datetime.now(datetime.UTC)
    now_bj = beijing_now()
    tag_date = now_bj.strftime("%Y-%m-%d")
    tag_time = now_bj.strftime("%H:%M:%S")

    section("产物校验")
    txt_dir, mrs_dir = product_dirs()
    combined_hash, c1, c2 = combined_products_hash(
        txt_dir, mrs_dir, format_tag=manifest.MANIFEST_MAGIC
    )
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
        if not publish and needs_release_repair(combined_hash):
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
    reusable = False
    if asset_names is not None and expected <= asset_names:
        reusable, why = verify_release_reusable(release_tag, tag_id)
        if reusable:
            info(f"  Release {release_tag} 已存在且资产摘要与当前内容一致，复用")
            if run_gh(["release", "edit", release_tag, "--latest"]) is None:
                error(f"  无法将 {release_tag} 标记为 latest，本轮发布失败")
                gh_error(f"无法将 {release_tag} 标记为 latest，下次运行将重试")
                sys.exit(1)
        else:
            info(f"  Release {release_tag} 资产不可复用（{why}），重新打包上传")
    if not reusable:
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

        asset_names = confirm_release_assets(
            release_tag,
            expected,
            content_check=lambda: verify_release_reusable(release_tag, tag_id),
        )
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
        listing_valid = True
        try:
            releases = json.loads(releases_json)
        except (TypeError, ValueError) as e:
            # 发布与哈希保存已完成，清理失败只告警不标红（下轮运行自愈）
            warning(f"  旧 Release 清理失败: gh 返回的列表无法解析（{e}），本轮跳过清理")
            releases = []
            listing_valid = False
        if not isinstance(releases, list):
            warning("  旧 Release 清理失败: gh 返回的不是列表，本轮跳过清理")
            releases = []
            listing_valid = False

        cutoff_time = utc_now - datetime.timedelta(days=KEEP_DAYS)
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
                created_at = datetime.datetime.fromisoformat(rel["createdAt"].replace("Z", "+00:00"))
                tag = rel["tagName"]
            except (KeyError, AttributeError, TypeError, ValueError) as e:
                warning(f"  跳过无法解析的 Release 记录: {rel!r}（{e}）")
                continue
            if not isinstance(tag, str) or not RULES_TAG_RE.fullmatch(tag):
                continue
            if created_at < cutoff_time and tag != release_tag:
                info(f"  删除旧 Release: {tag}")
                if run_gh(["release", "delete", tag, "--yes"]) is None:
                    warning(f"  删除 Release 失败，跳过: {tag}")
                    continue
                release_tags.discard(tag)
                if run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{tag}"]) is None:
                    warning(f"  删除 tag 失败，跳过计数（Release 已删，tag 残留）: {tag}")
                    continue
                cleaned += 1
        if can_reconcile_orphans:
            cleaned += cleanup_orphan_rules_tags(release_tags, release_tag, cutoff_time)
        elif listing_valid:
            warning("  Release 列表可能不完整，跳过孤立 tag 对账")
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
