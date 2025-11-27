#!/usr/bin/env python3
import json
import subprocess
import sys
import time
import os

# =================配置=================
PLAN_FILE = "workflow_plan.json"
# =====================================

def log(msg, level="info"):
    icons = {"info": "ℹ️", "success": "✅", "error": "❌", "wait": "⏳"}
    print(f"{icons.get(level, '')} {msg}")
    sys.stdout.flush()

def run_command(cmd):
    """执行 Shell 命令并返回输出"""
    try:
        result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        log(f"Command failed: {cmd}\nError: {e.stderr}", "error")
        return None

def trigger_workflow(workflow_file):
    """触发工作流"""
    log(f"Triggering {workflow_file}...", "wait")
    # 使用 gh workflow run 触发
    if run_command(f"gh workflow run {workflow_file} --ref {os.getenv('GITHUB_REF_NAME', 'main')}") is not None:
        return True
    return False

def get_latest_run_id(workflow_file):
    """获取某工作流正在运行的最新 Run ID"""
    # 等待几秒让 GitHub API 刷新
    time.sleep(5)
    # 获取最新的一个 run (无论状态)
    output = run_command(f"gh run list --workflow {workflow_file} --limit 1 --json databaseId,status --jq '.[0]'")
    if output:
        data = json.loads(output)
        return data['databaseId']
    return None

def watch_workflow(run_id, timeout_mins):
    """阻塞等待工作流完成"""
    log(f"Watching run ID: {run_id} (Timeout: {timeout_mins}m)...", "wait")
    
    # 使用 gh run watch 自动轮询直到结束
    # --exit-status 会让命令在工作流失败时返回非 0 值
    cmd = f"gh run watch {run_id} --exit-status"
    
    try:
        # 这里不使用 run_command 因为我们需要实时看到 watch 的输出（如果有的话），或者单纯阻塞
        # 但 gh run watch 默认很安静，我们手动处理超时
        subprocess.run(cmd, shell=True, check=True, timeout=timeout_mins * 60)
        return True
    except subprocess.TimeoutExpired:
        log(f"Workflow timed out after {timeout_mins} minutes!", "error")
        return False
    except subprocess.CalledProcessError:
        log("Workflow failed!", "error")
        return False

def main():
    if not os.path.exists(PLAN_FILE):
        log(f"Plan file {PLAN_FILE} not found!", "error")
        sys.exit(1)

    with open(PLAN_FILE, 'r', encoding='utf-8') as f:
        plan = json.load(f)

    print(f"::group::🚀 Starting Orchestrator for {len(plan)} workflows")
    
    for step in plan:
        name = step['name']
        file = step['file']
        timeout = step.get('timeout_minutes', 20)

        print(f"\n----------------------------------------")
        log(f"Step: {name} ({file})", "info")
        
        # 1. 记录当前最新的 ID (防止捕捉到旧的)
        # old_id = get_latest_run_id(file) 
        # 实际上 gh run watch 逻辑比较智能，我们这里采用直接 Trigger 后获取最新的策略
        
        # 2. 触发
        if not trigger_workflow(file):
            log(f"Failed to trigger {name}", "error")
            sys.exit(1)

        # 3. 获取刚刚触发的 ID
        # 稍微等待 GitHub 生成 ID
        time.sleep(3)
        current_id = get_latest_run_id(file)
        
        if not current_id:
            log(f"Could not find run ID for {file}", "error")
            sys.exit(1)

        # 4. 监控直到结束
        if watch_workflow(current_id, timeout):
            log(f"Step {name} finished successfully!", "success")
        else:
            log(f"Step {name} failed or timed out. Stopping orchestrator.", "error")
            sys.exit(1) # 只要有一步失败，整个链条停止

    print("----------------------------------------")
    print("::endgroup::")
    log("🎉 All workflows in the plan completed successfully!", "success")

if __name__ == "__main__":
    main()
