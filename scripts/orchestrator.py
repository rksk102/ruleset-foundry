"""
流水线调度器 — 解析 workflow_plan.json，顺序触发各 GitHub Actions Workflow。

主要改进：
- 统一日志模块
"""
import os
import json
import subprocess
import time
import sys
from datetime import datetime

from logger import banner, info, success, error, warning, get_logger

logger = get_logger()

PLAN_FILE = "workflow_plan.json"
SUMMARY_FILE = os.getenv("GITHUB_STEP_SUMMARY")


def log_group_start(title):
    print(f"::group::{title}")
    sys.stdout.flush()


def log_group_end():
    print("::endgroup::")
    sys.stdout.flush()


def get_latest_run(workflow_file, retry=3):
    """尝试多次获取最新的 Run ID"""
    for _ in range(retry):
        time.sleep(3)
        try:
            cmd = [
                "gh", "run", "list",
                "--workflow", workflow_file,
                "--limit", "1",
                "--json", "databaseId,url,status,conclusion",
            ]
            res = subprocess.check_output(cmd).decode()
            data = json.loads(res)
            if data:
                return data[0]
        except Exception:
            pass
    return None


def format_time(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


def generate_mermaid_chart(results):
    """生成 Mermaid 流程图"""
    graph = ["graph LR"]
    graph.append("    START((🚀 开始)) --> N0")

    for i, res in enumerate(results):
        status_style = "stroke:#333,stroke-width:2px"
        if res["status"] == "success":
            status_style = "fill:#e6ffec,stroke:#2da44e,stroke-width:2px,color:#1a7f37"
        elif res["status"] == "failure":
            status_style = "fill:#ffebe9,stroke:#cf222e,stroke-width:2px,color:#cf222e"
        elif res["status"] == "skipped":
            status_style = "stroke-dasharray: 5 5"

        node_id = f"N{i}"
        time_label = (
            f"<br/>⏱️ {format_time(res['duration'])}"
            if res["duration"] > 0
            else ""
        )
        graph.append(f"    {node_id}[{res['name']}{time_label}]")
        graph.append(f"    style {node_id} {status_style}")

        if i < len(results) - 1:
            graph.append(f"    {node_id} --> N{i+1}")

    last_status = results[-1]["status"] if results else "success"
    end_node = (
        "END_OK(((✅ 完成)))"
        if last_status == "success"
        else "END_FAIL(((❌ 中断)))"
    )
    graph.append(f"    N{len(results) - 1} --> {end_node}")

    if last_status == "success":
        graph.append("    style END_OK fill:#2da44e,stroke:#fff,color:#fff")
    else:
        graph.append("    style END_FAIL fill:#cf222e,stroke:#fff,color:#fff")

    return "\n".join(graph)


def write_summary(results, total_time):
    if not SUMMARY_FILE:
        return

    success_count = sum(1 for r in results if r["status"] == "success")
    is_all_pass = (success_count == len(results)) and len(results) > 0
    md = "# 🕹️ 自动化构建控制台\n\n"

    if is_all_pass:
        md += (
            f"> ### ✅ 构建成功\n"
            f"> **总耗时**: {format_time(total_time)} &nbsp;|&nbsp; "
            f"**执行时间**: {datetime.utcnow().strftime('%H:%M UTC')}\n\n"
        )
    else:
        md += "> ### ❌ 构建失败\n> 请检查下方红色节点。\n\n"

    md += "### 🗺️ 执行路径图\n"
    md += "```mermaid\n"
    md += generate_mermaid_chart(results)
    md += "\n```\n\n"
    md += "### 📋 任务详细报告\n"
    md += "| 步骤 | 任务名 | 结果 | 耗时 | 日志链接 |\n"
    md += "| :--- | :--- | :---: | :---: | :--- |\n"

    icon_map = {
        "success": "✅",
        "failure": "❌",
        "skipped": "🚫",
    }
    for i, res in enumerate(results):
        icon = icon_map.get(res["status"], "⏳")
        link = f"[🔗 点击查看]({res['url']})" if res["url"] else "-"
        md += (
            f"| **{i + 1}** | {res['name']} | {icon} | "
            f"{format_time(res['duration'])} | {link} |\n"
        )

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write(md)


def run():
    start_total = time.time()

    if not os.path.exists(PLAN_FILE):
        error(f"❌ 缺少配置文件 {PLAN_FILE}")
        sys.exit(1)

    with open(PLAN_FILE, "r") as f:
        plan = json.load(f)

    banner(f"启动编排系统 - 计划任务数: {len(plan)}")

    results = []
    abort_flow = False

    for idx, task in enumerate(plan):
        job_start = time.time()
        res = {
            "name": task["name"],
            "filename": task["filename"],
            "status": "pending",
            "url": "",
            "duration": 0,
        }

        if abort_flow:
            res["status"] = "skipped"
            info(f"🚫 [跳过] {task['name']} (因上游失败)")
            results.append(res)
            continue

        log_group_start(f"正在执行 [{idx + 1}/{len(plan)}]: {task['name']}")
        info(f"📄 目标文件: {task['filename']}")

        try:
            info("🚀 正在发送触发指令...")
            subprocess.run(["gh", "workflow", "run", task["filename"]], check=True)

            info("⏳ 等待 GitHub 创建运行实例...")
            run_info = get_latest_run(task["filename"])

            if run_info:
                res["url"] = run_info["url"]
                run_id = run_info["databaseId"]
                info(f"🔗 任务已创建: {run_info['url']} (ID: {run_id})")

                if task.get("wait", True):
                    info(">>> 进入同步监控模式 <<<")
                    subprocess.run(
                        ["gh", "run", "watch", str(run_id), "--exit-status"],
                        check=True,
                    )
                    success(f"✅ 任务执行成功")
                    res["status"] = "success"
                else:
                    info("⚡ 异步任务 - 已触发但不等待结果")
                    res["status"] = "success"
            else:
                warning("无法获取 Run ID，无法追踪状态")
                res["status"] = "unknown"

        except subprocess.CalledProcessError:
            error(f"❌ 任务执行失败！")
            res["status"] = "failure"
            abort_flow = True
            error("关键路径中断，停止后续任务")

        except Exception as e:
            error(f"系统异常: {e}")
            res["status"] = "failure"
            abort_flow = True

        res["duration"] = time.time() - job_start
        results.append(res)
        log_group_end()

        if idx < len(plan) - 1 and not abort_flow:
            time.sleep(2)

    total_time = time.time() - start_total
    write_summary(results, total_time)

    if abort_flow:
        banner("❌ 流程异常结束")
        sys.exit(1)
    else:
        banner("✅ 流程圆满完成")


if __name__ == "__main__":
    run()
