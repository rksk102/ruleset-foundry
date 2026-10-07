import os
import sys
from pathlib import Path

import manifest
from config_loader import CONFLICT_POLICIES, get, load_config
from logger import error, gh_error, group_end, group_start, info, section, success, warning
from utils import (
    DomainTrie,
    anchor_cwd_to_repo_root,
    atomic_write_with_header,
    beijing_timestamp,
    clean_directory,
    dedup_domain_suffix,
    flatten_ip_cidr,
    normalize_path,
)

CONFIG_FILE = "config.yaml"
SOURCE_DIR = get("paths", "rulesets_dir", default="rulesets")
OUTPUT_DIR = get("paths", "merged_output_dir", default="merged-rules")
REPO_ROOT = Path(__file__).resolve().parent.parent


def detect_mode(type_str):
    return "IP-CIDR" if "ipcidr" in str(type_str).lower() else "DOMAIN"


def merge_product_paths(merge_tasks):
    paths = set()
    for task in merge_tasks or []:
        if not isinstance(task, dict):
            continue
        parts = [task.get(key) for key in ("strategy", "type", "owner", "filename")]
        if all(isinstance(part, str) and part.strip() for part in parts):
            paths.add("/".join(part.strip().strip("/").replace("\\", "/") for part in parts))
    return paths


def missing_merge_inputs(merge_tasks, base_dir=None):
    return manifest.merge_inputs(merge_tasks, base_dir or SOURCE_DIR)


def verify_merged_products(merge_tasks, output_dir=None, manifest_file=None):
    out_dir = output_dir or OUTPUT_DIR
    manifest_file = manifest_file or os.path.join(SOURCE_DIR, manifest.MANIFEST_NAME)
    baseline = manifest.load_manifest(manifest_file)
    if not baseline:
        raise manifest.ManifestError(
            f"清单基线缺失: {manifest_file} 不存在或为空（需先运行 scripts/main.py）"
        )
    expected = sorted(set(baseline) | merge_product_paths(merge_tasks))
    actual = sorted(manifest.collect_files(out_dir, ".txt"))
    return manifest.verify_matches(expected, actual, f"{out_dir} 绝对基准校验")


def _ensure_within(base_dir, target_path, what):
    root = Path(base_dir).resolve()
    target = Path(target_path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"{what}路径越界，拒绝访问: {target}（须位于 {root} 内）")
    return target


def _ensure_repo_anchored(label, path):
    target = Path(path).resolve()
    if target != REPO_ROOT and REPO_ROOT not in target.parents:
        error(f"{label}不在仓库内，拒绝继续: {target}（仓库根 {REPO_ROOT}）")
        gh_error(f"{label}不在仓库内，拒绝继续: {target}")
        sys.exit(1)


