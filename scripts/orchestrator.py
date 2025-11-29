import os
import json
import subprocess
import time
import sys
from datetime import datetime

PLAN_FILE = "workflow_plan.json"
SUMMARY_FILE = os.getenv("GITHUB_STEP_SUMMARY")

class Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    
    ICON_WAIT = "⏳"
    ICON_OK = "✅"
    ICON_FAIL = "❌"
    ICON_RUN = "🚀"

def log_group_start(title):
    print(f"::group::{Style.BOLD}{Style.CYAN}▶ {title} {Style.RESET}")
    sys.stdout.flush()

def log_group_end():
    print("::endgroup::")
    sys.stdout.flush()

def print_banner(text):
    print(f"\n{Style.BOLD}{Style.GREEN}{'='*60}")
    print(f" {text}")
    print(f"{'='*60}{Style.RESET}\n")

def get_latest_run(workflow_file, retry=3):
    """尝试多次获取最新的 Run ID"""
    for _ in range(retry):
        time.sleep(3)
        try:
            cmd = ["gh", "run", "list", "--workflow", workflow_file, "--limit", "1", "--json", "databaseId,url,status,conclusion"]
            res = subprocess.check_output(cmd).decode()
            data = json.loads(res)
            if data: return data[0]
        except:
            pass
    return None

def format_time(seconds):
    if seconds < 60: return f"{int(seconds)}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"

def generate_mermaid_chart(results):
    """生成 Mermaid 流程图代码"""
    graph = ["graph LR"]
    graph.append("    START((🚀 开始)) --> N0")
    
    for i, res in enumerate(results):
        status_style = "stroke:#333,stroke-width:2px"
        if res['status'] == 'success':
            status_style = "fill:#e6ffec,stroke:#2da44e,stroke-width:2px,color:#1a7f37"
        elif res['status'] == 'failure':
            status_style = "fill:#ffebe9,stroke:#cf222e,stroke-width:2px,color:#cf222e"
        elif res['status'] == 'skipped':
            status_style = "stroke-dasharray: 5 5"

        node_id = f"N{i}"
        safe_name = res['name'].replace(" ", "_")
        time_label = f"<br/>⏱️ {format_time(res['duration'])}" if res['duration'] > 0 else ""
        
        graph.append(f"    {node_id}[{res['name']}{time_label}]")
        graph.append(f"    style {node_id} {status_style}")

        if i < len(results) - 1:
            graph.append(f"    {node_id} --> N{i+1}")
    
    last_status = results[-1]['status'] if results else 'success'
    end_node = "END_OK(((✅ 完成)))" if last_status == 'success' else "END_FAIL(((❌ 中断)))"
    graph.append(f"    N{len(results)-1} --> {end_node}")
    
    if last_status == 'success':
        graph.append(f"    style END_OK fill:#2da44e,stroke:#fff,color:#fff")
    else:
        graph.append(f"    style END_FAIL fill:#cf222e,stroke:#fff,color:#fff")

    return "\n".join(graph)

def write_summary(results, total_time):
    if not SUMMARY_FILE: return

    success_count = sum(1 for r in results if r['status'] == 'success')
    is_all_pass = (success_count == len(results)) and len(results) > 0
    md = f"# 🕹️ 自动化构建控制台\n\n"

    if is_all_pass:
        md += f"> ### ✅ 构建成功\n> **总耗时**: {format_time(total_time)} &nbsp;|&nbsp; **执行时间**: {datetime.utcnow().strftime('%H:%M UTC')}\n\n"
    else:
        md += f"> ### ❌ 构建失败\n> 请检查下方红色节点。\n\n"

    md += "### 🗺️ 执行路径图\n"
    md += "```mermaid\n"
    md += generate_mermaid_chart(results)
    md += "\n```\n\n"
    md += "### 📋 任务详细报告\n"
    md += "| 步骤 | 任务名 | 结果 | 耗时 | 日志链接 |\n"
    md += "| :--- | :--- | :---: | :---: | :--- |\n"
    
    for i, res in enumerate(results):
        icon = Style.ICON_WAIT
        if res['status'] == 'success': icon = Style.ICON_OK
        elif res['status'] == 'failure': icon = Style.ICON_FAIL
        elif res['status'] == 'skipped': icon = "🚫"
        
        link = f"[🔗 点击查看]({res['url']})" if res['url'] else "-"
        
        md += f"| **{i+1}** | {res['name']} | {icon} | {format_time(res['duration'])} | {link} |\n"

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write(md)

def run():
    start_total = time.time()
    
    if not os.path.exists(PLAN_FILE):
        print("::error::❌ 缺少配置文件 workflow_plan.json")
        exit(1)

    with open(PLAN_FILE, 'r') as f:
        plan = json.load(f)

    print_banner(f"启动编排系统 - 计划任务数: {len(plan)}")
    
    results = []
    abort_flow = False

    for idx, task in enumerate(plan):
        job_start = time.time()
        res = {
            "name": task['name'],
            "filename": task['filename'],
            "status": "pending",
            "url": "",
            "duration": 0
        }
        
        if abort_flow:
            res['status'] = 'skipped'
            print(f"🚫 [跳过] {task['name']} (因上游失败)")
            results.append(res)
            continue
            
        log_group_start(f"正在执行 [{idx+1}/{len(plan)}]: {task['name']}")
        print(f"📄 目标文件: {task['filename']}")
        
        try:
            print(f"{Style.ICON_RUN} 正在发送触发指令...")
            subprocess.run(["gh", "workflow", "run", task['filename']], check=True)
            
            print("⏳ 等待 GitHub 创建运行实例...")
            run_info = get_latest_run(task['filename'])
            
            if run_info:
                res['url'] = run_info['url']
                run_id = run_info['databaseId']
                print(f"🔗 任务已创建: {run_info['url']} (ID: {run_id})")

                if task.get('wait', True):
                    print(f"\n{Style.YELLOW}>>> 进入同步监控模式 (实时日志将流式传输) <<<{Style.RESET}")
                    subprocess.run(["gh", "run", "watch", str(run_id), "--exit-status"], check=True)
                    print(f"\n{Style.GREEN}✅ 任务执行成功{Style.RESET}")
                    res['status'] = 'success'
                else:
                    print("⚡ 异步任务 - 已触发但不等待结果")
                    res['status'] = 'success'
            else:
                print("::warning::无法获取 Run ID，无法追踪状态")
                res['status'] = 'unknown'

        except subprocess.CalledProcessError:
            print(f"\n{Style.RED}❌ 任务执行失败！{Style.RESET}")
            res['status'] = 'failure'
            abort_flow = True
            print("::error::关键路径中断，停止后续任务")

        except Exception as e:
            print(f"::error::系统异常: {e}")
            res['status'] = 'failure'
            abort_flow = True

        res['duration'] = time.time() - job_start
        results.append(res)
        log_group_end()
        if idx < len(plan) - 1 and not abort_flow:
            time.sleep(2)

    total_time = time.time() - start_total
    write_summary(results, total_time)
    
    if abort_flow:
        print_banner("❌ 流程异常结束")
        exit(1)
    else:
        print_banner("✅ 流程圆满完成")

if __name__ == "__main__":
    run()
