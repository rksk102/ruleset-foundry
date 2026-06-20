import os
import sys
import time
from pathlib import Path

from logger import info, success, warning, error, group_start, group_end, section, get_logger
from config_loader import load_config, get
from utils import (
    normalize_path,
    flatten_ip_cidr,
    dedup_domain_suffix,
    atomic_write_with_header,
    clean_directory,
)

logger = get_logger()

CONFIG_FILE = "config.yaml"
SOURCE_DIR = get("paths", "rulesets_dir", default="rulesets")
OUTPUT_DIR = get("paths", "merged_output_dir", default="merged-rules")

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
                warning(f"    无效 CIDR: {bad_cidr} -> {err_msg}")
        dedup_removed = 0
    else:
        final_list, dedup_removed = dedup_domain_suffix(combined_rules)

    opt_count = len(final_list)

    count_desc = f"{opt_count} (Raw: {raw_count})"
    if dedup_removed > 0:
        count_desc += f" | Dedup: -{dedup_removed}"

    metadata = {
        "strategy": strategy,
        "type": rule_type,
        "owner": owner,
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "count": count_desc,
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
                "description": f"自动透传自 {rel_path_norm}",
            })

    return discovered_tasks


def load_domains_from_file(filepath):
    """从规则文件中加载域名集合（跳过注释和空行）。"""
    domains = set()
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            domains.add(line.lower())
    return domains


def _build_domain_trie(domains):
    """构建倒序标签 Trie，用于高效检测父子域名关系。"""
    trie = {}
    for domain in domains:
        parts = domain.split(".")
        parts.reverse()
        node = trie
        for part in parts:
            if part not in node:
                node[part] = {}
            node = node[part]
        node["_mark"] = True
    return trie


def _find_covering_parent(domain, trie):
    """在 Trie 中查找 domain 的已标记祖先域名，返回 (祖先域名, 是否找到)。

    沿 domain 的标签路径搜索，若遇到已标记节点则返回该祖先的域名。
    """
    parts = domain.split(".")
    parts.reverse()
    node = trie
    matched_parts = []
    for part in parts:
        if part not in node:
            break
        node = node[part]
        matched_parts.append(part)
        if node.get("_mark") and len(matched_parts) < len(parts):
            # 找到祖先（不能是自身，必须是严格祖先）
            ancestor = ".".join(reversed(matched_parts))
            return ancestor, True
    return None, False


def detect_cross_policy_conflicts(merged_dir):
    """检测跨策略的域名冲突，包括显式冲突和隐式冲突。

    显式冲突：同一域名同时出现在多个策略中。
    隐式冲突：一个策略中的父域名覆盖另一个策略中的子域名
    （如 google.com 在 policy 中，adservice.google.com 在 block 中，
    suffix 匹配下 google.com 会覆盖 adservice.google.com）。
    """
    policy_domains = {}

    if not os.path.exists(merged_dir):
        return {}, {}

    for strategy_dir in Path(merged_dir).iterdir():
        if not strategy_dir.is_dir():
            continue
        domains = set()
        for txt_file in strategy_dir.rglob("*.txt"):
            domains.update(load_domains_from_file(str(txt_file)))
        if domains:
            policy_domains[strategy_dir.name] = domains

    if len(policy_domains) < 2:
        return {}, {}

    # 显式冲突：同一域名出现在多个策略中
    strategies = sorted(policy_domains.keys())
    explicit_conflicts = {}
    for i, s1 in enumerate(strategies):
        for s2 in strategies[i + 1:]:
            overlap = policy_domains[s1] & policy_domains[s2]
            if overlap:
                explicit_conflicts[f"{s1} ↔ {s2}"] = sorted(overlap)

    # 隐式冲突：一个策略的父域名覆盖另一个策略的子域名
    # 为每个策略构建 Trie
    tries = {s: _build_domain_trie(d) for s, d in policy_domains.items()}

    # 定义需要检测的覆盖方向（父域策略 → 子域策略）
    # block 子域被其他策略父域覆盖是最危险的
    implicit_conflicts = {}
    for parent_strategy, parent_trie in tries.items():
        for child_strategy, child_domains in policy_domains.items():
            if parent_strategy == child_strategy:
                continue
            key = f"{parent_strategy}(父) → {child_strategy}(子)"
            items = []
            for domain in sorted(child_domains):
                ancestor, found = _find_covering_parent(domain, parent_trie)
                if found:
                    items.append((domain, ancestor))
            if items:
                implicit_conflicts[key] = items

    return explicit_conflicts, implicit_conflicts


