#!/usr/bin/env bash
set -uo pipefail

# ================= CONFIGURATION =================
STRICT_MODE="${STRICT_MODE:-false}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROCESSOR="${SCRIPT_DIR}/lib/processor.py"
SOURCE_DIR="rulesets"
TEMP_DIR="${RUNNER_TEMP:-/tmp}/sync-engine"
mkdir -p "$TEMP_DIR"

# Icons for console output
ICON_OK="✅"
ICON_FAIL="❌"
ICON_WARN="⚠️"
ICON_WORK="⚙️"

# ================= FUNCTIONS =================

cleanup() {
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

map_filename() {
  # 强制将所有输出映射为 .txt
  local policy="$1"; local type="$2"; local owner="$3"; local url="$4"
  local filename=$(basename "$url")
  local base="${filename%.*}"
  # 路径结构: rulesets/policy/type/owner/filename.txt
  echo "${policy}/${type}/${owner}/${base}.txt"
}

normalize_args() {
  # 归一化输入参数
  local input="${1,,}"
  case "$input" in
    *reject*|*block*|*deny*|*ads*) echo "block" ;;
    *direct*|*bypass*)             echo "direct" ;;
    *proxy*|*gfw*)                 echo "proxy" ;;
    *)                             echo "${input:-proxy}" ;;
  esac
}

normalize_type() {
  local input="${1,,}"
  case "$input" in
    *ip*|*cidr*) echo "ipcidr" ;;
    *)           echo "domain" ;;
  esac
}

get_owner() {
  echo "$1" | awk -F/ '{print $3}' | sed 's/raw.githubusercontent.com/github/'
}

# ================= MAIN EXECUTION =================

echo "::group::🔧 Initialization"
if [ ! -f "$PROCESSOR" ]; then
  echo "::error::Helper script processor.py not found!"
  exit 1
fi

# 预处理 Sources 文件
if [ ! -f sources.urls ]; then
  echo "::warning::sources.urls file missing."
  exit 0
fi

# 清洗 sources.urls (去BOM, 去注释, 去空行)
awk 'NR==1{sub(/^\xEF\xBB\xBF/,"")} {print}' sources.urls \
  | sed 's/\r$//' | sed -E 's/[[:space:]]+#.*$//' \
  | grep -v "^$" > "${TEMP_DIR}/clean_sources.list"
echo "Loaded $(wc -l < "${TEMP_DIR}/clean_sources.list") sources."
echo "::endgroup::"

# --- 准备文件列表 ---
TASKS_FILE="${TEMP_DIR}/tasks.tsv"
: > "$TASKS_FILE"

current_pol="proxy"
current_typ="domain"

while read -r line; do
  # 解析 [Tag]
  if [[ "$line" =~ ^\[policy:(.+)\]$ ]]; then current_pol="$(normalize_args "${BASH_REMATCH[1]}")"; continue; fi
  if [[ "$line" =~ ^\[type:(.+)\]$ ]]; then current_typ="$(normalize_type "${BASH_REMATCH[1]}")"; continue; fi
  
  # 提取 URL
  if [[ "$line" =~ https?:// ]]; then
    url=$(echo "$line" | grep -oE 'https?://[^ ]+')
    echo -e "${current_pol}\t${current_typ}\t${url}" >> "$TASKS_FILE"
  fi
done < "${TEMP_DIR}/clean_sources.list"

# --- 清理孤儿文件 ---
echo "::group::🧹 Cleaning Orphan Files"
EXPECTED_FILES="${TEMP_DIR}/expected.txt"
: > "$EXPECTED_FILES"
while IFS=$'\t' read -r p t u; do
  rel_path=$(map_filename "$p" "$t" "$(get_owner "$u")" "$u")
  echo "${SOURCE_DIR}/${rel_path}" >> "$EXPECTED_FILES"
done < "$TASKS_FILE"

if [ -d "$SOURCE_DIR" ]; then
  find "$SOURCE_DIR" -type f | sort > "${TEMP_DIR}/actual.txt"
  sort "$EXPECTED_FILES" -o "$EXPECTED_FILES"
  comm -23 "${TEMP_DIR}/actual.txt" "$EXPECTED_FILES" | while read -r f; do
    echo "Deleting orphan: $f"
    rm -f "$f"
  done
fi
echo "::endgroup::"

# --- 核心循环 ---
FAIL_COUNT=0

while IFS=$'\t' read -r policy type url; do
  fn=$(basename "$url")
  owner=$(get_owner "$url")
  rel_path=$(map_filename "$policy" "$type" "$owner" "$url")
  abs_path="${SOURCE_DIR}/${rel_path}"
  
  # 这里的 Grouping 让 GitHub 日志非常整洁
  echo "::group::${ICON_WORK} Processing: $fn"
  echo "Target: $rel_path"
  echo "Source: $url"
  
  mkdir -p "$(dirname "$abs_path")"
  
  # 1. 下载
  DOWNLOAD_FILE="${abs_path}.tmp"
  HTTP_CODE=$(curl -sL --connect-timeout 15 --retry 2 -w "%{http_code}" -o "$DOWNLOAD_FILE" "$url")
  
  if [ "$HTTP_CODE" -lt 200 ] || [ "$HTTP_CODE" -ge 300 ]; then
    echo "::error::Download failed with code $HTTP_CODE"
    echo "ERROR_DL: $url" # 供报表提取
    rm -f "$DOWNLOAD_FILE"
    FAIL_COUNT=$((FAIL_COUNT+1))
    echo "::endgroup::"
    
    if [ "$STRICT_MODE" = "true" ]; then
      echo "::error::Strict mode enabled. Stopping workflow."
      exit 1
    fi
    continue
  fi
  
  # 2. 清洗 (Python)
  # 确定模式
  PY_MODE="domain"
  if [ "$type" == "ipcidr" ]; then PY_MODE="ipcidr"; fi
  
  # 调用 Python
  if python3 "$PROCESSOR" "$PY_MODE" < "$DOWNLOAD_FILE" > "$abs_path"; then
    LINE_COUNT=$(wc -l < "$abs_path")
    echo "SUCCESS: Saved $LINE_COUNT lines to $rel_path"
    rm -f "$DOWNLOAD_FILE"
  else
    echo "::error::Content Sanitize Failed!"
    echo "ERROR_PARSE: $url" # 供报表提取
    cat "$DOWNLOAD_FILE" | head -n 5 # 打印前5行帮助除错
    rm -f "$DOWNLOAD_FILE" "$abs_path"
    FAIL_COUNT=$((FAIL_COUNT+1))
    echo "::endgroup::"
    
    if [ "$STRICT_MODE" = "true" ]; then
       exit 1
    fi
    continue
  fi
  
  echo "::endgroup::"
  
done < "$TASKS_FILE"

# --- 结果判定 ---
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "::error::Workflow completed with $FAIL_COUNT errors."
  # 这里虽然我们之前continue了，但根据你的要在遇到错误后停止（即 workflow 失败）
  # 如果前面是 permissive 模式，这里补刀，保证最后状态是红的
  exit 1
fi

# --- Git 提交 ---
echo "::group::💾 Git Commit"
git config user.name 'GitHub Actions Bot'
git config user.email 'actions@github.com'
git add -A
if git diff-index --quiet HEAD; then
  echo "No changes to commit."
else
  echo "Changes detected. Pushing..."
  git commit -m "chore(sync): Auto-sync rules $(date +'%Y-%m-%d')"
  git push
fi
echo "::endgroup::"
