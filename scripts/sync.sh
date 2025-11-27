#!/usr/bin/env bash
set -uo pipefail

# ================= CONFIGURATION =================
STRICT_MODE="${STRICT_MODE:-false}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROCESSOR="${SCRIPT_DIR}/lib/processor.py"
SOURCE_DIR="rulesets"
TEMP_DIR="${RUNNER_TEMP:-/tmp}/sync-engine"
mkdir -p "$TEMP_DIR"

# Icons
ICON_OK="✅"
ICON_FAIL="❌"
ICON_WARN="⚠️"
ICON_WORK="⚙️"

# ================= FUNCTIONS =================

cleanup() {
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

# 【核心修正】精准提取来源作者 (Owner)
get_owner() {
  local url="$1"
  # 提取域名 (第3段)
  local domain=$(echo "$url" | awk -F/ '{print $3}')

  if [[ "$domain" == *"github"* ]]; then
    # 匹配 github.com 和 raw.githubusercontent.com
    # 格式通常是: https://site/USER/REPO/...
    # 第4段即为用户名
    echo "$url" | awk -F/ '{print $4}'
    
  elif [[ "$domain" == "cdn.jsdelivr.net" ]]; then
    # 格式: https://cdn.jsdelivr.net/gh/USER/REPO...
    local type_seg=$(echo "$url" | awk -F/ '{print $4}')
    if [ "$type_seg" == "gh" ]; then
        echo "$url" | awk -F/ '{print $5}'
    else
        echo "jsdelivr"
    fi
  else
    # 普通域名直接作为作者名 (如 dl.google.com)
    echo "$domain"
  fi
}

normalize_args() {
  local input="${1,,}"
  case "$input" in
    *reject*|*block*|*deny*|*ads*|*adblock*) echo "block" ;;
    *direct*|*bypass*|*no-proxy*)           echo "direct" ;;
    *proxy*|*gfw*)                          echo "proxy" ;;
    *)                                      echo "${input:-proxy}" ;;
  esac
}

normalize_type() {
  local input="${1,,}"
  case "$input" in
    *ip*|*cidr*) echo "ipcidr" ;;
    *)           echo "domain" ;;
  esac
}

# 【路径生成】确保层级: 策略/类型/作者/文件.txt
map_filename() {
  local policy="$1"
  local type="$2"
  local owner="$3"
  local url="$4"
  
  local filename=$(basename "$url")
  # 强制去除所有后缀，统一加 .txt
  local base="${filename%.*}"
  
  # 最终路径结构
  echo "${policy}/${type}/${owner}/${base}.txt"
}

# ================= MAIN EXECUTION =================

echo "::group::🔧 Initialization"
if [ ! -f "$PROCESSOR" ]; then
  echo "::error::Helper script processor.py not found!"
  exit 1
fi

if [ ! -f sources.urls ]; then
  echo "::warning::sources.urls file missing."
  exit 0
fi

# 预处理源列表
awk 'NR==1{sub(/^\xEF\xBB\xBF/,"")} {print}' sources.urls \
  | sed 's/\r$//' | sed -E 's/[[:space:]]+#.*$//' \
  | grep -v "^$" > "${TEMP_DIR}/clean_sources.list"
echo "Loaded $(wc -l < "${TEMP_DIR}/clean_sources.list") sources."
echo "::endgroup::"

# --- 1. 生成任务列表 ---
TASKS_FILE="${TEMP_DIR}/tasks.tsv"
: > "$TASKS_FILE"

current_pol="proxy"
current_typ="domain"

