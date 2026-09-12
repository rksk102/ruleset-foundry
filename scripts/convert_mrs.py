import gzip
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from config_loader import get
from logger import error, get_logger, group_end, group_start, info, success, warning
from utils import clean_directory

logger = get_logger()

SRC_ROOT = get("paths", "merged_output_dir", default="merged-rules")
DST_ROOT = get("paths", "mrs_output_dir", default="merged-rules-mrs")
REPO_API = get("mihomo", "repo_api",
               default="https://api.github.com/repos/MetaCubeX/mihomo/releases/latest")
PINNED_VERSION = (get("mihomo", "pinned_version", default="") or "").strip()
ASSET_NAME = (get("mihomo", "asset_name", default="") or "").strip()
EXPECTED_SHA = (get("mihomo", "kernel_sha256", default="") or "").strip().lower()
KERNEL_CACHE_DIR = Path(get("mihomo", "kernel_cache_path", default=".cache/mihomo-kernel"))
KERNEL_BIN = str(KERNEL_CACHE_DIR / "mihomo")
VERSION_FILE = KERNEL_CACHE_DIR / "version.txt"
MAX_KERNEL_BYTES = 100 * 1024 * 1024


def release_api_url(pinned_version, repo_api=None):
    """由 repo_api 派生 release 查询地址；pinned_version 为空时跟随 latest。"""
    root = (repo_api or REPO_API).rstrip("/")
    if root.endswith("/latest"):
        root = root[: -len("/latest")]
    if pinned_version:
        return f"{root}/tags/{pinned_version}"
    return f"{root}/latest"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def select_kernel_asset(assets, asset_name, pinned_version):
    """确定性地选择 linux-amd64 内核资产。

    1) asset_name 非空：精确匹配；
    2) 否则在候选 .gz 中排除 go1xx / v1 / v2 / v3 / compatible 变体；
    3) 再优先文件名形如 mihomo-linux-amd64-<tag>.gz 的默认构建。
    返回 browser_download_url，找不到返回 None。
    """
    gz = [a for a in assets if "linux-amd64" in a["name"] and a["name"].endswith(".gz")]
    if asset_name:
        for a in gz:
            if a["name"] == asset_name:
                return a["browser_download_url"]
        return None

    def is_variant(name):
        base = name[:-3]  # 去 .gz
        return (
            any(k in base for k in ("-go1", "-go2", "compatible"))
            or "-v1-" in base or "-v2-" in base or "-v3-" in base
            or base.endswith("-v1") or base.endswith("-v2") or base.endswith("-v3")
        )

    stable = [a for a in gz if not is_variant(a["name"])]
    if pinned_version:
        exact = f"mihomo-linux-amd64-{pinned_version}.gz"
        for a in stable:
            if a["name"] == exact:
                return a["browser_download_url"]
    if stable:
        return sorted(stable, key=lambda a: a["name"])[0]["browser_download_url"]
    return None


def verify_kernel_file(path, expected_sha, expected_magic=b"\x7fELF"):
    """校验内核结构，并在提供 expected_sha 时强制比对解压后二进制哈希。"""
    with open(path, "rb") as f:
        magic = f.read(len(expected_magic))
    if magic != expected_magic:
        raise ValueError(f"内核不是有效 ELF 文件（magic={magic!r}）")

    actual = sha256_file(path)
    if expected_sha and actual != expected_sha:
        raise ValueError(f"内核哈希不匹配：期望 {expected_sha}，实际 {actual}")
    return actual


