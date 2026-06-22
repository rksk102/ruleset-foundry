import os
import sys
import re
import asyncio
import aiohttp
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
    flush_etag_cache,
    atomic_write,
)

logger = get_logger()

SOURCES_FILE = get("paths", "sources_file", default="sources.urls")
RULESETS_DIR = Path(get("paths", "rulesets_dir", default="rulesets"))
TIMEOUT = get("network", "timeout_seconds", default=15)
RETRIES = get("network", "max_retries", default=2)
STRICT_MODE = get("behavior", "strict_mode", default=False)


def build_filepath(task):
    owner = get_owner_from_url(task["url"])
    last_segment = task["url"].split("/")[-1].split("?")[0].split("#")[0]
    filename = last_segment.split(".")[0] + ".txt"
    rel_path = Path(task["policy"]) / task["type"] / owner / filename
    abs_path = RULESETS_DIR / rel_path
    return owner, filename, rel_path, abs_path


class SyncStats:
    def __init__(self):
        self.success = 0
        self.skipped = 0
        self.download_errors = []
        self.parse_errors = []
        self.total_lines = 0
        self.start_time = time.time()
        self.etag_hits = []

    def elapsed(self):
        return f"{time.time() - self.start_time:.1f}s"


stats = SyncStats()


def parse_sources():
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

        url_match = re.search(r"https?://[^\s#]+", line)
        if url_match:
            tasks.append({
                "policy": current_policy,
                "type": current_type,
                "url": url_match.group(0),
            })

    return tasks


async def download_one(session, task):
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
                    return (task, None, True, None)

                if resp.status == 200:
                    content = await resp.read()
                    update_etag_cache(url, resp)
                    return (task, content, False, None)

                if 400 <= resp.status < 500 and resp.status != 429:
                    warning(f"  下载失败 (不可重试): {url} -> HTTP {resp.status}")
                    return (task, None, False, f"HTTP {resp.status}")

                if attempt == RETRIES:
                    return (task, None, False, f"HTTP {resp.status}")
                await asyncio.sleep(1 * (attempt + 1))

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == RETRIES:
                warning(f"  下载失败 [{attempt+1}/{RETRIES+1}]: {url} -> {e}")
                return (task, None, False, f"{type(e).__name__}: {e}")
            await asyncio.sleep(2 * (attempt + 1))

    return (task, None, False, "未知错误")


async def download_all(tasks):
    connector = aiohttp.TCPConnector(limit=3)
    async with aiohttp.ClientSession(connector=connector) as session:
        coros = [download_one(session, t) for t in tasks]
        results = await asyncio.gather(*coros)
    return results


def process_task(task, raw_bytes):
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


def clean_orphans(expected_files):
    group_start("清理孤儿文件")
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


def generate_summary():
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    dl_fail = len(stats.download_errors)
    parse_fail = len(stats.parse_errors)
    total_fail = dl_fail + parse_fail

    section(f"同步报告 | 成功:{stats.success} 缓存:{stats.skipped} 失败:{total_fail} | 耗时:{stats.elapsed()}")

    if stats.download_errors:
        warning(f"  下载失败 ({dl_fail}):")
        for url, reason in stats.download_errors:
            warning(f"    L {url} | {reason}")

    if stats.parse_errors:
        warning(f"  解析失败 ({parse_fail}):")
        for url, reason in stats.parse_errors:
            warning(f"    L {url} | {reason}")

    if not summary_path:
        return

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("# 规则同步仪表盘\n\n")
        f.write("| 成功 | 缓存命中 | 失败 | 总规则数 |\n")
        f.write("| :---: | :---: | :---: | :---: |\n")
        f.write(f"| **{stats.success}** | **{stats.skipped}** | **{total_fail}** | **{stats.total_lines}** |\n\n")

        if dl_fail > 0:
            f.write("### 下载失败详情\n\n| URL | 原因 |\n| :--- | :--- |\n")
            for url, reason in stats.download_errors:
                f.write(f"| `{url}` | {reason} |\n")
            f.write("\n")

        if parse_fail > 0:
            f.write("### 解析失败详情\n\n| URL | 原因 |\n| :--- | :--- |\n")
            for url, reason in stats.parse_errors:
                f.write(f"| `{url}` | {reason} |\n")
            f.write("\n")

        if total_fail == 0:
            f.write("### 全部源同步正常\n\n")

        f.write(f"\n_生成时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")


def main():
    group_start("初始化")
    RULESETS_DIR.mkdir(parents=True, exist_ok=True)
    info(f"  超时:{TIMEOUT}s | 重试:{RETRIES}次 | 严格模式:{'开' if STRICT_MODE else '关'}")
    tasks = parse_sources()
    info(f"  加载 {len(tasks)} 个上游源")
    group_end()

    group_start(f"并发下载 ({len(tasks)} 源)")
    results = asyncio.run(download_all(tasks))
    flush_etag_cache()
    info(f"  下载完成 | ETag 命中: {len(stats.etag_hits)}")
    group_end()

    expected_files = []

    for task, raw_bytes, is_cached, error_msg in results:
        owner, filename, rel_path, abs_path = build_filepath(task)
        expected_files.append(abs_path)

        label = f"[{task['policy']}/{task['type']}] {owner}/{filename}"

        if is_cached:
            stats.skipped += 1
            debug(f"  跳过(未变化): {label}")
            continue

        if raw_bytes is None:
            stats.download_errors.append((task["url"], error_msg or "下载失败"))
            warning(f"  下载失败: {label} | {error_msg}")
            continue

        try:
            count, path = process_task(task, raw_bytes)
            stats.success += 1
            stats.total_lines += count
            success(f"  {label} -> {count} 条规则")
        except Exception as e:
            stats.parse_errors.append((task["url"], str(e)))
            warning(f"  解析失败: {label} | {e}")

    if stats.success == 0 and stats.download_errors == 0:
        info("  所有源均未变化（ETag 缓存命中），无新内容写入")
        summary_file = RULESETS_DIR / "sync-summary.txt"
        summary_file.write_text(
            "# 同步摘要\n"
            f"# 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# 所有 {len(tasks)} 个源均未变化（ETag 缓存命中），无新内容同步\n",
            encoding="utf-8",
        )
        expected_files.append(summary_file)

    clean_orphans(expected_files)
    generate_summary()

    if STRICT_MODE and (stats.download_errors or stats.parse_errors):
        gh_error("严格模式下存在失败源，退出")
        sys.exit(1)

    info(f"\n同步完成: {stats.success} 成功, {stats.skipped} 缓存命中, "
         f"{len(stats.download_errors)} 下载失败, {len(stats.parse_errors)} 解析失败")


if __name__ == "__main__":
    main()
