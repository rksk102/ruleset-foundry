"""
阶段① 规则同步引擎 — 并发下载 + ETag 增量更新 + 部分降级模式。

主要改进：
- 使用 asyncio + aiohttp 并发下载（速度提升 5-10x）
- ETag / Last-Modified 检测，仅在远程有更新时才下载
- 部分降级：单个源失败不阻断整体流程
- 统一日志模块
"""
import os
import sys
import re
import asyncio
import aiohttp
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone

import processor
from logger import info, success, warning, error, debug, group_start, group_end, section, gh_error, get_logger
from config_loader import get
from utils import (
    normalize_policy,
    normalize_type,
    get_owner_from_url,
    get_cached_headers,
    update_etag_cache,
    atomic_write,
)

logger = get_logger()

# ---------- 常量 ----------
SOURCES_FILE = get("paths", "sources_file", default="sources.urls")
RULESETS_DIR = Path(get("paths", "rulesets_dir", default="rulesets"))
TIMEOUT = get("network", "timeout_seconds", default=15)
RETRIES = get("network", "max_retries", default=2)
STRICT_MODE = get("behavior", "strict_mode", default=False)


def build_filepath(task):
    """根据任务信息构建输出文件路径（相对路径 + 绝对路径 + 文件名）"""
    owner = get_owner_from_url(task["url"])
    filename = task["url"].split("/")[-1].split(".")[0] + ".txt"
    rel_path = Path(task["policy"]) / task["type"] / owner / filename
    abs_path = RULESETS_DIR / rel_path
    return owner, filename, rel_path, abs_path


# ---------- 统计 ----------
class SyncStats:
    def __init__(self):
        self.success = 0
        self.skipped = 0
        self.download_errors = []  # (url, reason)
        self.parse_errors = []     # (url, reason)
        self.total_lines = 0
        self.start_time = time.time()
        self.etag_hits = []

    def elapsed(self):
        return f"{time.time() - self.start_time:.1f}s"


stats = SyncStats()


# ---------- 解析 sources.urls ----------

def parse_sources():
    """解析 sources.urls 文件，返回任务列表"""
    tasks = []
    current_policy = "policy"
    current_type = "domain"

    if not os.path.exists(SOURCES_FILE):
        gh_error(f"文件 {SOURCES_FILE} 未找到！")
        sys.exit(1)

    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        content = f.read().lstrip("\ufeff")

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        m_pol = re.match(r"^\[policy:(.+)\]$", line)
        if m_pol:
            current_policy = normalize_policy(m_pol.group(1))
            continue

        m_type = re.match(r"^\[type:(.+)\]$", line)
        if m_type:
            current_type = normalize_type(m_type.group(1))
            continue

        url_match = re.search(r"https?://\S+", line)
        if url_match:
            tasks.append({
                "policy": current_policy,
                "type": current_type,
                "url": url_match.group(0),
            })

    return tasks


# ---------- 并发下载器 ----------

async def download_one(session, task):
    """下载单个源（带 ETag 检测 + 重试）"""
    url = task["url"]
    cached_headers = get_cached_headers(url)

    for attempt in range(RETRIES + 1):
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                headers=cached_headers,
            ) as resp:
                if resp.status == 304:
                    debug(f"  ETag 命中(未变化): {url}")
                    stats.etag_hits.append(url)
                    return (task, None, True)

                if resp.status == 200:
                    content = await resp.read()
                    update_etag_cache(url, resp)
                    return (task, content, False)

                if attempt == RETRIES:
                    return (task, None, False)
                await asyncio.sleep(1 * (attempt + 1))

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == RETRIES:
                warning(f"  下载失败 [{attempt+1}/{RETRIES+1}]: {url} → {e}")
                return (task, None, False)
            await asyncio.sleep(2 * (attempt + 1))

    return (task, None, False)


async def download_all(tasks):
    """并发下载所有源"""
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        coros = [download_one(session, t) for t in tasks]
        results = await asyncio.gather(*coros)
    return results


# ---------- 处理单个任务 ----------

def process_task(task, raw_bytes, is_cached):
    """处理单个源：解码 → 解析 → 清洗 → 原子写入"""
    owner, filename, rel_path, abs_path = build_filepath(task)

    content_str = processor.safe_decode(raw_bytes)
    lines = processor.parse_lines(content_str)

    if task["type"] == "ipcidr":
        result = processor.process_ip(lines)
    else:
        result = processor.process_domain(lines)

    abs_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(str(abs_path), result)

    return len(result), str(abs_path)


# ---------- 清理孤儿文件 ----------

def clean_orphans(expected_files):
    """清理不再需要的文件"""
    group_start("🧹 清理孤儿文件")
    if not RULESETS_DIR.exists():
        group_end()
        return

    actual_files = set(str(p) for p in RULESETS_DIR.rglob("*.txt"))
    expected_set = set(str(f) for f in expected_files)

    removed = 0
    for f in actual_files:
        if f not in expected_set:
            os.remove(f)
            debug(f"  已删除: {f}")
            removed += 1

    for dirpath, _, _ in os.walk(RULESETS_DIR, topdown=False):
        if not os.listdir(dirpath):
            os.rmdir(dirpath)

    info(f"  共清理 {removed} 个孤儿文件")
    group_end()


