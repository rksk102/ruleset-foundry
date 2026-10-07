import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import config_loader
from config_loader import get
from logger import error, group_end, group_start, info, success, warning
from utils import anchor_cwd_to_repo_root, clean_directory, file_sha256

SRC_ROOT = get("paths", "merged_output_dir", default="merged-rules")
DST_ROOT = get("paths", "mrs_output_dir", default="merged-rules-mrs")
REPO_API = get("mihomo", "repo_api",
               default="https://api.github.com/repos/MetaCubeX/mihomo/releases/latest")
PINNED_VERSION = (get("mihomo", "pinned_version", default="") or "").strip()
EXPECTED_SHA = (get("mihomo", "kernel_sha256", default="") or "").strip().lower()
KERNEL_CACHE_DIR = Path(get("mihomo", "kernel_cache_path", default=".cache/mihomo-kernel"))
KERNEL_BIN = str(KERNEL_CACHE_DIR / "mihomo")
VERSION_FILE = KERNEL_CACHE_DIR / "version.txt"
MAX_KERNEL_BYTES = 100 * 1024 * 1024
CONVERT_TIMEOUT = 120


def ensure_kernel_platform():
    if sys.platform.startswith("linux"):
        return
    error("  内核准备需要执行 Linux 版 mihomo（mihomo -v）做可运行性校验；"
          "请在 Linux/WSL 上运行，或交给 CI 的 kernel-bump 工作流")
    sys.exit(1)


def ensure_config_usable():
    if getattr(config_loader, "_HAS_YAML", True):
        return
    config_file = config_loader.resolve_config_file()
    if config_file is None or not Path(config_file).exists():
        return
    error(f"检测到 {config_file} 但 PyYAML 未安装，配置将被整份忽略；请先执行 pip install -r requirements.txt")
    sys.exit(1)


def expected_kernel_asset_name(tag_name):
    return f"mihomo-linux-amd64-{tag_name}.gz"


def release_api_url(pinned_version, repo_api=None):
    root = (repo_api or REPO_API).rstrip("/")
    if root.endswith("/latest"):
        root = root[: -len("/latest")]
    if pinned_version:
        return f"{root}/tags/{pinned_version}"
    return f"{root}/latest"


def select_kernel_asset(assets, tag_name):
    """按官方命名规则取 linux-amd64 压缩包；命名不符即返回 None（调用方 fail-closed）。"""
    if not tag_name:
        return None
    want = expected_kernel_asset_name(tag_name)
    for asset in assets:
        if asset["name"] == want:
            return {
                "name": asset["name"],
                "url": asset["browser_download_url"],
                "digest": asset.get("digest") or "",
            }
    return None


def verify_kernel_file(path, expected_sha, expected_magic=b"\x7fELF", require_sha=False):
    with open(path, "rb") as f:
        magic = f.read(len(expected_magic))
    if magic != expected_magic:
        raise ValueError(f"内核不是有效 ELF 文件（magic={magic!r}）")

    actual = file_sha256(path)
    if not expected_sha:
        if require_sha:
            raise ValueError(
                "缺少 kernel_sha256，拒绝以仅校验 ELF magic 的方式降级使用内核"
            )
        return actual
    if actual != expected_sha:
        raise ValueError(f"内核哈希不匹配：期望 {expected_sha}，实际 {actual}")
    return actual


def _fetch_latest_release_info(max_retries=3, pinned=None):
    version = PINNED_VERSION if pinned is None else pinned
    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(release_api_url(version, REPO_API))
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                warning(f"  获取 release 信息失败 [{attempt + 1}/{max_retries}]: {e}，重试中...")
                time.sleep(2 * (attempt + 1))
    raise last_err


