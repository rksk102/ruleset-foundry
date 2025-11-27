#!/usr/bin/env python3
import os
import sys
import shutil
import subprocess
import stat
import requests
import gzip
import json
import time

# ================= 配置区域 =================
SRC_ROOT = "merged-rules"
DST_ROOT = "merged-rules-mrs"
REPO_API = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
KERNEL_BIN = "./mihomo"
# ===========================================

# 终端颜色配置
class C:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    END = '\033[0m'
    BOLD = '\033[1m'

def log(msg, type="info"):
    ts = time.strftime("%H:%M:%S")
    if type == "info": print(f"{C.BLUE}[{ts} INFO]{C.END} {msg}")
    elif type == "succ": print(f"{C.GREEN}[{ts} SUCC]{C.END} {msg}")
    elif type == "warn": print(f"{C.WARNING}[{ts} WARN]{C.END} {msg}")
    elif type == "err":  print(f"{C.FAIL}[{ts} ERROR]{C.END} {msg}")
    elif type == "group": print(f"::group::{msg}")
    elif type == "endgroup": print("::endgroup::")

def get_latest_mihomo():
    """动态获取最新版 Mihomo 内核 (Linux-amd64)"""
    log("Fetching latest Mihomo release info...", "group")
    
    headers = {}
    if "GH_TOKEN" in os.environ:
        headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"

    try:
        # 1.用 API 获取最新 Release 信息
        resp = requests.get(REPO_API, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        tag_name = data['tag_name']
        log(f"Latest version identified: {C.BOLD}{tag_name}{C.END}")

        # 2. 筛选 linux-amd64 资源
        download_url = None
        for asset in data['assets']:
            # 匹配 linux-amd64 但排除 compatible (旧CPU兼容版)
            if "linux-amd64" in asset['name'] and "compatible" not in asset['name'] and asset['name'].endswith(".gz"):
                download_url = asset['browser_download_url']
                break
        
        if not download_url:
            raise Exception("No suitable linux-amd64 asset found in latest release.")

        # 3. 下载
        log(f"Downloading kernel from: {download_url}")
        dl_resp = requests.get(download_url, stream=True)
        dl_resp.raise_for_status()

        with gzip.GzipFile(fileobj=dl_resp.raw) as gz:
            with open(KERNEL_BIN, "wb") as f:
                shutil.copyfileobj(gz, f)
        
        st = os.stat(KERNEL_BIN)
        os.chmod(KERNEL_BIN, st.st_mode | stat.S_IEXEC)
        
        # 验证版本
        ver_o = subprocess.check_output([KERNEL_BIN, "-v"], text=True)
        log(f"Kernel Installed: {ver_o.strip()}", "succ")
        
    except Exception as e:
        log(f"Failed to install kernel: {e}", "err")
        sys.exit(1)
    finally:
        log("", "endgroup")

def get_rule_type(path_parts):
    """基于目录结构推断类型"""
    # 路径通常拆分为: [merged-rules, block, domain, filename.txt]
    for part in path_parts:
        p = part.lower()
        if "domain" in p: return "domain"
        if "ip" in p and "cidr" in p: return "ipcidr"
        if "ip" in p: return "ipcidr" # 宽容匹配
    return None

def write_summary(stats, total_time):
    """生成 GitHub Actions Summary 表格"""
    if "GITHUB_STEP_SUMMARY" not in os.environ: return

    markdown = [
        "### 🍭 MRS Conversion Report",
        f"**Status**: ✅ Completed in {total_time:.2f}s",
        "",
        "| Metric | Count |",
        "| :--- | :--- |",
        f"| 🟢 **Success** | {stats['success']} |",
        f"| 🔴 **Failed** | {stats['failed']} |",
        f"| 🟡 **Skipped** | {stats['skipped']} |",
        f"| 📦 **Total Files** | {stats['total']} |",
        ""
    ]
    
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
        f.write("\n".join(markdown))

def main():
    start_time = time.time()
    get_latest_mihomo()

    log(f"Starting Conversion Task: {SRC_ROOT} -> {DST_ROOT}", "group")
    
    if os.path.exists(DST_ROOT): shutil.rmtree(DST_ROOT)
    os.makedirs(DST_ROOT)

    if not os.path.exists(SRC_ROOT):
        log(f"Source dir {SRC_ROOT} not found!", "err")
        sys.exit(1)

    files_map = []
    for root, _, files in os.walk(SRC_ROOT):
        for f in files:
            if f.endswith(".txt"):
                files_map.append(os.path.join(root, f))

    total_files = len(files_map)
    stats = {"success": 0, "failed": 0, "skipped": 0, "total": total_files}
    
    log(f"Found {total_files} text rules to process.")

    for idx, src_path in enumerate(files_map, 1):
        rel_path = os.path.relpath(src_path, SRC_ROOT)
        # 进度条效果
        # logic: [23/100] block/domain/google.txt
        prefix = f"[{idx}/{total_files}]"
        
        # 推断类型
        path_parts = rel_path.split(os.sep)
        rule_type = get_rule_type(path_parts)
        
        dst_rel = os.path.splitext(rel_path)[0] + ".mrs"
        dst_path = os.path.join(DST_ROOT, dst_rel)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)

        if not rule_type:
            print(f"{C.WARNING}{prefix} SKIP: {rel_path} (Unknown Type){C.END}")
            stats["skipped"] += 1
            continue

        # 转换
        cmd = [KERNEL_BIN, "convert-ruleset", rule_type, src_path, dst_path]
        try:
            # 注意：这里去掉了 stdout=subprocess.DEVNULL，以便调试
            p = subprocess.run(cmd, check=True, capture_output=True, text=True)
            
            print(f"{C.GREEN}{prefix} OK: {rel_path} -> MRS{C.END}")
            stats["success"] += 1
            
        except subprocess.CalledProcessError as e:
            # 🔥 关键修改：打印具体的错误信息 (STDERR)
            err_msg = e.stderr.strip() if e.stderr else "Unknown Error"
            print(f"{C.FAIL}{prefix} ERR: {rel_path}")
            print(f"    └── Reason: {err_msg}{C.END}")
            stats["failed"] += 1

    log("", "endgroup")
    
    end_time = time.time()
    duration = end_time - start_time
    
    log(f"🎉 Task Finished. Success: {stats['success']}, Failed: {stats['failed']}", "succ")
    
    # 生成页面摘要
    write_summary(stats, duration)

if __name__ == "__main__":
    main()
