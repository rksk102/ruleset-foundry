import asyncio
import os
import re
import sys
import time
from pathlib import Path

import aiohttp
import processor
from config_loader import get
from logger import debug, get_logger, gh_error, group_end, group_start, info, section, success, warning
from utils import (
    atomic_write,
    beijing_now,
    beijing_timestamp,
    get_owner_from_url,
    normalize_policy,
    normalize_type,
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


def plan_groups(tasks):
    """按输出绝对路径分组。同路径多源将在清洗前合并，避免静默覆盖。"""
    groups = {}
    for idx, task in enumerate(tasks):
        _, _, _, abs_path = build_filepath(task)
        key = str(abs_path)
        group = groups.setdefault(key, {
            "path": key,
            "policy": task["policy"],
            "type": task["type"],
            "members": [],
        })
        group["members"].append((idx, task))

    ordered = []
    for key in sorted(groups):
        group = groups[key]
        if len(group["members"]) > 1:
            warning(f"  输出路径冲突，合并 {len(group['members'])} 个源 -> {group['path']}")
            for _idx, t in group["members"]:
                warning(f"    L {t['url']}")
        ordered.append(group)
    return ordered


def process_group(group, raw_by_index):
    """合并组内所有成员的原始规则行后统一清洗写出。

    返回 (规则数|None, {"download": [(url,原因)], "parse": [(url,原因)]})。
    下载失败与解析失败分开上报，避免严格模式把两者混为一谈。
    """
    all_lines = []
    errors = {"download": [], "parse": []}
    for idx, task in group["members"]:
        raw = raw_by_index.get(idx)
        if raw is None:
            errors["download"].append((task["url"], "下载失败"))
            continue
        try:
            all_lines.extend(processor.parse_lines(processor.safe_decode(raw)))
        except Exception as e:
            errors["parse"].append((task["url"], f"解析失败: {e}"))

    if not all_lines:
        return None, errors

    if group["type"] == "ipcidr":
        result = processor.process_ip(all_lines)
    else:
        result = processor.process_domain(all_lines)
        special = processor.analyze_domain(all_lines)
        for key, label in (
            ("dropped_exception", "例外规则(@@)被丢弃"),
            ("dropped_keyword", "关键字/正则规则被丢弃"),
            ("widened_exact", "精确规则(full:/host:)被放宽为 suffix"),
            ("bad_anchor", "含 AdBlock 锚点(^)已归一化"),
        ):
            if special.get(key):
                warning(f"    {label}: {special[key]} 行")

    atomic_write(group["path"], result)
    return len(result), errors


class SyncStats:
    def __init__(self):
        self.success = 0
        self.download_errors = []
        self.parse_errors = []
        self.total_lines = 0
        self.start_time = time.time()

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

    for attempt in range(RETRIES + 1):
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    if not content:
                        warning(f"  空响应: {url}")
                        return (task, None, "空响应")
                    if not processor.is_text_data(processor.safe_decode(content)):
                        warning(f"  非文本响应: {url}")
                        return (task, None, "非文本响应")
                    return (task, content, None)

                if 400 <= resp.status < 500 and resp.status != 429:
                    warning(f"  下载失败 (不可重试): {url} -> HTTP {resp.status}")
                    return (task, None, f"HTTP {resp.status}")

                if attempt == RETRIES:
                    return (task, None, f"HTTP {resp.status}")
                await asyncio.sleep(1 * (attempt + 1))

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == RETRIES:
                warning(f"  下载失败 [{attempt+1}/{RETRIES+1}]: {url} -> {e}")
                return (task, None, f"{type(e).__name__}: {e}")
            await asyncio.sleep(2 * (attempt + 1))
        except Exception as e:
            warning(f"  下载异常: {url} -> {type(e).__name__}: {e}")
            return (task, None, f"{type(e).__name__}: {e}")

    return (task, None, "未知错误")


async def download_all(tasks):
    connector = aiohttp.TCPConnector(limit=6)
    async with aiohttp.ClientSession(connector=connector) as session:
        coros = [download_one(session, t) for t in tasks]
        results = await asyncio.gather(*coros, return_exceptions=True)

    normalized = []
    for task, result in zip(tasks, results):
        if isinstance(result, BaseException):
            normalized.append((task, None, f"{type(result).__name__}: {result}"))
        else:
            normalized.append(result)
    return normalized


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

    section(f"同步报告 | 成功:{stats.success} 失败:{total_fail} | 耗时:{stats.elapsed()}")

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
        f.write("| 成功 | 失败 | 总规则数 |\n")
        f.write("| :---: | :---: | :---: |\n")
        f.write(f"| **{stats.success}** | **{total_fail}** | **{stats.total_lines}** |\n\n")

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

        f.write(f"\n_生成时间: {beijing_now().strftime('%Y-%m-%d %H:%M:%S')} (北京时间)_\n")


def main():
    group_start("初始化")
    RULESETS_DIR.mkdir(parents=True, exist_ok=True)
    info(f"  超时:{TIMEOUT}s | 重试:{RETRIES}次 | 严格模式:{'开' if STRICT_MODE else '关'}")
    tasks = parse_sources()
    info(f"  加载 {len(tasks)} 个上游源")
    group_end()

    group_start(f"并发下载 ({len(tasks)} 源)")
    results = asyncio.run(download_all(tasks))
    info("  下载完成")
    group_end()

    groups = plan_groups(tasks)
    raw_by_index = {}
    for idx, (_task, raw_bytes, _err) in enumerate(results):
        if raw_bytes is not None:
            raw_by_index[idx] = raw_bytes

    expected_files = []
    for group in groups:
        count, errors = process_group(group, raw_by_index)
        for url, reason in errors["download"]:
            stats.download_errors.append((url, reason))
            warning(f"  下载失败: {url} | {reason}")
        for url, reason in errors["parse"]:
            stats.parse_errors.append((url, reason))
            warning(f"  解析失败: {url} | {reason}")
        if count is None:
            warning(f"  跳过（无可用规则）: {group['path']}")
            continue
        expected_files.append(Path(group["path"]))
        stats.success += 1
        stats.total_lines += count
        label = f"[{group['policy']}/{group['type']}] {Path(group['path']).name}"
        success(f"  {label} -> {count} 条规则")

    if stats.success == 0:
        reason = "（全部源下载失败）" if stats.download_errors else ""
        info(f"  无新规则写入{reason}")
        summary_file = RULESETS_DIR / "sync-summary.txt"
        summary_file.write_text(
            "# 同步摘要\n"
            f"# 时间: {beijing_timestamp()}\n"
            f"# 成功: {stats.success} 失败: {len(stats.download_errors)}\n"
            "# 无新规则内容同步\n",
            encoding="utf-8",
        )
        expected_files.append(summary_file)

    clean_orphans(expected_files)
    generate_summary()

    if STRICT_MODE and (stats.download_errors or stats.parse_errors):
        gh_error("严格模式下存在失败源，退出")
        sys.exit(1)

    info(f"\n同步完成: {stats.success} 个输出成功, "
         f"{len(stats.download_errors)} 下载失败, {len(stats.parse_errors)} 解析失败")


if __name__ == "__main__":
    main()
