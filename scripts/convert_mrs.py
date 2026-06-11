"""
阶段③ MRS 二进制转换 — 将 .txt 规则转为 Mihomo 专用 .mrs 格式。

主要改进：
- 统一日志模块
- Mihomo 内核缓存到 .cache/mihomo-kernel/，仅在版本变化时更新
"""
import os
import sys
import shutil
import subprocess
import stat
import gzip
import time
import json
import urllib.request
import urllib.error
from pathlib import Path

from logger import info, success, warning, error, group_start, group_end, get_logger
from config_loader import get

logger = get_logger()

SRC_ROOT = get("paths", "merged_output_dir", default="merged-rules")
DST_ROOT = get("paths", "mrs_output_dir", default="merged-rules-mrs")
REPO_API = get("mihomo", "repo_api",
               default="https://api.github.com/repos/MetaCubeX/mihomo/releases/latest")
KERNEL_CACHE_DIR = Path(get("mihomo", "kernel_cache_path", default=".cache/mihomo-kernel"))
KERNEL_BIN = str(KERNEL_CACHE_DIR / "mihomo")
VERSION_FILE = KERNEL_CACHE_DIR / "version.txt"


def get_latest_mihomo():
    """获取/更新 Mihomo 内核（带缓存）"""
    group_start("🔧 准备 Mihomo 内核")

    headers = {}
    if "GH_TOKEN" in os.environ:
        headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"

    try:
        req = urllib.request.Request(REPO_API, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag_name = data["tag_name"]
        info(f"  最新版本: {tag_name}")

        # 检查缓存
        if KERNEL_CACHE_DIR.exists() and VERSION_FILE.exists():
            cached_ver = VERSION_FILE.read_text().strip()
            if cached_ver == tag_name and os.path.exists(KERNEL_BIN):
                info(f"  ✅ 使用缓存内核 ({tag_name})")
                group_end()
                return

        # 下载新内核
        download_url = None
        for asset in data["assets"]:
            if ("linux-amd64" in asset["name"]
                    and "compatible" not in asset["name"]
                    and asset["name"].endswith(".gz")):
                download_url = asset["browser_download_url"]
                break

        if not download_url:
            raise Exception("未找到合适的 linux-amd64 内核资源")

        info(f"  下载内核: {download_url}")
        KERNEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        dl_req = urllib.request.Request(download_url, headers=headers)
        with urllib.request.urlopen(dl_req, timeout=120) as dl_resp:
            with gzip.GzipFile(fileobj=dl_resp) as gz:
                with open(KERNEL_BIN, "wb") as f:
                    shutil.copyfileobj(gz, f)

        # 可执行权限
        st = os.stat(KERNEL_BIN)
        os.chmod(KERNEL_BIN, st.st_mode | stat.S_IEXEC)

        ver_out = subprocess.check_output([KERNEL_BIN, "-v"], text=True)
        info(f"  内核安装成功: {ver_out.strip()}")

        # 记录版本
        VERSION_FILE.write_text(tag_name)

    except Exception as e:
        error(f"  内核准备失败: {e}")
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
    """检查文件是否包含有效规则"""
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
    status_icon = "❌" if is_failed else "✅"
    status_text = "失败" if is_failed else "成功"

    markdown = [
        "### 🍭 MRS 转换报告",
        f"**结果**: {status_icon} {status_text} (耗时: {total_time:.2f}s)",
        "",
        "| 指标 | 计数 |",
        "| :--- | :--- |",
        f"| 🟢 **成功** | {stats['success']} |",
        f"| 🔴 **失败** | **{stats['failed']}** |",
        f"| 🟡 **跳过** | {stats['skipped']} |",
        f"| 📦 **总数** | {stats['total']} |",
        "",
    ]
    if is_failed:
        markdown.append("⚠️ **严重错误**: 部分文件转换失败，请检查上方日志。")

    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
        f.write("\n".join(markdown))


def main():
    start_time = time.time()
    get_latest_mihomo()

    group_start(f"🔄 转换: {SRC_ROOT} → {DST_ROOT}")

    # 清空目标目录
    if os.path.exists(DST_ROOT):
        for item in os.listdir(DST_ROOT):
            item_path = os.path.join(DST_ROOT, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception:
                pass
    else:
        os.makedirs(DST_ROOT)

    if not os.path.exists(SRC_ROOT):
        error(f"源目录 {SRC_ROOT} 不存在！")
        sys.exit(1)

    # 收集所有 .txt 文件
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
            success(f"  {prefix} {rel_path} → MRS")
            stats["success"] += 1
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.strip() if e.stderr else "未知错误"
            error(f"  {prefix} {rel_path}")
            error(f"      └ {err_msg}")
            stats["failed"] += 1

    group_end()

    duration = time.time() - start_time
    write_summary(stats, duration)

    if stats["failed"] > 0:
        error(f"❌ 转换失败！ {stats['failed']} 个文件无法转换")
        sys.exit(1)
    else:
        success(f"🎉 转换完成 ({stats['success']} 成功, {stats['skipped']} 跳过)")
        sys.exit(0)


if __name__ == "__main__":
    main()
