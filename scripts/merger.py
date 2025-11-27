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
from rich import print as rprint

# 安装 Rich 异常捕获，报错更好看
install(show_locals=True)

console = Console()

# =========================
# 配置区域
# =========================
CONFIG_FILE = "merge-config.yaml"
SOURCE_DIR = "rulesets"
OUTPUT_DIR = "merged-rules"

STATS = {
    "success": 0,
    "skipped": 0,
    "failed": 0,
    "total_rules": 0
}

# =========================
# 逻辑部分
# =========================

def detect_mode_by_type(type_str, filename):
    """
    根据配置的 Type 或文件名智能判断模式
    """
    check_str = (type_str + filename).lower()
    if 'ip' in check_str or 'cidr' in check_str:
        return 'IP-CIDR'
    return 'DOMAIN'

def flatten_ip(cidr_set):
    """IP CIDR 智能聚合"""
    try:
        # 过滤掉空行和注释
        nets = [ipaddress.ip_network(c.strip(), strict=False) for c in cidr_set if c.strip()]
        # 核心优化：合并重叠网段
        collapsed = ipaddress.collapse_addresses(nets)
        return [str(n) for n in collapsed]
    except ValueError as e:
        console.print(f"[bold red]❌ IP Format Error:[/bold red] {e}")
        # 如果解析失败，回退到普通文本排序
        return sorted(list(cidr_set))

def process_task(task, progress, task_id):
    """处理单个任务"""
    
    # 1. 获取并校验必要字段
    try:
        strategy = task.get('strategy', 'Uncategorized')
        rule_type = task.get('type', 'General')
        owner = task.get('owner', 'Unknown')
        filename = task.get('filename')
        inputs = task.get('inputs', [])
        desc = task.get('description', 'No description')

        if not filename or not inputs:
            raise ValueError("Missing 'filename' or 'inputs' in config.")
    except Exception as e:
        console.print(f"[bold red]⚠️ Config Error:[/bold red] {e}")
        STATS["failed"] += 1
        return None

    # 2. 构建标准输出路径: merged-rules/[Strategy]/[Type]/[Owner]/[File]
    rel_path = os.path.join(strategy, rule_type, owner, filename)
    full_output_path = os.path.join(OUTPUT_DIR, rel_path)
    
    progress.update(task_id, description=f"[cyan]Processing:[/cyan] {filename}")

    # 3. 智能模式识别
    mode = detect_mode_by_type(rule_type, filename)
    
    combined_rules = set()
    files_read_count = 0

    # 4. 读取输入文件 (不移动源文件，只读取内容)
    for rel_input in inputs:
        src_path = os.path.join(SOURCE_DIR, rel_input)
        
        if not os.path.exists(src_path):
            console.print(f"  [yellow]⚠️ Source missing:[/yellow] {rel_input}")
            continue
        
        try:
            with open(src_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    # 忽略注释、空行
                    if not line or line.startswith('#') or line.startswith('//') or line.startswith('!'):
                        continue
                    # 简单的行内注释清理 (例如: 1.1.1.1 # Cloudflare -> 1.1.1.1)
                    if '#' in line: line = line.split('#')[0].strip()
                    
                    combined_rules.add(line)
                files_read_count += 1
        except Exception as e:
            console.print(f"  [bold red]❌ Read Error:[/bold red] {rel_input} -> {e}")

    if files_read_count == 0:
        progress.update(task_id, description=f"[yellow]Skipped:[/yellow] {filename}")
        STATS["skipped"] += 1
        return None

    # 5. 数据处理 (去重、排序、聚合)
    original_count = len(combined_rules)
    
    if mode == 'IP-CIDR':
        final_list = flatten_ip(combined_rules)
    else:
        final_list = sorted(list(combined_rules))
    
    final_count = len(final_list)
    STATS["total_rules"] += final_count

    # 6. 写入文件
    os.makedirs(os.path.dirname(full_output_path), exist_ok=True)
    
    with open(full_output_path, 'w', encoding='utf-8') as f:
        # 写入漂亮的标准头
        f.write(f"# ----------------------------------------\n")
        f.write(f"# Strategy: {strategy}\n")
        f.write(f"# Type:     {rule_type}\n")
        f.write(f"# Owner:    {owner}\n")
        f.write(f"# Date:     {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Count:    {final_count} (Optimized from {original_count})\n")
        f.write(f"# Desc:     {desc}\n")
        f.write(f"# ----------------------------------------\n")
        f.write("\n".join(final_list))
        f.write("\n")

    STATS["success"] += 1
    
    # 返回元组供汇总表使用
    return (filename, f"{strategy}/{owner}", mode, str(files_read_count), str(original_count), str(final_count))

def main():
    console.rule("[bold blue]🚀 Rule Merger & Optimizer[/bold blue]")
    
    # 1. 环境检查
    if not os.path.exists(CONFIG_FILE):
        console.print(f"[bold red]❌ Config file not found:[/bold red] {CONFIG_FILE}")
        sys.exit(1)

    if not os.path.exists(SOURCE_DIR):
        console.print(f"[bold red]❌ Source directory not found:[/bold red] {SOURCE_DIR}")
        sys.exit(1)

    # 清理重建输出目录
    if os.path.exists(OUTPUT_DIR):
        import shutil
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR)

    # 2. 加载配置
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
        tasks = config.get('merges', [])
    
    if not tasks:
        console.print("[yellow]⚠️ No tasks found in config.[/yellow]")
        sys.exit(0)

    console.print(f"[green]Found {len(tasks)} tasks to process.[/green]\n")

    results = []

    # 3. 执行主循环 (带进度条)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:
        
        main_task = progress.add_task("[green]Processing...[/green]", total=len(tasks))
        
        for task_conf in tasks:
            res = process_task(task_conf, progress, main_task)
            if res:
                results.append(res)
            progress.advance(main_task)

    # 4. 生成汇总表格
    table = Table(title="🧩 Merge Result Summary", show_header=True, header_style="bold magenta")
    table.add_column("Filename", style="cyan")
    table.add_column("Path Context", style="dim")
    table.add_column("Mode", justify="center")
    table.add_column("Sources", justify="right")
    table.add_column("Raw", justify="right", style="red")
    table.add_column("Optimized", justify="right", style="green")

    for row in results:
        table.add_row(*row)

    console.print("\n")
    console.print(table)

    # 5. 最终状态面板
    summary_panel = Panel(
        f"[green]Success: {STATS['success']}[/green] | "
        f"[yellow]Skipped: {STATS['skipped']}[/yellow] | "
        f"[red]Failed: {STATS['failed']}[/red]\n"
        f"[bold]Total Rules Generated: {STATS['total_rules']}[/bold]",
        title="Execution Finished",
        expand=False
    )
    console.print(summary_panel)

    # 6. 写入 GHA Summary
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.getenv('GITHUB_STEP_SUMMARY'), 'a') as f:
            f.write("## 🚀 Rule Merge Summary\n")
            f.write(f"- **Total Files Generated:** {STATS['success']}\n")
            f.write(f"- **Total Rules:** {STATS['total_rules']}\n\n")
            f.write("| File | Context | Mode | Inputs | Count |\n")
            f.write("| :--- | :--- | :---: | :---: | :---: |\n")
            for r in results:
                # r: filename, path, mode, sources, raw, optimized
                f.write(f"| `{r[0]}` | `{r[1]}` | {r[2]} | {r[3]} | **{r[5]}** |\n")

    if STATS["failed"] > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
