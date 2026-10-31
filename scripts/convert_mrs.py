import contextlib
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import config_loader
from config_loader import ConfigError, get
from logger import error, gh_error, group_end, group_start, info, success, warning
from utils import (
    anchor_cwd_to_repo_root,
    clean_directory,
    file_sha256,
    get_rule_type,
    is_repo_subdir,
    is_safe_component,
)

try:
    SRC_ROOT = get("paths", "merged_output_dir", default="merged-rules")
    DST_ROOT = get("paths", "mrs_output_dir", default="merged-rules-mrs")
    REPO_API = get("mihomo", "repo_api", default="https://api.github.com/repos/MetaCubeX/mihomo/releases/latest")
    PINNED_VERSION = (get("mihomo", "pinned_version", default="") or "").strip()
    EXPECTED_SHA = (get("mihomo", "kernel_sha256", default="") or "").strip().lower()
    EXPECTED_SHA_ARM64 = (get("mihomo", "kernel_sha256_arm64", default="") or "").strip().lower()
    KERNEL_CACHE_DIR = Path(get("mihomo", "kernel_cache_path", default=".cache/mihomo-kernel"))
except ConfigError as _cfg_err:
    gh_error(f"配置校验失败: {_cfg_err}")
    sys.exit(1)
KERNEL_BIN = str(KERNEL_CACHE_DIR / "mihomo")
VERSION_FILE = KERNEL_CACHE_DIR / "version.txt"
MAX_KERNEL_BYTES = 100 * 1024 * 1024
CONVERT_TIMEOUT = 120
RELEASE_TOTAL_TIMEOUT = 90
DOWNLOAD_TOTAL_TIMEOUT = 300
SUPPORTED_KERNEL_ARCHES = ("amd64", "arm64")
ELF_MACHINES = {"amd64": 0x3E, "arm64": 0xB7}


def normalize_kernel_arch(machine):
    normalized = (machine or "").strip().lower()
    if normalized in ("x86_64", "amd64"):
        return "amd64"
    if normalized in ("aarch64", "arm64"):
        return "arm64"
    return None


def current_kernel_arch():
    return normalize_kernel_arch(platform.machine())


def kernel_sha_key(arch):
    if arch == "amd64":
        return "kernel_sha256"
    if arch == "arm64":
        return "kernel_sha256_arm64"
    raise ValueError(f"不支持的内核架构: {arch!r}")


def expected_kernel_sha(arch):
    return EXPECTED_SHA if arch == "amd64" else EXPECTED_SHA_ARM64


def _kernel_cache_paths(arch):
    if arch not in SUPPORTED_KERNEL_ARCHES:
        raise ValueError(f"不支持的内核架构: {arch!r}")
    return KERNEL_CACHE_DIR / arch / "mihomo", KERNEL_CACHE_DIR / arch / "version.txt"


def ensure_kernel_platform():
    if not sys.platform.startswith("linux"):
        error(
            "  内核准备需要执行 Linux 版 mihomo（mihomo -v）做可运行性校验；"
            "请在 Linux/WSL 上运行，或交给 CI 的 kernel-bump 工作流"
        )
        sys.exit(1)
    arch = current_kernel_arch()
    if arch is None:
        error(f"  不支持的 Linux CPU 架构: {platform.machine()!r}；当前仅支持 x86_64/amd64 与 aarch64/arm64")
        sys.exit(1)
    return arch


def ensure_config_usable():
    if getattr(config_loader, "_HAS_YAML", True):
        return
    config_file = config_loader.resolve_config_file()
    if config_file is None or not Path(config_file).exists():
        return
    error(f"检测到 {config_file} 但 PyYAML 未安装，配置将被整份忽略；请先执行 uv sync")
    sys.exit(1)


def expected_kernel_asset_name(tag_name, arch="amd64"):
    if arch not in SUPPORTED_KERNEL_ARCHES:
        raise ValueError(f"不支持的内核架构: {arch!r}")
    return f"mihomo-linux-{arch}-{tag_name}.gz"


def release_api_url(pinned_version, repo_api=None):
    root = (repo_api or REPO_API).rstrip("/")
    if root.endswith("/latest"):
        root = root[: -len("/latest")]
    if pinned_version:
        return f"{root}/tags/{pinned_version}"
    return f"{root}/latest"