def _download_kernel(download_url, expected_digest=None, max_retries=3):
    last_err = None
    for attempt in range(max_retries):
        try:
            dl_req = urllib.request.Request(download_url)
            with urllib.request.urlopen(dl_req, timeout=120) as dl_resp:
                digest = hashlib.sha256()
                buf = io.BytesIO()
                total = 0
                while True:
                    chunk = dl_resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_KERNEL_BYTES:
                        raise ValueError(
                            f"内核压缩包超过上限 {MAX_KERNEL_BYTES} 字节，已中止"
                        )
                    digest.update(chunk)
                    buf.write(chunk)

                actual_digest = digest.hexdigest()
                if not expected_digest:
                    raise ValueError(
                        "资产未提供 digest，拒绝在无压缩流校验的情况下使用该内核"
                    )
                want = expected_digest.split(":", 1)[-1].strip().lower()
                if actual_digest != want:
                    raise ValueError(
                        f"内核资产摘要不匹配：期望 {want}，实际 {actual_digest}"
                    )

                buf.seek(0)
                with gzip.GzipFile(fileobj=buf) as gz:
                    written = 0
                    with open(KERNEL_BIN, "wb") as f:
                        while True:
                            chunk = gz.read(65536)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > MAX_KERNEL_BYTES:
                                raise ValueError(
                                    f"内核解压后超过上限 {MAX_KERNEL_BYTES} 字节，已中止"
                                )
                            f.write(chunk)
            st = os.stat(KERNEL_BIN)
            os.chmod(KERNEL_BIN, st.st_mode | stat.S_IEXEC)
            return
        except Exception as e:
            last_err = e
            if os.path.exists(KERNEL_BIN):
                os.unlink(KERNEL_BIN)
            if attempt < max_retries - 1:
                warning(f"  下载内核失败 [{attempt + 1}/{max_retries}]: {e}，重试中...")
                time.sleep(2 * (attempt + 1))
    raise last_err


def _verify_kernel():
    try:
        ver_out = subprocess.check_output([KERNEL_BIN, "-v"], text=True, timeout=10)
        return ver_out.strip()
    except Exception:
        return None


def get_latest_mihomo(skip_hash_check=False):
    group_start("准备 Mihomo 内核")

    try:
        data = _fetch_latest_release_info()
        tag_name = data["tag_name"]
        info(f"  最新版本: {tag_name}")

        expected_sha = "" if skip_hash_check else EXPECTED_SHA

        if VERSION_FILE.exists():
            cached_ver = VERSION_FILE.read_text(encoding="utf-8").strip()
            if cached_ver == tag_name and os.path.exists(KERNEL_BIN):
                try:
                    verify_kernel_file(KERNEL_BIN, expected_sha, require_sha=True)
                except ValueError as e:
                    warning(f"  缓存内核校验失败，将重新下载: {e}")
                    with contextlib.suppress(OSError):
                        os.unlink(KERNEL_BIN)
                else:
                    ver_out = _verify_kernel()
                    if ver_out and "Mihomo" in ver_out:
                        info(f"  使用缓存内核 ({tag_name}): {ver_out}")
                        return
                    warning("  缓存内核不可运行，将重新下载")

        asset = select_kernel_asset(data["assets"], tag_name)
        if not asset:
            raise Exception(
                f"未找到 Linux 内核资产 {expected_kernel_asset_name(tag_name)}"
            )

        info(f"  下载内核: {asset['url']}")
        if not asset["digest"]:
            raise ValueError("上游未提供资产 digest，拒绝下载（无法锚定压缩流完整性）")
        KERNEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _download_kernel(asset["url"], expected_digest=asset["digest"])

        try:
            actual_sha = verify_kernel_file(KERNEL_BIN, expected_sha, require_sha=not skip_hash_check)
        except ValueError as e:
            error(f"  内核完整性校验失败: {e}")
            sys.exit(1)
        if not expected_sha:
            warning(f"  未配置 kernel_sha256，本次实际哈希: {actual_sha}")

        ver_out = _verify_kernel()
        if not ver_out or "Mihomo" not in ver_out:
            raise Exception("内核下载后验证失败（mihomo -v 输出异常）")

        info(f"  内核安装成功: {ver_out}")
        VERSION_FILE.write_text(tag_name, encoding="utf-8")

    except SystemExit:
        raise
    except Exception as e:
        error(f"  内核准备失败: {e}")
        if os.path.exists(KERNEL_BIN):
            warning("  尝试降级使用已缓存的内核...")
            try:
                verify_kernel_file(KERNEL_BIN, EXPECTED_SHA, require_sha=True)
            except (ValueError, OSError) as ve:
                error(f"  缓存内核校验失败，拒绝执行: {ve}")
                sys.exit(1)
            ver_out = _verify_kernel()
            if ver_out:
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
            error("  缓存内核也无法运行")
        sys.exit(1)
    finally:
        group_end()


