"""
阶段② 规则合并器 — Hybrid Clean 模式。

主要改进：
- 统一日志模块替代 rich.console / print
- 调用 utils.flatten_ip_cidr 统一 CIDR 聚合
- 原子写入替代直接写文件
- 读统一配置 config.yaml（兼容 merge-config.yaml）
"""
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

# ---------- 常量 ----------
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
    """通用的任务处理核心逻辑"""
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
            raise FileNotFoundError(f"源文件未找到: {rel_input}")

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
                warning(f"    ⚠ 无效 CIDR: {bad_cidr} → {err_msg}")
    else:
        final_list = sorted(list(combined_rules))

    opt_count = len(final_list)

    # 原子写入
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
    """扫描 rulesets 文件夹，发现未在配置中指定的文件（自动透传）"""
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
                "description": f"自动透传自 {rel_path_norm}",
            })

    return discovered_tasks


def main():
    section("🚀 规则合并器")

    # 读取配置
    actual_config = CONFIG_FILE if os.path.exists(CONFIG_FILE) else FALLBACK_CONFIG
    if not os.path.exists(actual_config):
        warning(f"配置文件 '{actual_config}' 未找到，仅使用自动模式")
        config_tasks = []
    else:
        cfg = load_config()
        config_tasks = cfg.get("merges", [])
        info(f"  从 {actual_config} 加载 {len(config_tasks)} 个合并任务")

    if not os.path.exists(SOURCE_DIR):
        error(f"源目录 '{SOURCE_DIR}' 不存在！")
        sys.exit(1)

    # 清空输出目录
    if os.path.exists(OUTPUT_DIR):
        info("  🧹 清理输出目录...")
        for item in os.listdir(OUTPUT_DIR):
            item_path = os.path.join(OUTPUT_DIR, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception as e:
                warning(f"  清理失败: {item_path} → {e}")
    else:
        os.makedirs(OUTPUT_DIR)

    # 执行配置合并任务
    if config_tasks:
        group_start(f"📋 配置合并任务 ({len(config_tasks)})")
        for t in config_tasks:
            fname = t.get("filename", "Unknown")
            try:
                if "inputs" not in t:
                    raise ValueError("缺少 inputs")
                res = process_task_logic(
                    t.get("strategy", "Default"),
                    t.get("type", "General"),
                    t.get("owner", "Unknown"),
                    fname,
                    t["inputs"],
                    t.get("description", "配置合并"),
                )
                if res:
                    STATS["success"] += 1
                    STATS["total_rules"] += res["opt"]
                    SUMMARY_ROWS.append(res)
                    success(f"  {fname} → {res['opt']} 条")
                else:
                    STATS["skipped"] += 1
            except Exception as e:
                STATS["failed"] += 1
                ERROR_LOGS.append(f"配置任务 '{fname}': {str(e)}")
                warning(f"  ✖ {fname}: {e}")
        group_end()

    # 执行自动发现
    auto_tasks = auto_discover_files()
    if auto_tasks:
        group_start(f"🔍 自动发现透传 ({len(auto_tasks)})")
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
                    success(f"  {t['filename']} → {res['opt']} 条")
            except Exception as e:
                STATS["failed"] += 1
                ERROR_LOGS.append(f"自动任务 '{t['filename']}': {str(e)}")
                warning(f"  ✖ {t['filename']}: {e}")
        group_end()

    # 汇总
    section(f"📊 合并报告 | 成功:{STATS['success']} 跳过:{STATS['skipped']} 失败:{STATS['failed']}")

    if SUMMARY_ROWS:
        for r in SUMMARY_ROWS:
            info(f"  {r['file']:<30} {r['path']:<40} {r['mode']:<10} {r['opt']:>6} 条")

    # GitHub Summary
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write(f"### 🚀 合并报告: {STATS['success']} OK, {STATS['failed']} Failed\n\n")
            if ERROR_LOGS:
                f.write("```diff\n" + "\n".join([f"- {e}" for e in ERROR_LOGS]) + "\n```\n")
            f.write("| 文件 | 输出路径 | 规则数 |\n|---|---|---|\n")
            for r in SUMMARY_ROWS:
                f.write(f"| `{r['file']}` | `{r['path']}` | **{r['opt']}** |\n")

    if STATS["failed"] > 0:
        error("存在失败任务，退出")
        sys.exit(1)


if __name__ == "__main__":
    main()
