import os
import sys
import shutil
import time
from pathlib import Path

from logger import info, success, warning, error, group_start, group_end, section, get_logger
from config_loader import load_config
from utils import (
    normalize_path,
    flatten_ip_cidr,
    atomic_write_with_header,
)

logger = get_logger()

CONFIG_FILE = "config.yaml"
FALLBACK_CONFIG = "merge-config.yaml"
SOURCE_DIR = "rulesets"
OUTPUT_DIR = "merged-rules"

STATS = {
    "success": 0,
    "skipped": 0,
    "failed": 0,
    "total_rules": 0,
}
ERROR_LOGS = []
SUMMARY_ROWS = []
USED_SOURCE_FILES = set()


def detect_mode(type_str, filename):
    check_str = (str(type_str) + str(filename)).lower()
    if "ip" in check_str or "cidr" in check_str:
        return "IP-CIDR"
    return "DOMAIN"


def process_task_logic(strategy, rule_type, owner, filename, inputs, desc):
    relative_dir = os.path.join(strategy, rule_type, owner)
    full_output_dir = os.path.join(OUTPUT_DIR, relative_dir)
    full_output_file = os.path.join(full_output_dir, filename)
    combined_rules = set()
    files_read_count = 0

    for rel_input in inputs:
        full_src_path = os.path.join(SOURCE_DIR, rel_input)
        rel_src_norm = normalize_path(rel_input)
        USED_SOURCE_FILES.add(rel_src_norm)

        if not os.path.exists(full_src_path):
            raise FileNotFoundError(f"source not found: {rel_input}")

        with open(full_src_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("//"):
                    continue
                if "#" in line:
                    line = line.split("#")[0].strip()
                combined_rules.add(line)
            files_read_count += 1

    if files_read_count == 0 and inputs:
        return None

    mode = detect_mode(rule_type, filename)
    raw_count = len(combined_rules)

    if mode == "IP-CIDR":
        final_list, cidr_errors = flatten_ip_cidr(combined_rules)
        if cidr_errors:
            for bad_cidr, err_msg in cidr_errors[:5]:
                warning(f"    invalid CIDR: {bad_cidr} -> {err_msg}")
    else:
        final_list = sorted(list(combined_rules))

    opt_count = len(final_list)

    metadata = {
        "strategy": strategy,
        "type": rule_type,
        "owner": owner,
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "count": f"{opt_count} (Raw: {raw_count})",
        "desc": desc,
    }
    atomic_write_with_header(full_output_file, final_list, metadata)

    return {
        "file": filename,
        "path": f"{strategy}/{rule_type}/{owner}",
        "mode": mode,
        "src_count": files_read_count,
        "raw": raw_count,
        "opt": opt_count,
    }


def auto_discover_files():
    discovered_tasks = []
    if not os.path.exists(SOURCE_DIR):
        return []

    for root, dirs, files in os.walk(SOURCE_DIR):
        for file in files:
            if file.startswith(".") or not file.endswith(".txt"):
                continue

            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, SOURCE_DIR)
            rel_path_norm = normalize_path(rel_path)
            if rel_path_norm in USED_SOURCE_FILES:
                continue

            parts = Path(rel_path_norm).parent.parts
            d_strat = parts[0] if len(parts) >= 1 else "Auto"
            d_type = parts[1] if len(parts) >= 2 else "General"
            d_owner = parts[2] if len(parts) >= 3 else "Unknown"

            discovered_tasks.append({
                "strategy": d_strat,
                "type": d_type,
                "owner": d_owner,
                "filename": file,
                "inputs": [rel_path_norm],
                "description": f"auto pass-through from {rel_path_norm}",
            })

    return discovered_tasks


def main():
    section("Rule Merger")

    actual_config = CONFIG_FILE if os.path.exists(CONFIG_FILE) else FALLBACK_CONFIG
    if not os.path.exists(actual_config):
        warning(f"config '{actual_config}' not found, auto mode only")
        config_tasks = []
    else:
        cfg = load_config()
        config_tasks = cfg.get("merges", [])
        info(f"  loaded {len(config_tasks)} merge tasks from {actual_config}")

    if not os.path.exists(SOURCE_DIR):
        error(f"source dir '{SOURCE_DIR}' not found!")
        sys.exit(1)

    if os.path.exists(OUTPUT_DIR):
        info("  cleaning output dir...")
        for item in os.listdir(OUTPUT_DIR):
            item_path = os.path.join(OUTPUT_DIR, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception as e:
                warning(f"  clean failed: {item_path} -> {e}")
    else:
        os.makedirs(OUTPUT_DIR)

    if config_tasks:
        group_start(f"Config Merge Tasks ({len(config_tasks)})")
        for t in config_tasks:
            fname = t.get("filename", "Unknown")
            try:
                if "inputs" not in t:
                    raise ValueError("missing inputs")
                res = process_task_logic(
                    t.get("strategy", "Default"),
                    t.get("type", "General"),
                    t.get("owner", "Unknown"),
                    fname,
                    t["inputs"],
                    t.get("description", "config merge"),
                )
                if res:
                    STATS["success"] += 1
                    STATS["total_rules"] += res["opt"]
                    SUMMARY_ROWS.append(res)
                    success(f"  {fname} -> {res['opt']} rules")
                else:
                    STATS["skipped"] += 1
            except Exception as e:
                STATS["failed"] += 1
                ERROR_LOGS.append(f"config task '{fname}': {str(e)}")
                warning(f"  [FAIL] {fname}: {e}")
        group_end()

    auto_tasks = auto_discover_files()
    if auto_tasks:
        group_start(f"Auto-Discover ({len(auto_tasks)})")
        for t in auto_tasks:
            try:
                res = process_task_logic(
                    t["strategy"], t["type"], t["owner"],
                    t["filename"], t["inputs"], t["description"],
                )
                if res:
                    STATS["success"] += 1
                    STATS["total_rules"] += res["opt"]
                    res["file"] = f"(Auto) {res['file']}"
                    SUMMARY_ROWS.append(res)
                    success(f"  {t['filename']} -> {res['opt']} rules")
            except Exception as e:
                STATS["failed"] += 1
                ERROR_LOGS.append(f"auto task '{t['filename']}': {str(e)}")
                warning(f"  [FAIL] {t['filename']}: {e}")
        group_end()

    section(f"Merge Report | ok:{STATS['success']} skip:{STATS['skipped']} fail:{STATS['failed']}")

    if SUMMARY_ROWS:
        for r in SUMMARY_ROWS:
            info(f"  {r['file']:<30} {r['path']:<40} {r['mode']:<10} {r['opt']:>6} rules")

    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a", encoding="utf-8") as f:
            f.write(f"\n### Merge Report: {STATS['success']} OK, {STATS['failed']} Failed\n\n")
            if ERROR_LOGS:
                f.write("```diff\n" + "\n".join([f"- {e}" for e in ERROR_LOGS]) + "\n```\n")
            f.write("| File | Output Path | Rules |\n|---|---|---|\n")
            for r in SUMMARY_ROWS:
                f.write(f"| `{r['file']}` | `{r['path']}` | **{r['opt']}** |\n")

    if STATS["failed"] > 0:
        error("failed tasks exist, exiting")
        sys.exit(1)


if __name__ == "__main__":
    main()
