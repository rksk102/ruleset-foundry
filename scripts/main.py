import asyncio
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import processor
from config_loader import get
from logger import debug, gh_error, group_end, group_start, info, section, success, warning
from utils import (
    atomic_write,
    beijing_now,
    beijing_timestamp,
    get_owner_from_url,
    normalize_policy,
    normalize_type,
)

SOURCES_FILE = get("paths", "sources_file", default="sources.urls")
RULESETS_DIR = Path(get("paths", "rulesets_dir", default="rulesets"))
TIMEOUT = get("network", "timeout_seconds", default=15)
RETRIES = get("network", "max_retries", default=2)
MAX_SOURCE_BYTES = get("network", "max_source_bytes", default=64 * 1024 * 1024)
STRICT_MODE = get("behavior", "strict_mode", default=False)
UNRECOGNIZED_WARN_RATIO = get("behavior", "unrecognized_warn_ratio", default=0.10)
CONCURRENCY = get("network", "max_concurrency", default=6)
PER_HOST = get("network", "max_per_host", default=2)
MAX_RETRY_AFTER = get("network", "max_retry_after_seconds", default=60)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

AUTH_HOST_SUFFIXES = (
    "githubusercontent.com",
    "github.com",
    "githubassets.com",
    "github.io",
)


def is_trusted_host(url):
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == sfx or host.endswith("." + sfx) for sfx in AUTH_HOST_SUFFIXES)


def auth_headers(url):
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token or not is_trusted_host(url):
        return None
    return {"Authorization": f"Bearer {token}"}


def parse_retry_after(value, default):
    if not value:
        return default
    text = value.strip()
    if text.isascii() and text.isdecimal():
        seconds = int(text)
    else:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return default
        if when is None:
            return default
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = int((when - datetime.now(timezone.utc)).total_seconds())
    return max(0, min(seconds, MAX_RETRY_AFTER))


