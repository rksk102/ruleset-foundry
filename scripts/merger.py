import os
import sys
import yaml
import ipaddress
import time
import shutil
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.traceback import install

install(show_locals=True)
console = Console()
CONFIG_FILE = "merge-config.yaml"
SOURCE_DIR = "rulesets"
OUTPUT_DIR = "merged-rules"

STATS = {
    "success": 0,
    "skipped": 0,
    "failed": 0,
    "total_rules": 0
}
ERROR_LOGS = []
SUMMARY_ROWS = []

USED_SOURCE_FILES = set()

def normalize_path(p):
    """标准化路径分隔符"""
    return str(Path(p).as_posix())

def detect_mode(type_str, filename):
    check_str = (str(type_str) + str(filename)).lower()
    if 'ip' in check_str or 'cidr' in check_str:
        return 'IP-CIDR'
    return 'DOMAIN'

def flatten_ip_cidr(cidr_set):
    """IPv4/IPv6 分离聚合算法"""
    ipv4_nets = []
    ipv6_nets = []
    for c in cidr_set:
        c = c.strip()
        if not c: continue
        try:
            net = ipaddress.ip_network(c, strict=False)
            if net.version == 4: ipv4_nets.append(net)
            else: ipv6_nets.append(net)
        except ValueError as e:
            raise ValueError(f"Invalid CIDR '{c}': {e}")
    
    v4_res = [str(n) for n in ipaddress.collapse_addresses(ipv4_nets)]
    v6_res = [str(n) for n in ipaddress.collapse_addresses(ipv6_nets)]
    return v4_res + v6_res

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
            raise FileNotFoundError(f"Source file not found: {rel_input}")
        
        with open(full_src_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('//'): continue
                if '#' in line: line = line.split('#')[0].strip()
                combined_rules.add(line)
            files_read_count += 1

    if files_read_count == 0 and inputs:
        return None

    mode = detect_mode(rule_type, filename)
    raw_count = len(combined_rules)
    
    if mode == 'IP-CIDR':
        final_list = flatten_ip_cidr(combined_rules)
    else:
        final_list = sorted(list(combined_rules))
    
    opt_count = len(final_list)

    os.makedirs(full_output_dir, exist_ok=True)
    with open(full_output_file, 'w', encoding='utf-8') as f:
        f.write(f"# ----------------------------------------\n")
        f.write(f"# Strategy: {strategy}\n")
        f.write(f"# Type:     {rule_type}\n")
        f.write(f"# Owner:    {owner}\n")
        f.write(f"# Date:     {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Mode:     {mode}\n")
        f.write(f"# Count:    {opt_count} (Raw: {raw_count})\n")
        f.write(f"# Desc:     {desc}\n")
        f.write(f"# ----------------------------------------\n")
        f.write("\n".join(final_list))
        f.write("\n")

    return {
        "file": filename,
        "path": f"{strategy}/{rule_type}/{owner}",
        "mode": mode,
        "src_count": files_read_count,
        "raw": raw_count,
        "opt": opt_count
    }

def auto_discover_files():
    """扫描 rulesets 文件夹，发现未使用的文件"""
    discovered_tasks = []
    if not os.path.exists(SOURCE_DIR):
        return []

    for root, dirs, files in os.walk(SOURCE_DIR):
        for file in files:
            if file.startswith('.') or not file.endswith('.txt'):
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
                "description": f"Auto-detected from {rel_path_norm}"
            })
            
    return discovered_tasks


def main():
    console.rule("[bold blue]🚀 Hybrid Merger (Smart Clean)[/bold blue]")

    if not os.path.exists(CONFIG_FILE):
        console.print(f"[yellow]⚠️ Warning: Config '{CONFIG_FILE}' not found. Will use Auto-Mode only.[/yellow]")
    
    if not os.path.exists(SOURCE_DIR):
        console.print(f"[bold red]❌ CRITICAL: Directory '{SOURCE_DIR}' not found![/bold red]")
        sys.exit(1)

    if os.path.exists(OUTPUT_DIR):
        console.print("[dim]🧹 Cleaning output directory...[/dim]")
        for item in os.listdir(OUTPUT_DIR):
            item_path = os.path.join(OUTPUT_DIR, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception as e:
                console.print(f"[yellow]Warning: Failed to delete {item_path}: {e}[/yellow]")
    else:
        os.makedirs(OUTPUT_DIR)

    config_tasks = []
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = yaml.safe_load(f) or {}
                config_tasks = data.get('merges', [])
        except Exception as e:
            console.print(f"[red]Config Error:[/red] {e}")
            sys.exit(1)
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:

        if config_tasks:
            task_main = progress.add_task("[cyan]Running Config Tasks[/cyan]", total=len(config_tasks))
            for t in config_tasks:
                try:
                    fname = t.get('filename', 'Unknown')
                    progress.update(task_main, description=f"Config Task: {fname}")
                    
                    if 'inputs' not in t: raise ValueError("Missing inputs")
                    
                    res = process_task_logic(
                        t.get('strategy', 'Default'), t.get('type', 'General'),
                        t.get('owner', 'Unknown'), fname, t['inputs'],
                        t.get('description', 'Configured Merge')
                    )
                    if res:
                        STATS['success'] += 1
                        STATS['total_rules'] += res['opt']
                        SUMMARY_ROWS.append(res)
                    else:
                        STATS['skipped'] += 1
                except Exception as e:
                    STATS['failed'] += 1
                    ERROR_LOGS.append(f"Config Task '{fname}': {str(e)}")
                progress.advance(task_main)

        auto_tasks = auto_discover_files()
        if auto_tasks:
            task_auto = progress.add_task("[magenta]Running Auto-Discovery[/magenta]", total=len(auto_tasks))
            for t in auto_tasks:
                try:
                    progress.update(task_auto, description=f"Auto: {t['filename']}")
                    res = process_task_logic(
                        t['strategy'], t['type'], t['owner'], 
                        t['filename'], t['inputs'], t['description']
                    )
                    if res:
                        STATS['success'] += 1
                        STATS['total_rules'] += res['opt']
                        res['file'] = f"(Auto) {res['file']}"
                        SUMMARY_ROWS.append(res)
                except Exception as e:
                    STATS['failed'] += 1
                    ERROR_LOGS.append(f"Auto Task '{t['filename']}': {str(e)}")
                progress.advance(task_auto)

    table = Table(title="Execution Summary", header_style="bold magenta")
    table.add_column("File", style="cyan")
    table.add_column("Output Path", style="dim")
    table.add_column("Mode")
    table.add_column("Rules", justify="right", style="green")

    for r in SUMMARY_ROWS:
        table.add_row(r['file'], r['path'], r['mode'], str(r['opt']))
    
    console.print("\n")
    console.print(table)

    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.getenv('GITHUB_STEP_SUMMARY'), 'a') as f:
            f.write(f"### 🚀 Rule Report: {STATS['success']} OK, {STATS['failed']} Failed\n\n")
            if ERROR_LOGS:
                f.write("```diff\n" + "\n".join([f"- {e}" for e in ERROR_LOGS]) + "\n```\n")
            f.write("| File | Output Path | Rules |\n|---|---|---|\n")
            for r in SUMMARY_ROWS:
                f.write(f"| `{r['file']}` | `{r['path']}` | **{r['opt']}** |\n")

    if STATS["failed"] > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