def get_rule_type(path_parts):
    for part in path_parts:
        p = part.lower()
        if "domain" in p:
            return "domain"
        if "ip" in p and "cidr" in p:
            return "ipcidr"
        if "ip" in p:
            return "ipcidr"
    return None


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
    new_text, count = re.subn(rf'(\b{key}:)\s*(?:"[^"]*"|\S+)', rf'\1 "{value}"',
                              text, flags=re.M)
    if count != 1:
        error(f"  config.yaml 中 {key} 命中 {count} 次，拒绝写入")
        sys.exit(1)
    return new_text


def _smoke_convert():
    src = KERNEL_CACHE_DIR / "smoke-input.txt"
    dst = KERNEL_CACHE_DIR / "smoke-output.mrs"
    src.write_text("smoke-test.com\nexample.org\n", encoding="utf-8")
    produced = False
    try:
        subprocess.run(
            [KERNEL_BIN, "convert-ruleset", "domain", "text", str(src), str(dst)],
            check=True, capture_output=True, text=True, timeout=60,
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
    tag = data["tag_name"]

    cfg_path = Path("config.yaml")
    text = cfg_path.read_text(encoding="utf-8")
    current = re.search(r'pinned_version:\s*"([^"]*)"', text)
    if current and current.group(1) == tag:
        info(f"  已是最新正式版 {tag}，无需更新")
        return

    asset = select_kernel_asset(data["assets"], tag)
    if not asset:
        error(f"  未找到期望资产 {expected_kernel_asset_name(tag)}，拒绝改用启发式挑选")
        sys.exit(1)

    info(f"  下载并校验 {tag} ...")
    if not asset["digest"]:
        error("  上游未提供资产 digest，无法锚定压缩流完整性，拒绝继续")
        sys.exit(1)
    KERNEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _download_kernel(asset["url"], expected_digest=asset["digest"])

    try:
        verify_kernel_file(KERNEL_BIN, "")
    except ValueError as e:
        error(f"  内核结构校验失败: {e}")
        sys.exit(1)

    sha = file_sha256(KERNEL_BIN)
    ver_out = _verify_kernel()
    if not ver_out or "Mihomo" not in ver_out:
        error("  内核无法运行，拒绝写入配置")
        sys.exit(1)
    _smoke_convert()

    text = _set_config_field(text, "pinned_version", tag)
    text = _set_config_field(text, "kernel_sha256", sha)
    cfg_path.write_text(text, encoding="utf-8")
    info(f"  已更新 config.yaml: {tag} ({sha[:12]}...)")

    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"changed=true\ntag={tag}\n")


def main():
    ensure_kernel_platform()
    ensure_config_usable()

    if "--print-kernel-hash" in sys.argv:
        get_latest_mihomo(skip_hash_check=True)
        print(file_sha256(KERNEL_BIN))
        return

    if "--bump-config" in sys.argv:
        bump_config()
        return

    start_time = time.time()
    get_latest_mihomo()

    group_start(f"转换: {SRC_ROOT} -> {DST_ROOT}")

    for stale, why in clean_directory(DST_ROOT):
        warning(f"  清理失败（可能残留陈旧 .mrs）: {stale} -> {why}")

    if not os.path.exists(SRC_ROOT):
        error(f"源目录 {SRC_ROOT} 不存在！")
        sys.exit(1)

    files_map = []
    for root, _, files in os.walk(SRC_ROOT):
        for f in files:
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

        cmd = [KERNEL_BIN, "convert-ruleset", rule_type, "text", src_path, dst_path]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True,
                           timeout=CONVERT_TIMEOUT)
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