while read -r line; do
  if [[ "$line" =~ ^\[policy:(.+)\]$ ]]; then current_pol="$(normalize_args "${BASH_REMATCH[1]}")"; continue; fi
  if [[ "$line" =~ ^\[type:(.+)\]$ ]]; then current_typ="$(normalize_type "${BASH_REMATCH[1]}")"; continue; fi
  
  if [[ "$line" =~ https?:// ]]; then
    url=$(echo "$line" | grep -oE 'https?://[^ ]+')
    echo -e "${current_pol}\t${current_typ}\t${url}" >> "$TASKS_FILE"
  fi
done < "${TEMP_DIR}/clean_sources.list"

# --- 2. 孤儿文件清理 (Orphan Cleaning) ---
echo "::group::🧹 Cleaning Orphan Files"
EXPECTED_FILES="${TEMP_DIR}/expected.txt"
: > "$EXPECTED_FILES"

# 计算预期存在的文件路径
while IFS=$'\t' read -r p t u; do
  owner=$(get_owner "$u")
  rel_path=$(map_filename "$p" "$t" "$owner" "$u")
  echo "${SOURCE_DIR}/${rel_path}" >> "$EXPECTED_FILES"
done < "$TASKS_FILE"

if [ -d "$SOURCE_DIR" ]; then
  # 找出实际存在的文件
  find "$SOURCE_DIR" -type f | sort > "${TEMP_DIR}/actual.txt"
  sort "$EXPECTED_FILES" -o "$EXPECTED_FILES"
  
  # 对比删除
  comm -23 "${TEMP_DIR}/actual.txt" "$EXPECTED_FILES" | while read -r f; do
    echo "Deleting orphan: $f"
    rm -f "$f"
  done
  
  # 清理空目录
  find "$SOURCE_DIR" -type d -empty -delete 2>/dev/null || true
fi
echo "::endgroup::"

# --- 3. 下载与处理循环 ---
FAIL_COUNT=0

while IFS=$'\t' read -r policy type url; do
  fn=$(basename "$url")
  owner=$(get_owner "$url") # 提取作者名
  
  # 归一化参数
  pol=$(normalize_args "$policy")
  typ=$(normalize_type "$type")
  
  rel_path=$(map_filename "$pol" "$typ" "$owner" "$url")
  abs_path="${SOURCE_DIR}/${rel_path}"
  
  echo "::group::${ICON_WORK} [${pol}/${typ}] ${owner}/${fn}"
  echo "Source: $url"
  echo "Target: rulesets/$rel_path"
  
  mkdir -p "$(dirname "$abs_path")"
  
  # 下载
  DL_FILE="${abs_path}.tmp"
  HTTP_CODE=$(curl -sL --connect-timeout 15 --retry 2 -w "%{http_code}" -o "$DL_FILE" "$url")
  
  if [ "$HTTP_CODE" -lt 200 ] || [ "$HTTP_CODE" -ge 300 ]; then
    echo "::error::Download failed ($HTTP_CODE)"
    echo "ERROR_DL: $url"
    rm -f "$DL_FILE"
    FAIL_COUNT=$((FAIL_COUNT+1))
    echo "::endgroup::"
    [ "$STRICT_MODE" = "true" ] && exit 1
    continue
  fi
  
  # 清洗 (调用 processor.py)
  PY_MODE="domain"
  [ "$typ" == "ipcidr" ] && PY_MODE="ipcidr"
  
  if python3 "$PROCESSOR" "$PY_MODE" < "$DL_FILE" > "$abs_path"; then
    LINES=$(wc -l < "$abs_path")
    echo "SUCCESS: Saved $LINES lines."
    rm -f "$DL_FILE"
  else
    echo "::error::Sanitize failed!"
    echo "ERROR_PARSE: $url"
    rm -f "$DL_FILE" "$abs_path"
    FAIL_COUNT=$((FAIL_COUNT+1))
    echo "::endgroup::"
    [ "$STRICT_MODE" = "true" ] && exit 1
    continue
  fi
  
  echo "::endgroup::"

done < "$TASKS_FILE"

# --- 4. 结果汇报 ---
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "::error::Completed with $FAIL_COUNT errors."
  exit 1
fi

# --- 5. Git 提交 ---
echo "::group::💾 Git Commit"
git config user.name 'GitHub Actions Bot'
git config user.email 'actions@github.com'
git add -A
if git diff-index --quiet HEAD; then
  echo "No changes."
else
  echo "Pushing changes..."
  git commit -m "chore(sync): Rules update $(date +'%Y-%m-%d')"
  git push
fi
echo "::endgroup::"
