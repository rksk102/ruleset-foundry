#!/usr/bin/env python3
import os
import sys
import yaml
import ipaddress
import time
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.traceback import install

# 美化报错堆栈
install(show_locals=True)
console = Console()

# =========================
# 配置
# =========================
CONFIG_FILE = "merge-config.yaml"
SOURCE_DIR = "rulesets"
OUTPUT_DIR = "merged-rules"

# 统计与日志容器
STATS = {
    "success": 0,
    "skipped": 0,
    "failed": 0,
    "total_rules": 0
}
ERROR_LOGS = []
SUMMARY_ROWS = []

# =========================
# 功能函数
# =========================

def detect_mode(type_str, filename):
    """根据类型或文件名判断处理模式"""
    check_str = (type_str + filename).lower()
    if 'ip' in check_str or 'cidr' in check_str:
        return 'IP-CIDR'
    return 'DOMAIN'

def flatten_ip_cidr(cidr_set):
    """
    IP CIDR 聚合去重 (修复版)
    自动分离 IPv4 和 IPv6 进行处理，防止版本混合报错
    """
    ipv4_nets = []
    ipv6_nets = []

    for c in cidr_set:
        c = c.strip()
        if not c: continue
        try:
            net = ipaddress.ip_network(c, strict=False)
            if net.version == 4:
                ipv4_nets.append(net)
            else:
                ipv6_nets.append(net)
        except ValueError as e:
            # 如果 IP 格式完全错误，可以选择报错或跳过
            # 这里选择抛出异常，保持严格模式
            raise ValueError(f"Invalid CIDR format '{c}': {e}")

    # 分别进行聚合计算
    # collapse_addresses 只能处理同版本的 IP 列表
    collapsed_v4 = ipaddress.collapse_addresses(ipv4_nets)
    collapsed_v6 = ipaddress.collapse_addresses(ipv6_nets)

    # 将结果转回字符串并合并
    result = [str(n) for n in collapsed_v4] + [str(n) for n in collapsed_v6]
    return result

