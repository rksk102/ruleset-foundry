#!/usr/bin/env python3
import os
import sys
import yaml
import ipaddress

# =========================
# 配置区域
# =========================
CONFIG_FILE = "merge-config.yaml"  # 你现在的配置文件名
SOURCE_DIR = "rulesets"            # 输入源目录
OUTPUT_DIR = "merged-rules"        # 输出目录

STATS = {
    "tasks": 0,
    "files_read": 0,
    "rules_generated": 0,
    "errors": []
}

# =========================
# 辅助函数：漂亮的日志
# =========================
def gh_group_start(title):
    print(f"::group::🧩 {title}")
    sys.stdout.flush()

def gh_group_end():
    print("::endgroup::")
    sys.stdout.flush()

def log_info(msg):
    print(f"\033[1;34m[INFO]\033[0m {msg}")

def log_ok(msg):
    print(f"\033[1;32m[OK]\033[0m   {msg}")

def log_warn(msg):
    print(f"::warning::{msg}")
    print(f"\033[1;33m[WARN]\033[0m {msg}")

def log_err(msg):
    print(f"::error::{msg}")
    print(f"\033[1;31m[ERR]\033[0m  {msg}")
    STATS["errors"].append(msg)

def fatal_exit(msg):
    """严重错误立即停止"""
    log_err(msg)
    print("\n\033[1;41m CRITICAL FAILURE \033[0m Stop.")
    sys.exit(1)

# =========================
# 智能逻辑
# =========================

def detect_rule_type(path_str):
    """
    根据输出路径判断是 IP 规则还是 域名 规则。
    逻辑：如果路径里包含 'ip' 或 'cidr'，就启用 IP 智能合并模式。
    """
    lower_path = path_str.lower()
    if 'ip' in lower_path or 'cidr' in lower_path:
        return 'ipcidr'
    return 'domain'

def flatten_ip(cidr_set):
    """IP CIDR 智能聚合"""
    try:
        nets = [ipaddress.ip_network(c.strip(), strict=False) for c in cidr_set if c.strip()]
        collapsed = ipaddress.collapse_addresses(nets)
        return [str(n) for n in collapsed]
    except Exception as e:
        log_warn(f"CIDR merge logic hit an error ({e}), falling back to simple sort.")
        return sorted(list(cidr_set))

def merge_group(task):
    # 1. 解析任务信息
    relative_output_path = task.get('name')
    description = task.get('description', 'No Check')
    inputs = task.get('inputs', [])

    if not relative_output_path or not inputs:
        fatal_exit(f"Invalid config in merge-config.yaml. Name or Inputs missing.")

    gh_group_start(f"Task: {relative_output_path}")
    log_info(f"Desc: {description}")

    # 2. 智能判断处理模式
    mode = detect_rule_type(relative_output_path)
    log_info(f"Mode Detected: \033[1;36m{mode.upper()}\033[0m (based on filename)")

    combined_rules = set()
    files_read_count = 0

    # 3. 读取所有输入文件
    for rel_path in inputs:
        # 构建完整输入路径
        src_path = os.path.join(SOURCE_DIR, rel_path)
        
        if not os.path.exists(src_path):
            log_err(f"Source missing: {src_path}")
            # 如果你想严格到文件缺失就报错，去掉下面这行的注释
            fatal_exit(f"Required source file not found: {src_path}") 
            continue
        
        try:
            with open(src_path, 'r', encoding='utf-8') as f:
                count = 0
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or line.startswith('//'): continue
                    combined_rules.add(line)
                    count += 1
                # log_info(f"Loaded {count} rules from {os.path.basename(src_path)}")
                files_read_count += 1
        except Exception as e:
            fatal_exit(f"Read error on {src_path}: {e}")

    if files_read_count == 0:
        log_warn("No files were read for this task. Skipping output.")
        gh_group_end()
        return None

    # 4. 处理合并 (排序或聚合)
    log_info(f"Processing {len(combined_rules)} unique lines...")
    
    if mode == 'ipcidr':
        final_list = flatten_ip(combined_rules)
    else:
        final_list = sorted(list(combined_rules))

    # 5. 写入输出文件
    # 构建输出绝对路径
    full_output_path = os.path.join(OUTPUT_DIR, relative_output_path)
    
    # 自动创建父级目录 (例如 merged-rules/block/domain/rksk102/)
    os.makedirs(os.path.dirname(full_output_path), exist_ok=True)

    try:
        with open(full_output_path, 'w', encoding='utf-8') as f:
            # 添加头部信息
            f.write(f"# Merged Rule: {os.path.basename(relative_output_path)}\n")
            f.write(f"# Description: {description}\n")
            f.write(f"# Count: {len(final_list)}\n")
            f.write(f"# Mode: {mode}\n")
            f.write("-" * 20 + "\n")
            f.write("\n".join(final_list))
            f.write("\n")
        
        log_ok(f"Generated: {full_output_path}")
        log_ok(f"Final Count: {len(final_list)}")
    except Exception as e:
        fatal_exit(f"Write error: {e}")

    gh_group_end()
    
    return {
        "file": relative_output_path,
        "inputs": files_read_count,
        "count": len(final_list),
        "mode": mode
    }

def main():
    # 1. 检查环境
    if not os.path.exists(CONFIG_FILE):
        fatal_exit(f"Config file missing: {CONFIG_FILE}")
    
    if not os.path.exists(SOURCE_DIR):
        fatal_exit(f"Source directory '{SOURCE_DIR}' missing. Run Sync first!")

    # 清理并重建输出目录
    if os.path.exists(OUTPUT_DIR):
        import shutil
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR)

    # 2. 解析 YAML
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            task_list = data.get('merges', [])
    except Exception as e:
        fatal_exit(f"YAML parse error: {e}")

    if not task_list:
        fatal_exit(f"No 'merges' found in {CONFIG_FILE}")

    log_info(f"Found {len(task_list)} merge tasks.")

    # 3. 执行循环
    report_data = []
    for task in task_list:
        res = merge_group(task)
        if res:
            STATS["tasks"] += 1
            STATS["files_read"] += res['inputs']
            STATS["rules_generated"] += res['count']
            report_data.append(res)

    # 4. 报告
    if STATS["errors"]:
        fatal_exit(f"Process finished with {len(STATS['errors'])} errors.")

    print(f"::notice::Merge Success! Generated {STATS['rules_generated']} rules.")
    
    # 生成 Markdown 摘要
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.getenv('GITHUB_STEP_SUMMARY'), 'a') as f:
            f.write("## 🧩 Merge Execution Report\n\n")
            f.write("| Output File | Type | Sources | **Count** |\n")
            f.write("| :--- | :---: | :---: | :---: |\n")
            for item in report_data:
                # 这里的 :broken_heart: 是给空文件用的，可选
                icon = "📄" if item['mode'] == 'domain' else "🌐"
                f.write(f"| `{item['file']}` | {icon} {item['mode']} | {item['inputs']} | **{item['count']}** |\n")
            f.write(f"\n**Summary**: Processed `{STATS['tasks']}` config blocks.\n")

if __name__ == "__main__":
    main()
