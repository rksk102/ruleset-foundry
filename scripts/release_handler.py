import os
import sys
import shutil
import subprocess
import json
import datetime
import zipfile

TARGET_CONFIG = {
    "merged-rules": ".txt",
    "merged-rules-mrs": ".mrs"
}
KEEP_DAYS = 3

def run_gh(cmd_list):
    """调用 GitHub CLI，简化报错处理"""
    try:
        result = subprocess.run(["gh"] + cmd_list, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"⚠️ GH API Note: {e.stderr.strip()}")
        return None

def zip_target_files(tag_date):
    """根据 TARGET_CONFIG 将指定文件压缩为 zip"""
    zip_name = f"merged-rules-{tag_date}.zip"
    print(f"📦 Packaging files into {zip_name}...")
    
    has_files = False
    
    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for folder, ext in TARGET_CONFIG.items():
            if not os.path.exists(folder):
                print(f"⚠️ Warning: Directory '{folder}' not found in repo root. Skipping.")
                continue

            print(f"   -> Scanning '{folder}' for *{ext} files...")
            for root, _, files in os.walk(folder):
                for file in files:
                    if file.endswith(ext):
                        file_path = os.path.join(root, file)
                        arcname = os.path.join(folder, file) 
                        zipf.write(file_path, arcname)
                        has_files = True
    
    if not has_files:
        print("❌ Error: No matching files found to pack! Please check if folders exist.")
        sys.exit(1)
        
    return zip_name

def main():
    print("::group::🚀 Processing Release")

    utc_now = datetime.datetime.now(datetime.timezone.utc)
    beijing_now = utc_now + datetime.timedelta(hours=8)
    tag_date = beijing_now.strftime("%Y-%m-%d")
    tag_time = beijing_now.strftime("%H:%M:%S")
    release_tag = f"rules-{tag_date}"

    print(f"📅 Target Release Tag: {release_tag} (Time: {tag_time})")

    zip_file = zip_target_files(tag_date)

    if run_gh(["release", "view", release_tag]):
        print(f"🔄 Release {release_tag} exists. Deleting for update...")
        run_gh(["release", "delete", release_tag, "--yes"])
        run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{release_tag}"])

    print(f"🚀 Uploading Release {release_tag}...")
    notes = f"""
    自动发布完成。
    
    - **日期**: {tag_date}
    - **时间**: {tag_time} (北京时间)
    - **包含内容**: 
      - `merged-rules/*.txt`
      - `merged-rules-mrs/*.mrs`
    """
    
    run_gh([
        "release", "create", release_tag, zip_file,
        "--title", f"Merged Rules - {tag_date}",
        "--notes", notes,
        "--latest"
    ])

    print(f"🧹 Cleaning up releases older than {KEEP_DAYS} days...")
    releases_json = run_gh(["release", "list", "--limit", "50", "--json", "tagName,createdAt"])
    
    if releases_json:
        releases = json.loads(releases_json)
        cutoff_time = utc_now - datetime.timedelta(days=KEEP_DAYS)
        
        for rel in releases:
            created_at = datetime.datetime.fromisoformat(rel['createdAt'].replace("Z", "+00:00"))
            tag = rel['tagName']
            
            if created_at < cutoff_time and tag != release_tag:
                print(f"🗑️ Deleting old release: {tag}")
                run_gh(["release", "delete", tag, "--yes"])
                run_gh(["api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/git/refs/tags/{tag}"])

    print("::endgroup::")
    print("✅ Feature Delivery Completed.")

if __name__ == "__main__":
    main()