def _require_https(url, what):
    try:
        scheme = urlsplit(str(url)).scheme
    except ValueError:
        scheme = ""
    if scheme != "https":
        raise ValueError(f"{what}必须为 https://: {url!r}")


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_https(newurl, "重定向目标")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_HTTPS_OPENER = urllib.request.build_opener(_HttpsOnlyRedirectHandler())


def _open_url(req, timeout):
    return _HTTPS_OPENER.open(req, timeout=timeout)


def _remaining_time(deadline, operation):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{operation}超过总时限")
    return remaining


def _set_response_timeout(resp, deadline, operation):
    remaining = _remaining_time(deadline, operation)
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is None:
        # urllib 的 addinfourl 还会再包一层 HTTPResponse。
        raw = getattr(getattr(fp, "fp", None), "raw", None)
        sock = getattr(raw, "_sock", None)
    if sock is not None:
        sock.settimeout(remaining)


def select_kernel_asset(assets, tag_name, arch="amd64"):
    """按官方命名规则取指定架构压缩包；命名不符即返回 None（调用方 fail-closed）。"""
    if not tag_name:
        return None
    want = expected_kernel_asset_name(tag_name, arch)
    for asset in assets:
        if asset["name"] == want:
            return {
                "name": asset["name"],
                "url": asset["browser_download_url"],
                "digest": asset.get("digest") or "",
            }
    return None


def verify_kernel_file(path, expected_sha, expected_magic=b"\x7fELF", require_sha=False, expected_arch=None):
    with open(path, "rb") as f:
        header = f.read(20)
    magic = header[: len(expected_magic)]
    if magic != expected_magic:
        raise ValueError(f"内核不是有效 ELF 文件（magic={magic!r}）")
    if expected_arch is not None:
        if expected_arch not in ELF_MACHINES:
            raise ValueError(f"不支持的内核架构: {expected_arch!r}")
        if len(header) < 20:
            raise ValueError("ELF 头不完整，无法校验内核架构")
        actual_machine = struct.unpack_from("<H", header, 18)[0]
        expected_machine = ELF_MACHINES[expected_arch]
        if actual_machine != expected_machine:
            raise ValueError(
                f"内核架构不匹配：期望 {expected_arch} (e_machine={expected_machine:#x})，"
                f"实际 e_machine={actual_machine:#x}"
            )

    actual = file_sha256(path)
    if not expected_sha:
        if require_sha:
            raise ValueError("缺少 kernel_sha256，拒绝以仅校验 ELF magic 的方式降级使用内核")
        return actual
    if actual != expected_sha:
        raise ValueError(f"内核哈希不匹配：期望 {expected_sha}，实际 {actual}")
    return actual


def _fetch_latest_release_info(max_retries=3, pinned=None):
    version = PINNED_VERSION if pinned is None else pinned
    url = release_api_url(version, REPO_API)
    _require_https(url, "release API 地址")
    deadline = time.monotonic() + RELEASE_TOTAL_TIMEOUT
    last_err = None
    for attempt in range(max_retries):
        try:
            remaining = _remaining_time(deadline, "获取 release 信息")
            req = urllib.request.Request(url)
            with _open_url(req, timeout=min(30, remaining)) as resp:
                _require_https(getattr(resp, "url", url), "release API 响应")
                body = bytearray()
                while True:
                    _set_response_timeout(resp, deadline, "读取 release 信息")
                    chunk = resp.read(65536)
                    _remaining_time(deadline, "读取 release 信息")
                    if not chunk:
                        break
                    body.extend(chunk)
                return json.loads(body.decode("utf-8"))
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                warning(f"  获取 release 信息失败 [{attempt + 1}/{max_retries}]: {e}，重试中...")
                remaining = _remaining_time(deadline, "获取 release 信息")
                time.sleep(min(2 * (attempt + 1), remaining))
    raise last_err


