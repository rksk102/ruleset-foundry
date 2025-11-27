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
ERROR_LOGS = []     # 收集错误信息
SUMMARY_ROWS = []   # 收集成功信息用于报告

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
    """IP CIDR 聚合去重"""
    try:
        nets = [ipaddress.ip_network(c.strip(), strict=False) for c in cidr_set if c.strip()]
        collapsed = ipaddress.collapse_addresses(nets)
        return [str(n) for n in collapsed]
    except ValueError as e:
        # 这是一个严重的数据错误，不应该忽略，应该抛出让 Task 失败
        raise ValueError(f"Invalid CIDR format: {e}")

def process_single_task(task_config):
    """
    处理单个具体任务
    返回: dict 成功结果 | 抛出 Exception 失败
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

    # 2. 构建强制目录结构: merged-rules/Strategy/Type/Owner/File
    # os.path.join 会处理路径分隔符
    relative_dir = os.path.join(strategy, rule_type, owner)
    full_output_dir = os.path.join(OUTPUT_DIR, relative_dir)
    full_output_file = os.path.join(full_output_dir, filename)

    # 3. 读取源文件
    combined_rules = set()
    files_read = 0

    for rel_input in inputs:
        src_path = os.path.join(SOURCE_DIR, rel_input)
        if not os.path.exists(src_path):
            # 严重错误：配置了文件但找不到
            raise FileNotFoundError(f"Source file not found: {src_path}")
        
        with open(src_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('//'): 
                    continue
                # 简单的行内注释清理
                if '#' in line: line = line.split('#')[0].strip()
                combined_rules.add(line)
            files_read += 1

    if files_read == 0:
        return None # 即使没有读取到文件，如果是 inputs 为空，视为空任务跳过

    # 4. 处理逻辑 (去重/聚合)
    mode = detect_mode(rule_type, filename)
    count_raw = len(combined_rules)
    
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

    # 1. 环境检查
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
        console.print("[yellow]⚠️ Config file is empty (no 'merges' section).[/yellow]")
        sys.exit(0)

    # 2. 执行循环
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

    # 3. 终端表格报告
    table = Table(title="Execution Result", header_style="bold magenta")
    table.add_column("File", style="cyan")
    table.add_column("Directory (Output)", style="dim")
    table.add_column("Mode")
    table.add_column("Rules", justify="right", style="green")

    for r in SUMMARY_ROWS:
        table.add_row(r['file'], r['path'], r['mode'], str(r['opt']))
    
    console.print("\n")
    console.print(table)

    # 4. 生成 GitHub Actions Summary (Markdown)
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.getenv('GITHUB_STEP_SUMMARY'), 'a') as f:
            f.write("### 🧩 Rule Processing Report\n\n")
            
            # 概览
            f.write(f"- ✅ **Success**: {STATS['success']}\n")
            f.write(f"- ⏭️ **Skipped**: {STATS['skipped']}\n")
            f.write(f"- ❌ **Failed**: {STATS['failed']}\n")
            f.write(f"- 📊 **Total Rules**: {STATS['total_rules']}\n\n")

            # 错误部分 (高亮)
            if ERROR_LOGS:
                f.write("#### ❌ Failures (Action Needed)\n")
                f.write("```diff\n")
                for err in ERROR_LOGS:
                    f.write(f"- {err}\n")
                f.write("```\n\n")

            # 成功明细表
            f.write("#### 📋 Details\n")
            f.write("| File | Output Path | Type | Raw | **Optimized** |\n")
            f.write("| :--- | :--- | :---: | :---: | :---: |\n")
            for r in SUMMARY_ROWS:
                f.write(f"| `{r['file']}` | `{r['path']}` | {r['mode']} | {r['raw']} | **{r['opt']}** |\n")

    # 5. 决定退出状态 (Fail Fast)
    if STATS["failed"] > 0:
        console.print(Panel(f"[bold red]Workflow Failed with {STATS['failed']} errors![/bold red]\nCheck logs above.", title="FAILURE", border_style="red"))
        sys.exit(1) # 这会让 Github Action 变红并停止
    else:
        console.print(Panel(f"[bold green]All {STATS['success']} tasks completed successfully.[/bold green]", title="SUCCESS", border_style="green"))
        sys.exit(0)

if __name__ == "__main__":
    main()