def _fetch_latest_release_info(headers, max_retries=3):
    """获取 release 信息，带重试。"""
    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(release_api_url(PINNED_VERSION, REPO_API), headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                warning(f"  获取 release 信息失败 [{attempt + 1}/{max_retries}]: {e}，重试中...")
                time.sleep(2 * (attempt + 1))
    raise last_err


def _download_kernel(download_url, headers, max_retries=3):
    """下载内核到 KERNEL_BIN（覆盖旧文件），带重试。失败时清理残留。"""
    last_err = None
    for attempt in range(max_retries):
        try:
            dl_req = urllib.request.Request(download_url, headers=headers)
            with urllib.request.urlopen(dl_req, timeout=120) as dl_resp:
                with gzip.GzipFile(fileobj=dl_resp) as gz:
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
    """验证内核可运行，返回版本输出字符串；不可用返回 None。"""
    try:
        ver_out = subprocess.check_output([KERNEL_BIN, "-v"], text=True, timeout=10)
        return ver_out.strip()
    except Exception:
        return None


def get_latest_mihomo(skip_hash_check=False):
    group_start("准备 Mihomo 内核")

    try:
        headers = {}
        if "GH_TOKEN" in os.environ:
            headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"

        data = _fetch_latest_release_info(headers)
        tag_name = data["tag_name"]
        info(f"  最新版本: {tag_name}")

        expected_sha = "" if skip_hash_check else EXPECTED_SHA

        # 版本一致且缓存内核可用（结构+哈希均通过）时直接复用
        if VERSION_FILE.exists():
            cached_ver = VERSION_FILE.read_text().strip()
            if cached_ver == tag_name and os.path.exists(KERNEL_BIN):
                try:
                    verify_kernel_file(KERNEL_BIN, expected_sha)
                except ValueError as e:
                    warning(f"  缓存内核校验失败，将重新下载: {e}")
                    try:
                        os.unlink(KERNEL_BIN)
                    except OSError:
                        pass
                else:
                    ver_out = _verify_kernel()
                    if ver_out and "Mihomo" in ver_out:
                        info(f"  使用缓存内核 ({tag_name}): {ver_out}")
                        return
                    warning("  缓存内核不可运行，将重新下载")

        download_url = select_kernel_asset(data["assets"], ASSET_NAME, PINNED_VERSION)
        if not download_url:
            raise Exception(f"未找到合适的 linux-amd64 内核资源（asset_name={ASSET_NAME!r}）")

        info(f"  下载内核: {download_url}")
        KERNEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _download_kernel(download_url, headers)

        try:
            actual_sha = verify_kernel_file(KERNEL_BIN, expected_sha)
        except ValueError as e:
            # 安全场景 fail-fast：不得降级复用旧内核
            error(f"  内核完整性校验失败: {e}")
            sys.exit(1)
        if not expected_sha:
            warning(f"  未配置 kernel_sha256，本次实际哈希: {actual_sha}")

        ver_out = _verify_kernel()
        if not ver_out or "Mihomo" not in ver_out:
            raise Exception("内核下载后验证失败（mihomo -v 输出异常）")

        info(f"  内核安装成功: {ver_out}")
        VERSION_FILE.write_text(tag_name)

    except SystemExit:
        raise
    except Exception as e:
        error(f"  内核准备失败: {e}")
        # 降级：尝试使用已缓存的内核
        if os.path.exists(KERNEL_BIN):
            warning("  尝试降级使用已缓存的内核...")
            ver_out = _verify_kernel()
            if ver_out:
                warning(f"  使用缓存内核（版本可能非最新）: {ver_out}")
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
        with open(filepath, "r", encoding="utf-8") as f:
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

    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
        f.write("\n".join(markdown))


def main():
    if "--print-kernel-hash" in sys.argv:
        # 跳过哈希强校验，否则旧哈希未清时打印不出新值
        get_latest_mihomo(skip_hash_check=True)
        print(sha256_file(KERNEL_BIN))
        return

    start_time = time.time()
    get_latest_mihomo()

    group_start(f"转换: {SRC_ROOT} -> {DST_ROOT}")

    clean_directory(DST_ROOT)

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
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            success(f"  {prefix} {rel_path} -> MRS")
            stats["success"] += 1
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
    main()