def _download_kernel(download_url, expected_digest=None, max_retries=3, output_path=None):
    _require_https(download_url, "内核下载地址")
    if not isinstance(expected_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest):
        raise ValueError("资产 digest 必须为 sha256: 加 64 位小写十六进制字符")
    want = expected_digest.removeprefix("sha256:")
    deadline = time.monotonic() + DOWNLOAD_TOTAL_TIMEOUT
    last_err = None
    target_path = Path(output_path or KERNEL_BIN)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(max_retries):
        temp_path = None
        try:
            remaining = _remaining_time(deadline, "下载内核")
            fd, temp_name = tempfile.mkstemp(prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent)
            os.close(fd)
            temp_path = Path(temp_name)
            dl_req = urllib.request.Request(download_url)
            with _open_url(dl_req, timeout=min(120, remaining)) as dl_resp:
                _require_https(getattr(dl_resp, "url", download_url), "内核下载响应")
                digest = hashlib.sha256()
                buf = io.BytesIO()
                total = 0
                while True:
                    _set_response_timeout(dl_resp, deadline, "读取内核下载")
                    chunk = dl_resp.read(65536)
                    _remaining_time(deadline, "读取内核下载")
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_KERNEL_BYTES:
                        raise ValueError(f"内核压缩包超过上限 {MAX_KERNEL_BYTES} 字节，已中止")
                    digest.update(chunk)
                    buf.write(chunk)

                actual_digest = digest.hexdigest()
                if actual_digest != want:
                    raise ValueError(f"内核资产摘要不匹配：期望 {want}，实际 {actual_digest}")

                buf.seek(0)
                with gzip.GzipFile(fileobj=buf) as gz:
                    written = 0
                    with open(temp_path, "wb") as f:
                        while True:
                            _remaining_time(deadline, "解压内核")
                            chunk = gz.read(65536)
                            _remaining_time(deadline, "解压内核")
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > MAX_KERNEL_BYTES:
                                raise ValueError(f"内核解压后超过上限 {MAX_KERNEL_BYTES} 字节，已中止")
                            f.write(chunk)
            st = os.stat(temp_path)
            os.chmod(temp_path, st.st_mode | stat.S_IEXEC)
            os.replace(temp_path, target_path)
            temp_path = None
            return
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                warning(f"  下载内核失败 [{attempt + 1}/{max_retries}]: {e}，重试中...")
                remaining = _remaining_time(deadline, "下载内核")
                time.sleep(min(2 * (attempt + 1), remaining))
        finally:
            if temp_path is not None:
                with contextlib.suppress(OSError):
                    temp_path.unlink()
    raise last_err


def _verify_kernel(kernel_bin=None):
    kernel_bin = kernel_bin or KERNEL_BIN
    try:
        ver_out = subprocess.check_output([kernel_bin, "-v"], text=True, timeout=10)
        return ver_out.strip()
    except Exception:
        return None


def _atomic_write_text(path, text):
    path = Path(path)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink()


def _migrate_legacy_kernel_cache(kernel_bin, version_file):
    legacy_bin = KERNEL_CACHE_DIR / "mihomo"
    legacy_version = KERNEL_CACHE_DIR / "version.txt"
    if kernel_bin.exists() or version_file.exists() or not legacy_bin.is_file() or not legacy_version.is_file():
        return
    kernel_bin.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".mihomo-legacy-", dir=kernel_bin.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as target, open(legacy_bin, "rb") as source:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        os.chmod(temp_path, legacy_bin.stat().st_mode)
        os.replace(temp_path, kernel_bin)
        temp_path = None
        _atomic_write_text(version_file, legacy_version.read_text(encoding="utf-8"))
        info("  已将旧版内核缓存迁移到架构隔离目录")
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink()


def _cached_kernel_usable(tag_name, expected_sha, arch, require_sha=True):
    if not VERSION_FILE.exists() or not os.path.exists(KERNEL_BIN):
        return None
    cached_ver = VERSION_FILE.read_text(encoding="utf-8").strip()
    if cached_ver != tag_name:
        return None
    try:
        verify_kernel_file(KERNEL_BIN, expected_sha, require_sha=require_sha, expected_arch=arch)
    except (ValueError, OSError) as e:
        warning(f"  缓存内核校验失败，将重新下载: {e}")
        return None
    if VERSION_FILE.read_text(encoding="utf-8").strip() != cached_ver:
        warning("  缓存版本在校验期间变化，将重新下载")
        return None
    ver_out = _verify_kernel()
    if ver_out and "Mihomo" in ver_out and VERSION_FILE.read_text(encoding="utf-8").strip() == cached_ver:
        return ver_out
    warning("  缓存内核不可运行或版本发生变化，将重新下载")
    return None


