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

SOURCES_FILE = get("paths", "sources_file", default="sources.urls")
RULESETS_DIR = Path(get("paths", "rulesets_dir", default="rulesets"))
TIMEOUT = get("network", "timeout_seconds", default=15)
RETRIES = get("network", "max_retries", default=2)
STRICT_MODE = get("behavior", "strict_mode", default=False)


def build_filepath(task):
    owner = get_owner_from_url(task["url"])
    filename = task["url"].split("/")[-1].split(".")[0] + ".txt"
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
        gh_error(f"file {SOURCES_FILE} not found!")
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
                    debug(f"  ETag hit(unchanged): {url}")
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
                warning(f"  download failed [{attempt+1}/{RETRIES+1}]: {url} -> {e}")
                return (task, None, False)
            await asyncio.sleep(2 * (attempt + 1))

    return (task, None, False)


async def download_all(tasks):
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        coros = [download_one(session, t) for t in tasks]
        results = await asyncio.gather(*coros)
    return results


def process_task(task, raw_bytes, is_cached):
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
    group_start("Clean orphan files")
    if not RULESETS_DIR.exists():
        group_end()
        return

    actual_files = set(str(p) for p in RULESETS_DIR.rglob("*.txt"))
    expected_set = set(str(f) for f in expected_files)

    removed = 0
    for f in actual_files:
        if f not in expected_set:
            os.remove(f)
            debug(f"  deleted: {f}")
            removed += 1

    for dirpath, _, _ in os.walk(RULESETS_DIR, topdown=False):
        if not os.listdir(dirpath):
            os.rmdir(dirpath)

    info(f"  cleaned {removed} orphan files")
    group_end()


def generate_summary():
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    dl_fail = len(stats.download_errors)
    parse_fail = len(stats.parse_errors)
    total_fail = dl_fail + parse_fail

    section(f"Sync Report | success:{stats.success} cache:{stats.skipped} fail:{total_fail} | elapsed:{stats.elapsed()}")

    if stats.download_errors:
        warning(f"  download failures ({dl_fail}):")
        for url, reason in stats.download_errors:
            warning(f"    L {url} | {reason}")

    if stats.parse_errors:
        warning(f"  parse failures ({parse_fail}):")
        for url, reason in stats.parse_errors:
            warning(f"    L {url} | {reason}")

    if not summary_path:
        return

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("# Sync Dashboard\n\n")
        f.write("| success | cache hit | fail | total rules |\n")
        f.write("| :---: | :---: | :---: | :---: |\n")
        f.write(f"| **{stats.success}** | **{stats.skipped}** | **{total_fail}** | **{stats.total_lines}** |\n\n")

        if dl_fail > 0:
            f.write("### Download Failures\n\n| URL | Reason |\n| :--- | :--- |\n")
            for url, reason in stats.download_errors:
                f.write(f"| `{url}` | {reason} |\n")
            f.write("\n")

        if parse_fail > 0:
            f.write("### Parse Failures\n\n| URL | Reason |\n| :--- | :--- |\n")
            for url, reason in stats.parse_errors:
                f.write(f"| `{url}` | {reason} |\n")
            f.write("\n")

        if total_fail == 0:
            f.write("### All sources synced successfully\n\n")

        f.write(f"\n_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")


def git_push():
    group_start("Git Commit")

    def run_cmd(args):
        return subprocess.run(args, check=False)

    run_cmd(["git", "config", "user.name", "github-actions[bot]"])
    run_cmd(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"])
    run_cmd(["git", "add", "rulesets/"])

    res = subprocess.run(["git", "diff-index", "--quiet", "HEAD"], check=False)
    if res.returncode == 0:
        info("  no changes, skip commit")
    else:
        info("  pushing changes...")
        msg = f"chore(sync): Rules update {datetime.now().strftime('%Y-%m-%d')}"
        run_cmd(["git", "commit", "-m", msg])
        push_res = run_cmd(["git", "push"])
        if push_res.returncode != 0:
            error("  git push failed, trying pull --rebase...")
            run_cmd(["git", "pull", "--rebase"])
            push_res2 = run_cmd(["git", "push"])
            if push_res2.returncode != 0:
                error("  git push retry failed")
                sys.exit(1)

    group_end()


def main():
    group_start("Init")
    info(f"  timeout:{TIMEOUT}s | retries:{RETRIES} | strict:{'ON' if STRICT_MODE else 'OFF'}")
    tasks = parse_sources()
    info(f"  loaded {len(tasks)} sources")
    group_end()

    group_start(f"Download ({len(tasks)} sources)")
    results = asyncio.run(download_all(tasks))
    info(f"  done | ETag hits: {len(stats.etag_hits)}")
    group_end()

    expected_files = []

    for task, raw_bytes, is_cached in results:
        owner, filename, rel_path, abs_path = build_filepath(task)
        expected_files.append(abs_path)

        label = f"[{task['policy']}/{task['type']}] {owner}/{filename}"

        if is_cached:
            stats.skipped += 1
            debug(f"  skip(unchanged): {label}")
            continue

        if raw_bytes is None:
            stats.download_errors.append((task["url"], "download failed"))
            warning(f"  download failed: {label}")
            continue

        try:
            count, path = process_task(task, raw_bytes, is_cached)
            stats.success += 1
            stats.total_lines += count
            success(f"  {label} -> {count} rules")
        except Exception as e:
            stats.parse_errors.append((task["url"], str(e)))
            warning(f"  parse failed: {label} | {e}")

    clean_orphans(expected_files)
    generate_summary()

    if STRICT_MODE and (stats.download_errors or stats.parse_errors):
        gh_error("strict mode: abort on failures")
        sys.exit(1)

    info(f"\nsync done: {stats.success} ok, {stats.skipped} cached, "
         f"{len(stats.download_errors)} dl-fail, {len(stats.parse_errors)} parse-fail")

    git_push()


if __name__ == "__main__":
    main()