def source_repo_slug(url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = [p for p in parts.path.strip("/").split("/") if p]

    if host in ("github.com", "www.github.com", "raw.githubusercontent.com",
                "objects.githubusercontent.com", "codeload.github.com",
                "gist.githubusercontent.com", "github.io") or host.endswith(".github.io"):
        if len(path) >= 2:
            return f"{path[0]}__{path[1]}"
        return ""
    if host == "cdn.jsdelivr.net":
        if len(path) >= 3 and path[0] == "gh":
            return f"{path[1]}__{path[2].split('@')[0]}"
        if len(path) >= 2:
            return f"{path[0]}__{path[1].split('@')[0]}"
        return ""
    return host.replace(".", "_") if host else ""


def _base_rel_path(task):
    owner = get_owner_from_url(task["url"])
    last_segment = task["url"].split("/")[-1].split("?")[0].split("#")[0]
    filename = last_segment.split(".")[0] + ".txt"
    return Path(task["policy"]) / task["type"] / owner / filename


def colliding_output_paths(tasks):
    by_path = {}
    for task in tasks:
        rel = _base_rel_path(task)
        by_path.setdefault(str(rel), []).append(source_repo_slug(task["url"]))

    plan = {}
    for rel, slugs in by_path.items():
        distinct = {s for s in slugs if s}
        if len(slugs) > 1 and len(distinct) > 1:
            plan[rel] = sorted(distinct)
    return plan


def build_filepath(task, collide_plan=None, taken=None):
    rel_path = _base_rel_path(task)
    owner = rel_path.parent.name
    slug = source_repo_slug(task["url"]) or owner

    if collide_plan and str(rel_path) in collide_plan:
        candidate = slug
        proposal = Path(task["policy"]) / task["type"] / candidate / rel_path.name
        if taken is not None:
            while str(proposal) in taken and taken[str(proposal)] != slug:
                candidate += "_"
                proposal = Path(task["policy"]) / task["type"] / candidate / rel_path.name
        rel_path = proposal
        owner = candidate

    if taken is not None:
        taken[str(rel_path)] = slug
    return owner, rel_path.name, rel_path, RULESETS_DIR / rel_path


def plan_groups(tasks):
    collide_plan = colliding_output_paths(tasks)
    for rel, slugs in sorted(collide_plan.items()):
        warning(f"  输出路径跨来源冲突 {rel} -> 已按来源拆分为 {slugs}")

    taken = {}
    for task in tasks:
        base = _base_rel_path(task)
        taken.setdefault(str(base), source_repo_slug(task["url"]) or base.parent.name)

    groups = {}
    for idx, task in enumerate(tasks):
        _, _, _, abs_path = build_filepath(task, collide_plan, taken)
        key = str(abs_path)
        group = groups.setdefault(key, {
            "path": key,
            "policy": task["policy"],
            "type": task["type"],
            "members": [],
            "sources": [],
        })
        group["members"].append((idx, task))
        group["sources"].append(task["url"])

    conflicts = []
    for key, group in groups.items():
        slugs = {source_repo_slug(t["url"]) for _i, t in group["members"]}
        if len(group["members"]) > 1 and len(slugs) > 1:
            conflicts.append((key, sorted(slugs)))
    if conflicts:
        for key, slugs in sorted(conflicts):
            warning(f"  输出路径仍被多来源共享 {key} <- {slugs}")

    ordered = []
    for key in sorted(groups):
        group = groups[key]
        if len(group["members"]) > 1:
            info(f"  同输出多源合并 {len(group['members'])} 个源 -> {group['path']}")
        ordered.append(group)
    return ordered


def _process_ip_group(all_lines):
    ip_lines = []
    stats = processor.new_stats()
    for line in all_lines:
        kind, payload, type_name = processor.classify_rule_line(line)
        if kind == "ip":
            if payload:
                ip_lines.append(payload)
            else:
                stats["unrecognized"] += 1
        elif kind in ("suffix", "exact"):
            stats["suffix" if kind == "suffix" else "relaxed_exact"] += 1
        elif kind == "opaque":
            stats["dropped_rule_type"][type_name] = \
                stats["dropped_rule_type"].get(type_name, 0) + 1
        else:
            ip_lines.append(line)

    result, ip_errors = processor.process_ip(ip_lines)
    for bad, why in ip_errors[:10]:
        warning(f"    无效 CIDR 已丢弃: {bad} -> {why}")
    if len(ip_errors) > 10:
        warning(f"    ... 及其他 {len(ip_errors) - 10} 条无效 CIDR")

    for label, count in (
        ("后缀(+./domain:)", stats["suffix"]),
        ("精确(full:/host:/DOMAIN,)", stats["relaxed_exact"]),
        ("空负载", stats["unrecognized"]),
    ):
        if count:
            warning(f"    {label} 规则不可放入 ipcidr 产物，已丢弃: {count} 行")
    for type_name, count in sorted(stats["dropped_rule_type"].items()):
        warning(f"    不可表达规则类型被丢弃 [{type_name}]: {count} 行")
    return result, stats


def process_group(group, raw_by_index):
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
        result, _special = _process_ip_group(all_lines)
    else:
        result, special = processor.process_domain_detailed(all_lines)
        for key, label in (
            ("suffix", "后缀规则(+./domain:)已保留 +. 前缀"),
            ("relaxed_exact", "精确规则(full:/host:/裸域名)按精确匹配输出"),
            ("wildcard", "通配符规则(*)已原样保留"),
            ("bare_single_label", "裸单标签条目按精确匹配保留"),
            ("dropped_exception", "例外规则(@@)被丢弃"),
            ("dropped_keyword", "关键字/正则规则被丢弃"),
            ("ip_in_domain", "IP 规则出现在 domain 源中，已丢弃"),
        ):
            if special.get(key):
                warning(f"    {label}: {special[key]} 行")
        for type_name, count in sorted((special.get("dropped_rule_type") or {}).items()):
            warning(f"    不可表达规则类型被丢弃 [{type_name}]: {count} 行")

        unrecognized = special.get("unrecognized", 0)
        if unrecognized:
            ratio = unrecognized / len(all_lines)
            warning(f"    未识别/已丢弃行: {unrecognized}/{len(all_lines)} ({ratio:.1%})")
            if ratio >= UNRECOGNIZED_WARN_RATIO:
                errors["parse"].append((
                    group["path"],
                    f"未识别行占比 {ratio:.1%} 超过阈值 {UNRECOGNIZED_WARN_RATIO:.0%}"
                    f"（{unrecognized}/{len(all_lines)}），疑似上游格式变更",
                ))

    if not result:
        for _idx, task in group["members"]:
            errors["parse"].append((task["url"], "未解析出有效规则"))
        return None, errors

    header = [f"# Source: {url}" for url in group.get("sources", [])]
    atomic_write(group["path"], header + result)
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


async def read_capped(stream):
    chunks = []
    total = 0
    async for chunk in stream.iter_chunked(65536):
        total += len(chunk)
        if total > MAX_SOURCE_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _retry_delay(resp, attempt):
    header = resp.headers.get("Retry-After") if resp.headers else None
    if header:
        return parse_retry_after(header, 1 * (attempt + 1))
    return 1 * (attempt + 1)


async def download_one(session, task):
    url = task["url"]
    headers = auth_headers(url)
    backoff_budget = MAX_RETRY_AFTER * 2

    for attempt in range(RETRIES + 1):
        delay = None
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=TIMEOUT), headers=headers,
            ) as resp:
                if resp.status == 200:
                    content = await read_capped(resp.content)
                    if content is None:
                        warning(f"  响应超过 {MAX_SOURCE_BYTES} 字节上限: {url}")
                        return (task, None, f"超过 {MAX_SOURCE_BYTES} 字节上限")
                    if not content:
                        warning(f"  空响应: {url}")
                        return (task, None, "空响应")
                    if not processor.is_text_data(processor.safe_decode(content)):
                        warning(f"  非文本响应: {url}")
                        return (task, None, "非文本响应")
                    return (task, content, None)

                if 400 <= resp.status < 500 and resp.status not in RETRYABLE_STATUS:
                    warning(f"  下载失败 (不可重试): {url} -> HTTP {resp.status}")
                    return (task, None, f"HTTP {resp.status}")

                if attempt == RETRIES:
                    return (task, None, f"HTTP {resp.status}")

                delay = _retry_delay(resp, attempt)
                warning(f"  下载失败，{delay}s 后重试 [{attempt+1}/{RETRIES+1}]: "
                        f"{url} -> HTTP {resp.status}")

            if delay:
                if delay > backoff_budget:
                    warning(f"  退避预算耗尽（{backoff_budget}s），放弃重试: {url}")
                    return (task, None, f"HTTP {resp.status}（退避预算耗尽）")
                backoff_budget -= delay
                await asyncio.sleep(delay)

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
    connector = aiohttp.TCPConnector(limit=CONCURRENCY, limit_per_host=PER_HOST)
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


def generate_summary(stats):
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
    stats = SyncStats()

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
    generate_summary(stats)

    if STRICT_MODE and (stats.download_errors or stats.parse_errors):
        gh_error("严格模式下存在失败源，退出")
        sys.exit(1)

    info(f"\n同步完成: {stats.success} 个输出成功, "
         f"{len(stats.download_errors)} 下载失败, {len(stats.parse_errors)} 解析失败")


if __name__ == "__main__":
    main()