def get_latest_mihomo(skip_hash_check=False):
    global KERNEL_BIN, VERSION_FILE

    group_start("准备 Mihomo 内核")
    arch = current_kernel_arch()
    kernel_bin, version_file = _kernel_cache_paths(arch)
    KERNEL_BIN = str(kernel_bin)
    VERSION_FILE = version_file
    expected_sha = "" if skip_hash_check else expected_kernel_sha(arch)
    published = False

    try:
        _migrate_legacy_kernel_cache(kernel_bin, version_file)
        if PINNED_VERSION and expected_sha and not skip_hash_check:
            cached = _cached_kernel_usable(PINNED_VERSION, expected_sha, arch)
            if cached:
                info(f"  使用缓存内核 ({PINNED_VERSION}): {cached}")
                return

        data = _fetch_latest_release_info()
        tag_name = data["tag_name"]
        info(f"  最新版本: {tag_name}")

        cached = _cached_kernel_usable(tag_name, expected_sha, arch, require_sha=not skip_hash_check)
        if cached:
            info(f"  使用缓存内核 ({tag_name}): {cached}")
            return

        asset = select_kernel_asset(data["assets"], tag_name, arch)
        if not asset:
            raise Exception(f"未找到 Linux 内核资产 {expected_kernel_asset_name(tag_name, arch)}")

        info(f"  下载内核: {asset['url']}")
        if not asset["digest"]:
            raise ValueError("上游未提供资产 digest，拒绝下载（无法锚定压缩流完整性）")
        kernel_bin.parent.mkdir(parents=True, exist_ok=True)
        fd, candidate_name = tempfile.mkstemp(prefix=f".mihomo-{arch}.candidate-", dir=kernel_bin.parent)
        os.close(fd)
        candidate = Path(candidate_name)
        try:
            _download_kernel(asset["url"], expected_digest=asset["digest"], output_path=candidate)
            try:
                actual_sha = verify_kernel_file(
                    candidate, expected_sha, require_sha=not skip_hash_check, expected_arch=arch
                )
            except ValueError as e:
                error(f"  内核完整性校验失败: {e}")
                sys.exit(1)
            if not expected_sha:
                warning(f"  未配置 kernel_sha256，本次实际哈希: {actual_sha}")

            ver_out = _verify_kernel(str(candidate))
            if not ver_out or "Mihomo" not in ver_out:
                raise Exception("内核下载后验证失败（mihomo -v 输出异常）")

            os.replace(candidate, KERNEL_BIN)
            published = True
            _atomic_write_text(VERSION_FILE, tag_name)
            info(f"  内核安装成功: {ver_out}")
        finally:
            with contextlib.suppress(OSError):
                candidate.unlink()

    except SystemExit:
        raise
    except Exception as e:
        error(f"  内核准备失败: {e}")
        if published:
            error("  新内核已发布但版本元数据更新失败，拒绝将其当作旧缓存降级")
            sys.exit(1)
        if os.path.exists(KERNEL_BIN):
            warning("  尝试降级使用已缓存的内核...")
            try:
                verify_kernel_file(KERNEL_BIN, expected_kernel_sha(arch), require_sha=True, expected_arch=arch)
            except (ValueError, OSError) as ve:
                error(f"  缓存内核校验失败，拒绝执行: {ve}")
                sys.exit(1)
            ver_out = _verify_kernel()
            if ver_out and "Mihomo" in ver_out:
                warning(f"  使用缓存内核（版本可能非最新）: {ver_out}")
                summary_path = os.getenv("GITHUB_STEP_SUMMARY")
                if summary_path:
                    with open(summary_path, "a", encoding="utf-8") as f:
                        f.write(
                            f"\n> ⚠️ 本次降级使用缓存内核"
                            f"（目标 {PINNED_VERSION or 'latest'} 准备失败）: "
                            f"{type(e).__name__}: {e}\n"
                        )
                return
            error("  缓存内核也无法运行或版本输出异常")
        sys.exit(1)
    finally:
        group_end()


def has_valid_content(filepath):
    try:
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                content = line.strip()
                if content and not content.startswith("#"):
                    return True
        return False
    except Exception:
        return False


def write_summary(stats, total_time):
    if "GITHUB_STEP_SUMMARY" not in os.environ:
        return

    is_failed = stats["failed"] > 0
    status_text = "失败" if is_failed else "成功"

    markdown = [
        "\n### MRS 转换报告",
        f"**结果**: {status_text} (耗时: {total_time:.2f}s)",
        "",
        "| 指标 | 计数 |",
        "| :--- | :--- |",
        f"| 成功 | {stats['success']} |",
        f"| 失败 | **{stats['failed']}** |",
        f"| 跳过 | {stats['skipped']} |",
        f"| 总数 | {stats['total']} |",
        "",
    ]
    if is_failed:
        markdown.append("**错误**: 部分文件转换失败，请检查上方日志。")

    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
        f.write("\n".join(markdown))