def main():
    section("规则合并器")

    if not os.path.exists(CONFIG_FILE):
        warning(f"配置文件 '{CONFIG_FILE}' 未找到，仅使用自动模式")
        config_tasks = []
    else:
        cfg = load_config()
        config_tasks = cfg.get("merges", [])
        info(f"  从 {CONFIG_FILE} 加载 {len(config_tasks)} 个合并任务")

    if not os.path.exists(SOURCE_DIR):
        error(f"源目录 '{SOURCE_DIR}' 不存在！")
        sys.exit(1)

    if os.path.exists(OUTPUT_DIR):
        info("  清理输出目录...")
        clean_directory(OUTPUT_DIR)
    else:
        os.makedirs(OUTPUT_DIR)

    if config_tasks:
        group_start(f"配置合并任务 ({len(config_tasks)})")
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
                    success(f"  {fname} -> {res['opt']} 条规则")
                else:
                    STATS["skipped"] += 1
            except Exception as e:
                STATS["failed"] += 1
                ERROR_LOGS.append(f"配置任务 '{fname}': {str(e)}")
                warning(f"  [失败] {fname}: {e}")
        group_end()

    auto_tasks = auto_discover_files()
    if auto_tasks:
        group_start(f"自动发现透传 ({len(auto_tasks)})")
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
                    success(f"  {t['filename']} -> {res['opt']} 条规则")
            except Exception as e:
                STATS["failed"] += 1
                ERROR_LOGS.append(f"自动任务 '{t['filename']}': {str(e)}")
                warning(f"  [失败] {t['filename']}: {e}")
        group_end()

    section(f"合并报告 | 成功:{STATS['success']} 跳过:{STATS['skipped']} 失败:{STATS['failed']}")

    if SUMMARY_ROWS:
        for r in SUMMARY_ROWS:
            info(f"  {r['file']:<30} {r['path']:<40} {r['mode']:<10} {r['opt']:>6} 条")

    # 跨策略冲突检测
    explicit_conflicts, implicit_conflicts = detect_cross_policy_conflicts(OUTPUT_DIR)

    if explicit_conflicts:
        group_start("显式冲突（同一域名出现在多个策略中）")
        total_explicit = sum(len(v) for v in explicit_conflicts.values())
        warning(f"  发现 {total_explicit} 个显式冲突域名")
        for pair, domains in explicit_conflicts.items():
            warning(f"  {pair}: {len(domains)} 个冲突")
            for d in domains[:10]:
                warning(f"    - {d}")
            if len(domains) > 10:
                warning(f"    ... 及其他 {len(domains) - 10} 个")
        group_end()

    if implicit_conflicts:
        group_start("隐式冲突（父域名覆盖其他策略的子域名）")
        for pair, items in implicit_conflicts.items():
            warning(f"  {pair}: {len(items)} 个子域被覆盖")
            for child, parent in items[:10]:
                warning(f"    - {child} 被 {parent} 覆盖")
            if len(items) > 10:
                warning(f"    ... 及其他 {len(items) - 10} 个")
        group_end()

    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a", encoding="utf-8") as f:
            f.write(f"\n### 合并报告: {STATS['success']} OK, {STATS['failed']} Failed\n\n")
            if ERROR_LOGS:
                f.write("```diff\n" + "\n".join([f"- {e}" for e in ERROR_LOGS]) + "\n```\n")
            f.write("| 文件 | 输出路径 | 规则数 |\n|---|---|---|\n")
            for r in SUMMARY_ROWS:
                f.write(f"| `{r['file']}` | `{r['path']}` | **{r['opt']}** |\n")

            if explicit_conflicts:
                f.write("\n### 显式冲突检测\n\n")
                f.write("> 以下域名同时出现在不同策略中，请确保 rules 顺序为 block > direct > policy\n\n")
                for pair, domains in explicit_conflicts.items():
                    f.write(f"**{pair}** ({len(domains)} 个冲突)\n\n")
                    sample = domains[:20]
                    for d in sample:
                        f.write(f"- `{d}`\n")
                    if len(domains) > 20:
                        f.write(f"- ... 及其他 {len(domains) - 20} 个\n")
                    f.write("\n")

            if implicit_conflicts:
                f.write("\n### 隐式冲突检测\n\n")
                f.write("> 以下子域名虽在低优先级策略中，但其父域名在高优先级策略中，")
                f.write("suffix 匹配下父域名会覆盖子域名。请确保 rules 顺序为 block > direct > policy\n\n")
                for pair, items in implicit_conflicts.items():
                    f.write(f"**{pair}** ({len(items)} 个子域被覆盖)\n\n")
                    sample = items[:20]
                    for child, parent in sample:
                        f.write(f"- `{child}` 被 `{parent}` 覆盖\n")
                    if len(items) > 20:
                        f.write(f"- ... 及其他 {len(items) - 20} 个\n")
                    f.write("\n")

    if STATS["failed"] > 0:
        error("存在失败任务，退出")
        sys.exit(1)


if __name__ == "__main__":
    main()
