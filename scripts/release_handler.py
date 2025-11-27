#!/usr/bin/env python3
import os
import sys
import shutil
import subprocess
import json
import datetime
import zipfile

# ================= 配置区域 =================
TARGET_DIR = "merged-rules"   # 要打包的文件夹
KEEP_DAYS = 3                 # 保留历史版本天数
# ===========================================

def run_gh(cmd_list):
    """调用 GitHub CLI，简化报错处理"""
    try:
        result = subprocess.run(["gh"] + cmd_list, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        # 某些删除命令报错可能只是因为不存在，不一定是致命错误，打印一下即可
        print(f"⚠️ GH API Note: {e.stderr.strip()}")
        return None

def zip_target_dir(tag_date):
    """将 TARGET_DIR 压缩为 zip"""
    if not os.path.exists(TARGET_DIR):
        print(f"❌ Error: Directory '{TARGET_DIR}' not found. Did you download artifacts?")
        sys.exit(1) # 没有文件就直接报错停止

    zip_name = f"merged-rules-{tag_date}.zip"
    print(f"📦 Packaging {TARGET_DIR} into {zip_name}...")
    
    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(TARGET_DIR):
            for file in files:
                file_path = os.path.join(root, file)
                # 在压缩包内保持相对路径
                arcname = os.path.relpath(file_path, os.path.dirname(TARGET_DIR))
                zipf.write(file_path, arcname)
    return zip_name

def main():
    print("::group::🚀 Processing Release")

    # 1. 计算北京时间 (UTC+8)
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    beijing_now = utc_now + datetime.timedelta(hours=8)
    
    tag_date = beijing_now.strftime("%Y-%m-%d")      # 例如 2023-10-01
    tag_time = beijing_now.strftime("%H:%M:%S")      # 例如 14:30:05
    release_tag = f"rules-{tag_date}"                # Tag 名称

    print(f"📅 Target Release Tag: {release_tag} (Time: {tag_time})")

    # 2. 打包文件
    zip_file = zip_target_dir(tag_date)

    # 3. 强制覆盖当天已有的 Release (防报错)
    # 如果今天已经跑过一次，先删掉旧的，再发新的
    if run_gh(["release", "view", release_tag]):
        print(f"🔄 Release {release_tag} exists. Deleting for update...")
        run_gh(["release", "delete", release_tag, "--yes"])
        # 必须同时删除 git tag ref，否则创建时会报错 "tag already exists"
        run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{release_tag}"])

    # 4. 创建新 Release
    print(f"🚀 Uploading Release {release_tag}...")
    notes = f"""
    自动构建完成。
    
    - **日期**: {tag_date}
    - **时间**: {tag_time} (北京时间)
    - **包含内容**: `merged-rules` 完整规则集
    """
    
    run_gh([
        "release", "create", release_tag, zip_file,
        "--title", f"Merged Rules - {tag_date}",
        "--notes", notes,
        "--latest" # 标记为 Latest Release
    ])

    # 5. 清理旧版本 (保留最近 KEEP_DAYS 天)
    print(f"🧹 Cleaning up releases older than {KEEP_DAYS} days...")
    releases_json = run_gh(["release", "list", "--limit", "50", "--json", "tagName,createdAt"])
    
    if releases_json:
        releases = json.loads(releases_json)
        cutoff_time = utc_now - datetime.timedelta(days=KEEP_DAYS)
        
        for rel in releases:
            # GitHub API 返回的时间是 ISO 8601 格式
            created_at = datetime.datetime.fromisoformat(rel['createdAt'].replace("Z", "+00:00"))
            tag = rel['tagName']
            
            # 如果(比截止时间老) 且 (不是今天刚发的这个)
            if created_at < cutoff_time and tag != release_tag:
                print(f"🗑️ Deleting old release: {tag}")
                run_gh(["release", "delete", tag, "--yes"])
                run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{tag}"])

    print("::endgroup::")
    print("✅ Feature Delivery Completed.")

if __name__ == "__main__":
    main()