def process_single_task(task_config):
    """
    处理单个具体任务
    """
    # 1. 校验必填项
    required_fields = ['strategy', 'type', 'owner', 'filename', 'inputs']
    for field in required_fields:
        if field not in task_config:
            raise ValueError(f"Config missing field: '{field}'")

    strategy = task_config['strategy']
    rule_type = task_config['type']
    owner = task_config['owner']
    filename = task_config['filename']
    inputs = task_config['inputs']
    desc = task_config.get('description', 'No Description')

    # 2. 构建强制目录结构
    relative_dir = os.path.join(strategy, rule_type, owner)
    full_output_dir = os.path.join(OUTPUT_DIR, relative_dir)
    full_output_file = os.path.join(full_output_dir, filename)

    # 3. 读取源文件
    combined_rules = set()
    files_read = 0

    for rel_input in inputs:
        src_path = os.path.join(SOURCE_DIR, rel_input)
        if not os.path.exists(src_path):
            # 抛出文件找不到的异常，这会被主循环捕获并记录为 Failure
            raise FileNotFoundError(f"Source file not found: {src_path}")
        
        with open(src_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('//'): 
                    continue
                if '#' in line: line = line.split('#')[0].strip()
                combined_rules.add(line)
            files_read += 1

    if files_read == 0 and inputs:
        # 如果 input 有配置但没文件读到（虽然上面已经 raise 了，这里是双重保险）
        return None

    # 4. 处理逻辑
    mode = detect_mode(rule_type, filename)
    count_raw = len(combined_rules)
    
    # 这里调用修复后的 flatten_ip_cidr
    if mode == 'IP-CIDR':
        final_list = flatten_ip_cidr(combined_rules)
    else:
        final_list = sorted(list(combined_rules))
    
    count_opt = len(final_list)

    # 5. 写入结果
    os.makedirs(full_output_dir, exist_ok=True)
    with open(full_output_file, 'w', encoding='utf-8') as f:
        f.write(f"# ----------------------------------------\n")
        f.write(f"# Strategy: {strategy}\n")
        f.write(f"# Type:     {rule_type}\n")
        f.write(f"# Owner:    {owner}\n")
        f.write(f"# Date:     {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Mode:     {mode}\n")
        f.write(f"# Count:    {count_opt} (Raw: {count_raw})\n")
        f.write(f"# Desc:     {desc}\n")
        f.write(f"# ----------------------------------------\n")
        f.write("\n".join(final_list))
        f.write("\n")

    return {
        "file": filename,
        "path": f"{strategy}/{rule_type}/{owner}",
        "mode": mode,
        "src_count": files_read,
        "raw": count_raw,
        "opt": count_opt
    }

# =========================
# 主程序
# =========================

def main():
    console.rule("[bold blue]🚀 Rule Merger & Validator[/bold blue]")

    # 环境检查
    if not os.path.exists(CONFIG_FILE):
        console.print(f"[bold red]❌ CRITICAL: Config '{CONFIG_FILE}' not found![/bold red]")
        sys.exit(1)
    if not os.path.exists(SOURCE_DIR):
        console.print(f"[bold red]❌ CRITICAL: Directory '{SOURCE_DIR}' not found![/bold red]")
        sys.exit(1)

    # 清理输出目录
    if os.path.exists(OUTPUT_DIR):
        import shutil
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR)

    # 加载 YAML
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f) or {}
            tasks = config_data.get('merges', [])
    except Exception as e:
        console.print(f"[bold red]❌ YAML Parsing Error:[/bold red] {e}")
        sys.exit(1)

    if not tasks:
        console.print("[yellow]⚠️ Config file is empty.[/yellow]")
        sys.exit(0)

    # 执行循环
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:
        
        main_task = progress.add_task("[cyan]Processing Rules[/cyan]", total=len(tasks))

        for t in tasks:
            t_name = t.get('filename', 'Unknown')
            progress.update(main_task, description=f"Processing: {t_name}")
            
            try:
                result = process_single_task(t)
                if result:
                    STATS["success"] += 1
                    STATS["total_rules"] += result['opt']
                    SUMMARY_ROWS.append(result)
                else:
                    STATS["skipped"] += 1
            except Exception as e:
                STATS["failed"] += 1
                error_msg = f"Task '{t_name}' failed: {str(e)}"
                ERROR_LOGS.append(error_msg)
                console.print(f"  [bold red]❌ Error:[/bold red] {error_msg}")
            
            progress.advance(main_task)

    # 终端表格报告
    table = Table(title="Execution Result", header_style="bold magenta")
    table.add_column("File", style="cyan")
    table.add_column("Directory (Output)", style="dim")
    table.add_column("Mode")
    table.add_column("Rules", justify="right", style="green")

    for r in SUMMARY_ROWS:
        table.add_row(r['file'], r['path'], r['mode'], str(r['opt']))
    
    console.print("\n")
    console.print(table)

    # GitHub Actions Summary
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.getenv('GITHUB_STEP_SUMMARY'), 'a') as f:
            f.write("### 🧩 Rule Processing Report\n\n")
            f.write(f"- ✅ **Success**: {STATS['success']}\n")
            f.write(f"- ❌ **Failed**: {STATS['failed']}\n")
            
            if ERROR_LOGS:
                f.write("\n> [!CAUTION]\n> **The following errors occurred:**\n\n")
                f.write("```diff\n")
                for err in ERROR_LOGS:
                    f.write(f"- {err}\n")
                f.write("```\n\n")

            f.write("#### 📋 Details\n")
            f.write("| File | Path | Inputs | Optimized Count |\n")
            f.write("| :--- | :--- | :---: | :---: |\n")
            for r in SUMMARY_ROWS:
                f.write(f"| `{r['file']}` | `{r['path']}` | {r['src_count']} | **{r['opt']}** |\n")

    # 退出状态
    if STATS["failed"] > 0:
        console.print(Panel(f"[bold red]Workflow Failed with {STATS['failed']} errors![/bold red]\nCheck logs above.", title="FAILURE", border_style="red"))
        sys.exit(1)
    else:
        console.print(Panel(f"[bold green]All {STATS['success']} tasks completed successfully.[/bold green]", title="SUCCESS", border_style="green"))
        sys.exit(0)

if __name__ == "__main__":
    main()
