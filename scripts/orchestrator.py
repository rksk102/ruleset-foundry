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
    graph = ["graph LR"]
    graph.append("    START((Start)) --> N0")

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
            f"<br/>{format_time(res['duration'])}"
            if res["duration"] > 0
            else ""
        )
        graph.append(f"    {node_id}[{res['name']}{time_label}]")
        graph.append(f"    style {node_id} {status_style}")

        if i < len(results) - 1:
            graph.append(f"    {node_id} --> N{i+1}")

    last_status = results[-1]["status"] if results else "success"
    end_node = (
        "END_OK(((OK)))"
        if last_status == "success"
        else "END_FAIL(((ABORT)))"
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
    md = "# Build Console\n\n"

    if is_all_pass:
        md += (
            f"> ### Build Success\n"
            f"> **Total**: {format_time(total_time)} | "
            f"**Time**: {datetime.utcnow().strftime('%H:%M UTC')}\n\n"
        )
    else:
        md += "> ### Build Failed\n> Check red nodes below.\n\n"

    md += "### Execution Graph\n"
    md += "```mermaid\n"
    md += generate_mermaid_chart(results)
    md += "\n```\n\n"
    md += "### Task Report\n"
    md += "| Step | Task | Result | Elapsed | Log |\n"
    md += "| :--- | :--- | :---: | :---: | :--- |\n"

    icon_map = {
        "success": "[OK]",
        "failure": "[FAIL]",
        "skipped": "[SKIP]",
    }
    for i, res in enumerate(results):
        icon = icon_map.get(res["status"], "[...]")
        link = f"[View]({res['url']})" if res["url"] else "-"
        md += (
            f"| **{i + 1}** | {res['name']} | {icon} | "
            f"{format_time(res['duration'])} | {link} |\n"
        )

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write(md)


def run():
    start_total = time.time()

    if not os.path.exists(PLAN_FILE):
        error(f"missing config {PLAN_FILE}")
        sys.exit(1)

    with open(PLAN_FILE, "r") as f:
        plan = json.load(f)

    banner(f"Orchestrator - {len(plan)} task(s)")

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
            info(f"[SKIP] {task['name']} (upstream failed)")
            results.append(res)
            continue

        log_group_start(f"Executing [{idx + 1}/{len(plan)}]: {task['name']}")
        info(f"target: {task['filename']}")

        try:
            info("triggering...")
            subprocess.run(["gh", "workflow", "run", task["filename"]], check=True)

            info("waiting for run instance...")
            run_info = get_latest_run(task["filename"])

            if run_info:
                res["url"] = run_info["url"]
                run_id = run_info["databaseId"]
                info(f"run created: {run_info['url']} (ID: {run_id})")

                if task.get("wait", True):
                    info(">>> monitoring <<<")
                    subprocess.run(
                        ["gh", "run", "watch", str(run_id), "--exit-status"],
                        check=True,
                    )
                    success(f"task completed")
                    res["status"] = "success"
                else:
                    info("async task - triggered")
                    res["status"] = "success"
            else:
                warning("cannot get Run ID, unable to track")
                res["status"] = "unknown"

        except subprocess.CalledProcessError:
            error(f"task failed!")
            res["status"] = "failure"
            abort_flow = True
            error("critical path stopped")

        except Exception as e:
            error(f"system error: {e}")
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
        banner("ABORTED")
        sys.exit(1)
    else:
        banner("DONE")


if __name__ == "__main__":
    run()