# ---------- Summary 生成 ----------

def generate_summary():
    """生成 GitHub Actions Summary"""
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    dl_fail = len(stats.download_errors)
    parse_fail = len(stats.parse_errors)
    total_fail = dl_fail + parse_fail

    section(f"📊 同步报告 | 成功:{stats.success} 缓存:{stats.skipped} 失败:{total_fail} | 耗时:{stats.elapsed()}")

    if stats.download_errors:
        warning(f"  ⚠ 下载失败 ({dl_fail}):")
        for url, reason in stats.download_errors:
            warning(f"    └ {url} | {reason}")

    if stats.parse_errors:
        warning(f"  ⚠ 解析失败 ({parse_fail}):")
        for url, reason in stats.parse_errors:
            warning(f"    └ {url} | {reason}")

    if not summary_path:
        return

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("# 🛡️ 规则同步仪表盘\n\n")
        f.write("| 🟢 成功 | 🟡 缓存命中 | 🔴 失败 | 📉 总规则数 |\n")
        f.write("| :---: | :---: | :---: | :---: |\n")
        f.write(f"| **{stats.success}** | **{stats.skipped}** | **{total_fail}** | **{stats.total_lines}** |\n\n")

        if dl_fail > 0:
            f.write("### 📡 下载失败详情\n\n| URL | 原因 |\n| :--- | :--- |\n")
            for url, reason in stats.download_errors:
                f.write(f"| `{url}` | {reason} |\n")
            f.write("\n")

        if parse_fail > 0:
            f.write("### 🧠 解析失败详情\n\n| URL | 原因 |\n| :--- | :--- |\n")
            for url, reason in stats.parse_errors:
                f.write(f"| `{url}` | {reason} |\n")
            f.write("\n")

        if total_fail == 0:
            f.write("### ✅ 全部源同步正常\n\n")

        f.write(f"\n_生成时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")


# ---------- Git 提交 ----------

def git_push():
    """执行 Git 提交"""
    group_start("💾 Git 提交")

    def run_cmd(args):
        subprocess.run(args, check=False)

    run_cmd(["git", "config", "user.name", "github-actions[bot]"])
    run_cmd(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"])
    run_cmd(["git", "add", "rulesets/"])

    res = subprocess.run(["git", "diff-index", "--quiet", "HEAD"], check=False)
    if res.returncode == 0:
        info("  无变更，跳过提交")
    else:
        info("  推送变更中...")
        msg = f"chore(sync): Rules update {datetime.now().strftime('%Y-%m-%d')}"
        run_cmd(["git", "commit", "-m", msg])
        run_cmd(["git", "push"])

    group_end()


# ---------- 主入口 ----------

def main():
    group_start("🔧 初始化")
    info(f"  超时:{TIMEOUT}s | 重试:{RETRIES}次 | 严格模式:{'开' if STRICT_MODE else '关'}")
    tasks = parse_sources()
    info(f"  加载 {len(tasks)} 个上游源")
    group_end()

    # 并发下载
    group_start(f"🌐 并发下载 ({len(tasks)} 源)")
    results = asyncio.run(download_all(tasks))
    info(f"  下载完成 | ETag 命中: {len(stats.etag_hits)}")
    group_end()

    # 逐个处理
    expected_files = []

    for task, raw_bytes, is_cached in results:
        owner, filename, rel_path, abs_path = build_filepath(task)
        expected_files.append(abs_path)

        label = f"[{task['policy']}/{task['type']}] {owner}/{filename}"

        # ETag 命中 → 跳过
        if is_cached:
            stats.skipped += 1
            debug(f"  ⏭ 跳过(未变化): {label}")
            continue

        # 下载失败 → 记录但不阻断
        if raw_bytes is None:
            stats.download_errors.append((url, "下载失败"))
            warning(f"  ⚠ 下载失败: {label}")
            continue

        # 解析 & 清洗
        try:
            count, path = process_task(task, raw_bytes, is_cached)
            stats.success += 1
            stats.total_lines += count
            success(f"  {label} → {count} 条规则")
        except Exception as e:
            stats.parse_errors.append((url, str(e)))
            warning(f"  ⚠ 解析失败: {label} | {e}")

    # 清理孤儿文件
    clean_orphans(expected_files)

    # 生成报告
    generate_summary()

    # 退出判断（降级模式）
    if STRICT_MODE and (stats.download_errors or stats.parse_errors):
        gh_error("严格模式下存在失败源，退出")
        sys.exit(1)

    info(f"\n🎯 同步完成: {stats.success} 成功, {stats.skipped} 缓存命中, "
         f"{len(stats.download_errors)} 下载失败, {len(stats.parse_errors)} 解析失败")

    git_push()


if __name__ == "__main__":
    main()
