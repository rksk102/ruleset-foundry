import os
import sys
import re
import shutil
import logging
import requests
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import processor

SOURCES_FILE = "sources.urls"
RULESETS_DIR = Path("rulesets")
TIMEOUT = 15
RETRIES = 2

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

class Statistics:
    def __init__(self):
        self.success = 0
        self.total_lines = 0
        self.download_errors = []
        self.parse_errors = []

stats = Statistics()

def normalize_policy(p):
    p = p.lower()
    if any(x in p for x in ['reject', 'block', 'deny', 'ads', 'adblock']): return 'block'
    if any(x in p for x in ['direct', 'bypass', 'no-proxy']): return 'direct'
    if any(x in p for x in ['proxy', 'gfw']): return 'proxy'
    return p if p else 'proxy'

def normalize_type(t):
    t = t.lower()
    return 'ipcidr' if 'ip' in t or 'cidr' in t else 'domain'

def get_owner(url):
    """从URL中提取所有者，逻辑复刻原Shell脚本"""
    parts = url.split('/')
    domain = parts[2]
    
    if 'github' in domain:
        return parts[3]
    elif domain == 'cdn.jsdelivr.net':
        if len(parts) > 4 and parts[3] == 'gh':
            return parts[4]
        return 'jsdelivr'
    else:
        return domain

def download_content(url):
    """下载内容，带重试机制"""
    for attempt in range(RETRIES + 1):
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException:
            if attempt == RETRIES:
                return None
    return None

def parse_sources():
    """解析 sources.urls 文件"""
    tasks = []
    current_policy = 'proxy'
    current_type = 'domain'
    
    if not os.path.exists(SOURCES_FILE):
        logger.error(f"File {SOURCES_FILE} not found!")
        sys.exit(1)

    with open(SOURCES_FILE, 'r', encoding='utf-8') as f:

        content = f.read().lstrip('\ufeff')
        
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
            
        m_pol = re.match(r'^\[policy:(.+)\]$', line)
        if m_pol:
            current_policy = normalize_policy(m_pol.group(1))
            continue
            
        m_type = re.match(r'^\[type:(.+)\]$', line)
        if m_type:
            current_type = normalize_type(m_type.group(1))
            continue

        url_match = re.search(r'https?://\S+', line)
        if url_match:
            tasks.append({
                'policy': current_policy,
                'type': current_type,
                'url': url_match.group(0)
            })
    return tasks

def clean_orphans(expected_files):
    """清理不再需要的文件"""
    logger.info("::group::🧹 Cleaning Orphan Files")
    if not RULESETS_DIR.exists():
        return

    actual_files = set()
    for p in RULESETS_DIR.rglob('*.txt'):
        actual_files.add(str(p))

    expected_set = set(str(f) for f in expected_files)
    
    for f in actual_files:
        if f not in expected_set:
            logger.info(f"Deleting orphan: {f}")
            os.remove(f)
    
    for dirpath, _, _ in os.walk(RULESETS_DIR, topdown=False):
        if not os.listdir(dirpath):
            os.rmdir(dirpath)
    logger.info("::endgroup::")

def generate_summary():
    """生成 GitHub Action Summary"""
    summary_path = os.getenv('GITHUB_STEP_SUMMARY')
    if not summary_path:
        return

    with open(summary_path, 'a', encoding='utf-8') as f:
        f.write("# 🛡️ Rules Sync Dashboard (Python Engine)\n\n")
        f.write(f"| 🟢 Success | 🔴 Failures | 📉 Total Rules |\n")
        f.write(f"| :---: | :---: | :---: |\n")
        total_fail = len(stats.download_errors) + len(stats.parse_errors)
        f.write(f"| **{stats.success}** | **{total_fail}** | **{stats.total_lines}** |\n\n")

        if total_fail > 0:
            f.write("## 🚨 Error Diagnostics\n\n| Type | Failed Source URL |\n| :--- | :--- |\n")
            for url in stats.download_errors:
                f.write(f"| 📡 **Download** | `{url}` |\n")
            for url in stats.parse_errors:
                f.write(f"| 🧠 **Parse** | `{url}` |\n")
        else:
            f.write("## ✅ All Systems Operational\n")
        
        f.write(f"\n_Generated at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")

def git_push():
    """执行 Git 提交"""
    logger.info("::group::💾 Git Commit")
    
    def run_cmd(args):
        subprocess.run(args, check=False)

    run_cmd(['git', 'config', 'user.name', 'GitHub Actions Bot'])
    run_cmd(['git', 'config', 'user.email', 'actions@github.com'])
    run_cmd(['git', 'add', '-A'])
    
    res = subprocess.run(['git', 'diff-index', '--quiet', 'HEAD'], check=False)
    if res.returncode == 0:
        logger.info("No changes.")
    else:
        logger.info("Pushing changes...")
        msg = f"chore(sync): Rules update {datetime.now().strftime('%Y-%m-%d')}"
        run_cmd(['git', 'commit', '-m', msg])
        run_cmd(['git', 'push'])
    
    logger.info("::endgroup::")

def main():
    logger.info("::group::🔧 Initialization")
    tasks = parse_sources()
    logger.info(f"Loaded {len(tasks)} sources.")
    logger.info("::endgroup::")

    expected_files = []

    for task in tasks:
        url = task['url']
        owner = get_owner(url)
        filename = url.split('/')[-1].split('.')[0] + ".txt"
        
        rel_path = Path(task['policy']) / task['type'] / owner / filename
        abs_path = RULESETS_DIR / rel_path
        expected_files.append(abs_path)
        
        logger.info(f"::group::⚙️ [{task['policy']}/{task['type']}] {owner}/{filename}")
        
        raw_bytes = download_content(url)
        if raw_bytes is None:
            logger.error(f"::error::Download failed: {url}")
            stats.download_errors.append(url)
            logger.info("::endgroup::")
            continue

        try:
            content_str = processor.safe_decode(raw_bytes)
            lines = processor.parse_lines(content_str)
            
            if task['type'] == 'ipcidr':
                result = processor.process_ip(lines)
            else:
                result = processor.process_domain(lines)
            
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            with open(abs_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(result))
            
            count = len(result)
            stats.success += 1
            stats.total_lines += count
            logger.info(f"SUCCESS: Saved {count} lines.")
            
        except Exception as e:
            logger.error(f"::error::Parse failed: {e}")
            stats.parse_errors.append(url)
        
        logger.info("::endgroup::")

    clean_orphans(expected_files)
    
    generate_summary()
    
    strict_mode = os.getenv('STRICT_MODE', 'false').lower() == 'true'
    fail_count = len(stats.download_errors) + len(stats.parse_errors)
    
    if fail_count > 0 and strict_mode:
        sys.exit(1)

    git_push()

if __name__ == "__main__":
    main()