def process_task_logic(strategy, rule_type, owner, filename, inputs, desc):
    relative_dir = os.path.join(strategy, rule_type, owner)
    full_output_dir = os.path.join(OUTPUT_DIR, relative_dir)
    full_output_file = os.path.join(full_output_dir, filename)
    _ensure_within(OUTPUT_DIR, full_output_dir, "合并输出")
    _ensure_within(full_output_dir, full_output_file, "合并输出")
    combined_rules = set()
    files_read_count = 0
    source_urls = []

    missing_files = []

    for rel_input in inputs:
        full_src_path = os.path.join(SOURCE_DIR, rel_input)
        _ensure_within(SOURCE_DIR, full_src_path, "合并输入")
        if not os.path.exists(full_src_path):
            missing_files.append(rel_input)
            continue

        with open(full_src_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    if line.startswith("# Source:"):
                        source_urls.append(line.split(":", 1)[1].strip())
                    continue
                if line.startswith("//"):
                    continue
                if "#" in line:
                    line = line.split("#")[0].strip()
                if line:
                    combined_rules.add(line)
            files_read_count += 1

    if missing_files:
        raise FileNotFoundError(f"合并输入缺失: {', '.join(missing_files)}")
    if files_read_count == 0 and inputs:
        warning(f"    无可用输入文件，跳过任务: {filename}")
        return None

    mode = detect_mode(rule_type)
    raw_count = len(combined_rules)

    if mode == "IP-CIDR":
        dropped_default_routes = []
        final_list, cidr_errors = flatten_ip_cidr(
            combined_rules, dropped_default_routes=dropped_default_routes)
        if dropped_default_routes:
            warning(f"    默认路由(/0) 已按设计丢弃: {len(dropped_default_routes)} 条")
        if cidr_errors:
            for bad_cidr, err_msg in cidr_errors[:5]:
                warning(f"    无效 CIDR: {bad_cidr} -> {err_msg}")
        dedup_removed = 0
    else:
        final_list, dedup_removed = dedup_domain_suffix(combined_rules)

    opt_count = len(final_list)

    if not final_list:
        raise ValueError(f"合并结果为空（0 条规则），拒绝写出只有表头的产物: {filename}")

    count_desc = f"{opt_count} (Raw: {raw_count})"
    if dedup_removed > 0:
        count_desc += f" | Dedup: -{dedup_removed}"

    metadata = {
        "strategy": strategy,
        "type": rule_type,
        "owner": owner,
        "date": beijing_timestamp(),
        "mode": mode,
        "count": count_desc,
        "desc": desc,
    }
    if source_urls:
        metadata["sources"] = " ".join(sorted(set(source_urls)))
    atomic_write_with_header(full_output_file, final_list, metadata)

    return {
        "file": filename,
        "path": f"{strategy}/{rule_type}/{owner}",
        "mode": mode,
        "src_count": files_read_count,
        "raw": raw_count,
        "opt": opt_count,
    }


def auto_discover_files(source_dir=None):
    discovered_tasks = []
    root_dir = source_dir or SOURCE_DIR
    if not os.path.exists(root_dir):
        return []

    for root, _dirs, files in os.walk(root_dir):
        for file in files:
            if file.startswith(".") or not file.endswith(".txt"):
                continue

            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, root_dir)
            rel_path_norm = normalize_path(rel_path)

            parts = Path(rel_path_norm).parent.parts
            if len(parts) < 3:
                continue

            discovered_tasks.append({
                "strategy": parts[0],
                "type": parts[1],
                "owner": parts[2],
                "filename": file,
                "inputs": [rel_path_norm],
                "description": f"自动透传自 {rel_path_norm}",
            })

    return discovered_tasks


def load_domains_from_file(filepath):
    domains = set()
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            domains.add(line.lower())
    return domains


IMPLICIT_SAMPLE_LIMIT = 3


def resolve_conflict_action(conflict_policy, has_conflicts):
    policy = (conflict_policy or "warn").lower()
    if policy not in CONFLICT_POLICIES:
        raise ValueError(f"behavior.conflict_policy 取值非法: {conflict_policy!r}")
    if not has_conflicts:
        return "none"
    return policy