def _set_config_field(text, key, value):
    new_text, count = re.subn(rf'(\b{key}:)\s*(?:"[^"]*"|\S+)', rf'\1 "{value}"', text, flags=re.M)
    if count != 1:
        error(f"  config.yaml 中 {key} 命中 {count} 次，拒绝写入")
        sys.exit(1)
    return new_text


def _smoke_convert(kernel_bin=None):
    kernel_bin = kernel_bin or KERNEL_BIN
    src = KERNEL_CACHE_DIR / "smoke-input.txt"
    dst = KERNEL_CACHE_DIR / "smoke-output.mrs"
    with contextlib.suppress(FileNotFoundError):
        dst.unlink()
    src.write_text("smoke-test.com\nexample.org\n", encoding="utf-8")
    produced = False
    try:
        subprocess.run(
            [kernel_bin, "convert-ruleset", "domain", "text", str(src), str(dst)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        produced = dst.exists() and dst.stat().st_size > 0
    except subprocess.CalledProcessError as e:
        error(f"  内核冒烟转换失败: {(e.stderr or '').strip()}")
        sys.exit(1)
    finally:
        for path in (src, dst):
            if path.exists():
                path.unlink()

    if not produced:
        error("  内核冒烟转换未产出有效文件")
        sys.exit(1)


def bump_config():
    data = _fetch_latest_release_info(pinned="")
    tag = data.get("tag_name", "")
    if not is_safe_component(tag):
        error(f"  远端 tag 不是安全组件名，拒绝写入: {tag!r}")
        gh_error(f"远端 tag 不是安全组件名，拒绝写入: {tag!r}")
        sys.exit(1)

    cfg_path = Path("config.yaml")
    text = cfg_path.read_text(encoding="utf-8")
    current = re.search(r'pinned_version:\s*"([^"]*)"', text)
    if current and current.group(1) == tag:
        info(f"  已是最新正式版 {tag}，无需更新")
        return

    KERNEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    candidates = {}
    hashes = {}
    selected = {}
    for arch in SUPPORTED_KERNEL_ARCHES:
        asset = select_kernel_asset(data["assets"], tag, arch)
        if not asset:
            error(f"  未找到期望资产 {expected_kernel_asset_name(tag, arch)}，拒绝改用启发式挑选")
            sys.exit(1)
        if not asset["digest"]:
            error(f"  {arch} 资产未提供 digest，无法锚定压缩流完整性，拒绝继续")
            sys.exit(1)
        selected[arch] = asset

    for arch in SUPPORTED_KERNEL_ARCHES:
        asset = selected[arch]
        info(f"  下载并校验 {tag} ({arch}) ...")
        candidate = KERNEL_CACHE_DIR / f"mihomo-{arch}"
        _download_kernel(asset["url"], expected_digest=asset["digest"], output_path=candidate)
        try:
            verify_kernel_file(candidate, "", require_sha=False, expected_arch=arch)
        except ValueError as e:
            error(f"  {arch} 内核结构校验失败: {e}")
            sys.exit(1)
        candidates[arch] = candidate
        hashes[arch] = file_sha256(candidate)

    current_arch = current_kernel_arch()
    candidate = candidates.get(current_arch)
    if candidate is None:
        error(f"  当前架构 {current_arch!r} 无可执行内核，拒绝写入配置")
        sys.exit(1)
    ver_out = _verify_kernel(candidate)
    if not ver_out or "Mihomo" not in ver_out:
        error("  内核无法运行，拒绝写入配置")
        sys.exit(1)
    _smoke_convert(candidate)

    text = _set_config_field(text, "pinned_version", tag)
    text = _set_config_field(text, "kernel_sha256", hashes["amd64"])
    text = _set_config_field(text, "kernel_sha256_arm64", hashes["arm64"])
    cfg_path.write_text(text, encoding="utf-8")
    info(f"  已更新 config.yaml: {tag} (amd64 {hashes['amd64'][:12]}..., arm64 {hashes['arm64'][:12]}...)")

    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"changed=true\ntag={tag}\n")


def ensure_products_anchored():
    for label, path in (("源目录", SRC_ROOT), ("输出目录", DST_ROOT)):
        if not is_repo_subdir(path):
            target = Path(path).resolve()
            error(f"  {label}不在仓库内或等于仓库根，拒绝继续: {target}")
            gh_error(f"{label}不在仓库内或等于仓库根，拒绝继续: {target}")
            sys.exit(1)


def main():
    ensure_kernel_platform()
    ensure_config_usable()

    if "--print-kernel-hash" in sys.argv:
        get_latest_mihomo(skip_hash_check=True)
        print(file_sha256(KERNEL_BIN))
        return

    if "--verify-current-kernel" in sys.argv:
        get_latest_mihomo()
        ver_out = _verify_kernel()
        if not ver_out or "Mihomo" not in ver_out:
            error("  当前架构内核版本校验失败")
            sys.exit(1)
        _smoke_convert()
        return

    if "--bump-config" in sys.argv:
        bump_config()
        return

    start_time = time.time()
    ensure_products_anchored()
    get_latest_mihomo()

    group_start(f"转换: {SRC_ROOT} -> {DST_ROOT}")

    for stale, why in clean_directory(DST_ROOT):
        warning(f"  清理失败（可能残留陈旧 .mrs）: {stale} -> {why}")

    if not os.path.exists(SRC_ROOT):
        error(f"源目录 {SRC_ROOT} 不存在！")
        sys.exit(1)

    files_map = []
    for root, dirs, files in os.walk(SRC_ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            if f.endswith(".txt"):
                files_map.append(os.path.join(root, f))

    total_files = len(files_map)
    stats = {"success": 0, "failed": 0, "skipped": 0, "total": total_files}
    info(f"  发现 {total_files} 个文本规则文件")

    for idx, src_path in enumerate(files_map, 1):
        rel_path = os.path.relpath(src_path, SRC_ROOT)
        prefix = f"[{idx}/{total_files}]"

        path_parts = rel_path.split(os.sep)
        rule_type = get_rule_type(path_parts)

        dst_rel = os.path.splitext(rel_path)[0] + ".mrs"
        dst_path = os.path.join(DST_ROOT, dst_rel)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)

        if not rule_type:
            warning(f"  {prefix} 跳过: {rel_path} (未知类型)")
            stats["skipped"] += 1
            continue

        if not has_valid_content(src_path):
            warning(f"  {prefix} 跳过: {rel_path} (无有效规则)")
            stats["skipped"] += 1
            continue

        temp_path = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=".convert-", suffix=".mrs", dir=os.path.dirname(dst_path))
            os.close(fd)
            temp_path = Path(temp_name)
            temp_path.unlink()
            cmd = [KERNEL_BIN, "convert-ruleset", rule_type, "text", src_path, str(temp_path)]
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=CONVERT_TIMEOUT)
            try:
                output_stat = os.lstat(temp_path)
                output_valid = stat.S_ISREG(output_stat.st_mode) and output_stat.st_size > 0
            except OSError:
                output_valid = False
            if not output_valid:
                error(f"  {prefix} {rel_path}")
                error("      L 转换未产出非空普通文件")
                stats["failed"] += 1
                continue
            os.replace(temp_path, dst_path)
            temp_path = None
            success(f"  {prefix} {rel_path} -> MRS")
            stats["success"] += 1
        except subprocess.TimeoutExpired:
            error(f"  {prefix} {rel_path}")
            error(f"      L 转换超时（>{CONVERT_TIMEOUT}s）")
            stats["failed"] += 1
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.strip() if e.stderr else "未知错误"
            error(f"  {prefix} {rel_path}")
            error(f"      L {err_msg}")
            stats["failed"] += 1
        except OSError as e:
            error(f"  {prefix} {rel_path}")
            error(f"      L 输出文件处理失败: {e}")
            stats["failed"] += 1
        finally:
            if temp_path is not None:
                with contextlib.suppress(OSError):
                    temp_path.unlink()

    group_end()

    duration = time.time() - start_time
    write_summary(stats, duration)

    if stats["failed"] > 0:
        error(f"转换失败！ {stats['failed']} 个文件无法转换")
        sys.exit(1)
    else:
        success(f"转换完成 ({stats['success']} 成功, {stats['skipped']} 跳过)")
        sys.exit(0)


if __name__ == "__main__":
    anchor_cwd_to_repo_root()
    main()
