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
    group_start("Prepare Mihomo Kernel")

    headers = {}
    if "GH_TOKEN" in os.environ:
        headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"

    try:
        req = urllib.request.Request(REPO_API, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag_name = data["tag_name"]
        info(f"  latest version: {tag_name}")

        if KERNEL_CACHE_DIR.exists() and VERSION_FILE.exists():
            cached_ver = VERSION_FILE.read_text().strip()
            if cached_ver == tag_name and os.path.exists(KERNEL_BIN):
                info(f"  using cached kernel ({tag_name})")
                group_end()
                return

        download_url = None
        for asset in data["assets"]:
            if ("linux-amd64" in asset["name"]
                    and "compatible" not in asset["name"]
                    and asset["name"].endswith(".gz")):
                download_url = asset["browser_download_url"]
                break

        if not download_url:
            raise Exception("no suitable linux-amd64 kernel asset found")

        info(f"  downloading: {download_url}")
        KERNEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        dl_req = urllib.request.Request(download_url, headers=headers)
        with urllib.request.urlopen(dl_req, timeout=120) as dl_resp:
            with gzip.GzipFile(fileobj=dl_resp) as gz:
                with open(KERNEL_BIN, "wb") as f:
                    shutil.copyfileobj(gz, f)

        st = os.stat(KERNEL_BIN)
        os.chmod(KERNEL_BIN, st.st_mode | stat.S_IEXEC)

        ver_out = subprocess.check_output([KERNEL_BIN, "-v"], text=True)
        info(f"  kernel installed: {ver_out.strip()}")

        VERSION_FILE.write_text(tag_name)

    except Exception as e:
        error(f"  kernel preparation failed: {e}")
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
    status_icon = "FAIL" if is_failed else "OK"
    status_text = "Failed" if is_failed else "Success"

    markdown = [
        "\n### MRS Conversion Report",
        f"**Result**: {status_icon} {status_text} (elapsed: {total_time:.2f}s)",
        "",
        "| Metric | Count |",
        "| :--- | :--- |",
        f"| Success | {stats['success']} |",
        f"| Failed | **{stats['failed']}** |",
        f"| Skipped | {stats['skipped']} |",
        f"| Total | {stats['total']} |",
        "",
    ]
    if is_failed:
        markdown.append("**ERROR**: Partial conversion failure, check logs above.")

    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
        f.write("\n".join(markdown))


def main():
    start_time = time.time()
    get_latest_mihomo()

    group_start(f"Convert: {SRC_ROOT} -> {DST_ROOT}")

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
        error(f"source dir {SRC_ROOT} not found!")
        sys.exit(1)

    files_map = []
    for root, _, files in os.walk(SRC_ROOT):
        for f in files:
            if f.endswith(".txt"):
                files_map.append(os.path.join(root, f))

    total_files = len(files_map)
    stats = {"success": 0, "failed": 0, "skipped": 0, "total": total_files}
    info(f"  found {total_files} text rule files")

    for idx, src_path in enumerate(files_map, 1):
        rel_path = os.path.relpath(src_path, SRC_ROOT)
        prefix = f"[{idx}/{total_files}]"

        path_parts = rel_path.split(os.sep)
        rule_type = get_rule_type(path_parts)

        dst_rel = os.path.splitext(rel_path)[0] + ".mrs"
        dst_path = os.path.join(DST_ROOT, dst_rel)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)

        if not rule_type:
            warning(f"  {prefix} skip: {rel_path} (unknown type)")
            stats["skipped"] += 1
            continue

        if not has_valid_content(src_path):
            warning(f"  {prefix} skip: {rel_path} (no valid rules)")
            stats["skipped"] += 1
            continue

        cmd = [KERNEL_BIN, "convert-ruleset", rule_type, "text", src_path, dst_path]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            success(f"  {prefix} {rel_path} -> MRS")
            stats["success"] += 1
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.strip() if e.stderr else "unknown error"
            error(f"  {prefix} {rel_path}")
            error(f"      L {err_msg}")
            stats["failed"] += 1

    group_end()

    duration = time.time() - start_time
    write_summary(stats, duration)

    if stats["failed"] > 0:
        error(f"conversion failed! {stats['failed']} files failed")
        sys.exit(1)
    else:
        success(f"conversion done ({stats['success']} ok, {stats['skipped']} skipped)")
        sys.exit(0)


if __name__ == "__main__":
    main()