def detect_cross_policy_conflicts(merged_dir):
    policy_domains = {}

    if not os.path.exists(merged_dir):
        return {}, {}

    for strategy_dir in Path(merged_dir).iterdir():
        if not strategy_dir.is_dir():
            continue
        domains = set()
        for txt_file in strategy_dir.rglob("*.txt"):
            if any("ipcidr" in part.lower() for part in txt_file.parts):
                continue
            domains.update(load_domains_from_file(str(txt_file)))
        if domains:
            policy_domains[strategy_dir.name] = domains

    if len(policy_domains) < 2:
        return {}, {}

    strategies = sorted(policy_domains.keys())

    def bare(entry):
        """去掉 `+.` / `.` 前缀，用于跨策略比较同一域名。"""
        return entry.lstrip("+.") if entry[:1] in ("+", ".") else entry

    def kind_of(entry):
        if entry.startswith("+."):
            return DomainTrie.SUFFIX
        if entry.startswith("."):
            return DomainTrie.SUBDOMAIN
        return DomainTrie.EXACT

    explicit_conflicts = {}
    for i, s1 in enumerate(strategies):
        for s2 in strategies[i + 1:]:
            left = {bare(d) for d in policy_domains[s1]}
            right = {bare(d) for d in policy_domains[s2]}
            overlap = left & right
            if overlap:
                explicit_conflicts[f"{s1} ↔ {s2}"] = sorted(overlap)

    tries = {}
    for strategy, domains in policy_domains.items():
        trie = DomainTrie()
        for domain in domains:
            trie.add(bare(domain), kind_of(domain))
        tries[strategy] = trie

    implicit_conflicts = {}
    for parent_strategy, parent_trie in tries.items():
        for child_strategy, child_domains in policy_domains.items():
            if parent_strategy == child_strategy:
                continue
            key = f"{parent_strategy}(父) → {child_strategy}(子)"
            items = []
            for domain in sorted(child_domains):
                ancestor = parent_trie.covering_parent(bare(domain), DomainTrie.SUFFIX)
                if ancestor:
                    items.append((domain, ancestor))
            if items:
                implicit_conflicts[key] = items

    return explicit_conflicts, implicit_conflicts


def main():
    section("规则合并器")
    _ensure_repo_anchored("源目录", SOURCE_DIR)
    _ensure_repo_anchored("输出目录", OUTPUT_DIR)

    stats = {"success": 0, "skipped": 0, "failed": 0}
    error_logs = []
    summary_rows = []

    if not os.path.exists(CONFIG_FILE):
        warning(f"配置文件 '{CONFIG_FILE}' 未找到，仅使用自动模式")
        config_tasks = []
    else:
        cfg = load_config()
        config_tasks = cfg.get("merges") or []
        info(f"  从 {CONFIG_FILE} 加载 {len(config_tasks)} 个合并任务")

    if not os.path.exists(SOURCE_DIR):
        error(f"源目录 '{SOURCE_DIR}' 不存在！")
        gh_error(f"源目录 '{SOURCE_DIR}' 不存在，合并中止")
        sys.exit(1)

    if config_tasks:
        missing_inputs = missing_merge_inputs(config_tasks)
        if missing_inputs:
            error(f"合并输入缺失 {len(missing_inputs)} 项（配置合并任务未执行，产物目录未改动）:")
            for rel in missing_inputs:
                error(f"    - {rel}")
            gh_error(
                f"合并输入缺失 {len(missing_inputs)} 项，配置合并任务未执行"
                f"（示例: {', '.join(missing_inputs[:3])}）"
            )
            sys.exit(1)

    auto_tasks = auto_discover_files()
    overlap = sorted(merge_product_paths(config_tasks) & merge_product_paths(auto_tasks))
    if overlap:
        for rel in overlap:
            error(f"合并任务与自动透传输出同一路径: {rel}")
        error("请改用不同的 owner/filename，或把该路径从 merges.inputs 中移除")
        gh_error(f"合并任务与自动透传输出同一路径: {', '.join(overlap)}")
        sys.exit(1)

    if os.path.exists(OUTPUT_DIR):
        info("  清理输出目录...")
        for path, why in clean_directory(OUTPUT_DIR):
            warning(f"  清理失败（可能残留陈旧产物）: {path} -> {why}")
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
                    stats["success"] += 1
                    summary_rows.append(res)
                    success(f"  {fname} -> {res['opt']} 条规则")
                else:
                    stats["skipped"] += 1
            except Exception as e:
                stats["failed"] += 1
                error_logs.append(f"配置任务 '{fname}': {e!s}")
                warning(f"  [失败] {fname}: {e}")
        group_end()

    if auto_tasks:
        group_start(f"自动发现透传 ({len(auto_tasks)})")
        for t in auto_tasks:
            try:
                res = process_task_logic(
                    t["strategy"], t["type"], t["owner"],
                    t["filename"], t["inputs"], t["description"],
                )
                if res:
                    stats["success"] += 1
                    res["file"] = f"(Auto) {res['file']}"
                    summary_rows.append(res)
                    success(f"  {t['filename']} -> {res['opt']} 条规则")
                else:
                    stats["skipped"] += 1
            except Exception as e:
                stats["failed"] += 1
                error_logs.append(f"自动任务 '{t['filename']}': {e!s}")
                warning(f"  [失败] {t['filename']}: {e}")
        group_end()

    if stats["failed"] == 0:
        expected_tasks = len(config_tasks) + len(auto_tasks)
        if stats["success"] + stats["skipped"] != expected_tasks:
            error(f"合并产出数量不一致: 期望 {expected_tasks}，实得 "
                  f"成功 {stats['success']} + 跳过 {stats['skipped']}")
            gh_error(
                f"合并产出数量不一致: 期望 {expected_tasks}，实得 "
                f"成功 {stats['success']} + 跳过 {stats['skipped']}"
            )
            sys.exit(1)
        try:
            verify_merged_products(config_tasks)
        except manifest.ManifestError as e:
            error(f"  {e}")
            gh_error(f"合并产物与清单基线不一致: {e}")
            sys.exit(1)
        info("  合并产物与清单基线一致")

    section(f"合并报告 | 成功:{stats['success']} 跳过:{stats['skipped']} 失败:{stats['failed']}")

    if summary_rows:
        for r in summary_rows:
            info(f"  {r['file']:<30} {r['path']:<40} {r['mode']:<10} {r['opt']:>6} 条")

    conflict_policy = get("behavior", "conflict_policy", default="warn")
    if str(conflict_policy or "warn").lower() == "ignore":
        explicit_conflicts, implicit_conflicts = {}, {}
    else:
        explicit_conflicts, implicit_conflicts = detect_cross_policy_conflicts(OUTPUT_DIR)
    has_conflicts = bool(explicit_conflicts or implicit_conflicts)
    try:
        action = resolve_conflict_action(conflict_policy, has_conflicts)
    except ValueError as e:
        error(str(e))
        gh_error(f"behavior.conflict_policy 配置非法: {e}")
        sys.exit(1)

    show_conflicts = action != "ignore"

    if show_conflicts and explicit_conflicts:
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

    if show_conflicts and implicit_conflicts:
        group_start("隐式冲突（父域名覆盖其他策略的子域名）")
        total_implicit = sum(len(v) for v in implicit_conflicts.values())
        warning(f"  共 {total_implicit} 个子域受父域规则影响（完整列表见 step summary）")
        for pair, items in implicit_conflicts.items():
            warning(f"  {pair}: {len(items)} 个子域被覆盖")
            for child, parent in items[:IMPLICIT_SAMPLE_LIMIT]:
                warning(f"    - {child} 被 {parent} 覆盖")
            if len(items) > IMPLICIT_SAMPLE_LIMIT:
                warning(f"    ... 及其他 {len(items) - IMPLICIT_SAMPLE_LIMIT} 个")
        group_end()

    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a", encoding="utf-8") as f:
            f.write(f"\n### 合并报告: {stats['success']} OK, {stats['failed']} Failed\n\n")
            if error_logs:
                f.write("```diff\n" + "\n".join([f"- {e}" for e in error_logs]) + "\n```\n")
            f.write("| 文件 | 输出路径 | 规则数 |\n|---|---|---|\n")
            for r in summary_rows:
                f.write(f"| `{r['file']}` | `{r['path']}` | **{r['opt']}** |\n")

            if show_conflicts and explicit_conflicts:
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

            if show_conflicts and implicit_conflicts:
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

    if action == "fail":
        error("检测到跨策略冲突，按配置终止合并")
        gh_error("检测到跨策略冲突，按 behavior.conflict_policy=fail 终止合并")
        sys.exit(1)

    if stats["failed"] > 0:
        error("存在失败任务，退出")
        gh_error(f"存在 {stats['failed']} 个合并任务失败，合并中止")
        sys.exit(1)


if __name__ == "__main__":
    anchor_cwd_to_repo_root()
    main()
