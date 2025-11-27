#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# 配置区域
# ==============================================================================

# 可选严格模式：任一源失败就让 Job 失败；默认 false
STRICT="${STRICT:-false}"

# 脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 核心处理器位置 (请确保该文件存在)
PROCESSOR="${SCRIPT_DIR}/lib/processor.py"

SOURCE_DIR="rulesets"
TMP_DIR="${RUNNER_TEMP:-/tmp}/sync-tmp"
mkdir -p "$TMP_DIR"

# ==============================================================================
# 通用函数
# ==============================================================================

# 退出/中断时清理临时文件
cleanup() {
  rm -rf "$TMP_DIR"
  if [ -d "$SOURCE_DIR" ]; then
    # 清理下载中间产物
    find "$SOURCE_DIR" -type f -name "*.download" -delete 2>/dev/null || true
    # 清理空目录
    find "$SOURCE_DIR" -type d -empty -delete 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# 扩展名映射：哪些输入扩展需要强制保存为 .txt
FORCE_TXT_EXTS="list"
force_txt_ext() {
  local ext="${1,,}"
  for t in $FORCE_TXT_EXTS; do
    if [[ "$ext" == "$t" ]]; then return 0; fi
  done
  return 1
}

# 归一化：规则策略（policy）
normalize_policy() {
  local p="${1,,}"
  case "$p" in
    reject|block|deny|ad|ads|adblock|拦截|拒绝|屏蔽|广告) echo "block" ;;
    direct|bypass|no-proxy|直连|直连规则)               echo "direct" ;;
    proxy|proxied|forward|代理|代理规则)               echo "proxy" ;;
    *) echo "" ;;
  esac
}

# 归一化：规则类型（type）
normalize_type() {
  local t="${1,,}"
  case "$t" in
    domain|domains|domainset) echo "domain" ;;
    ip|ipcidr|ip-cidr|cidr)   echo "ipcidr" ;;
    classical|classic|mix|mixed|general|all) echo "classical" ;;
    *) echo "" ;;
  esac
}

# 输出相对路径：<policy>/<type>/<owner>/<文件名>
map_out_relpath() {
  local policy="$1"; local type="$2"; local owner="$3"; local fn="$4"
  local ext="${fn##*.}"
  local base="${fn%.*}"
  local mapped="$fn"
  if force_txt_ext "$ext"; then
    mapped="${base}.txt"
  fi
  echo "${policy}/${type}/${owner}/${mapped}"
}

# 来源目录名解析 (从 Github/CDN URL 提取 Owner)
get_owner_dir() {
  local url="$1"
  local host
  host=$(echo "$url" | awk -F/ '{print $3}')
  if [ "$host" = "raw.githubusercontent.com" ]; then
    echo "$url" | awk -F/ '{print $4}'
  elif [ "$host" = "cdn.jsdelivr.net" ]; then
    local p4
    p4=$(echo "$url" | awk -F/ '{print $4}')
    if [ "$p4" = "gh" ]; then
      echo "$url" | awk -F/ '{print $5}'
    else
      echo "$host"
    fi
  else
    echo "$host"
  fi
}

# 下载函数（含 Loyalsoldier 专用路径纠错与 HTTP 状态码检查）
try_download() {
  local url="$1"; local out="$2"
  local code
  
  # 第一次尝试
  code=$(curl -sL --connect-timeout 10 --retry 2 --create-dirs -o "${out}.download" -w "%{http_code}" "$url" || true)
  
  if [ "$code" -ge 200 ] && [ "$code" -lt 300 ] && [ -s "${out}.download" ]; then
    echo "OK  ($code): $url"
    return 0
  fi
  echo "Warn ($code): $url"

  # 特殊处理: Loyalsoldier url 修正 (release/ruleset -> release)
  if [[ "$url" == https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/ruleset/* ]]; then
    local alt="${url/\/release\/ruleset\//\/release\/}"
    echo "Retry with corrected URL: $alt"
    code=$(curl -sL --connect-timeout 10 --retry 2 -o "${out}.download" -w "%{http_code}" "$alt" || true)
    if [ "$code" -ge 200 ] && [ "$code" -lt 300 ] && [ -s "${out}.download" ]; then
      echo "OK  ($code): $alt"
      return 0
    else
      echo "Fail($code): $alt"
    fi
  fi

  rm -f "${out}.download"
  return 1
}

# ==============================================================================
# 主逻辑
# ==============================================================================

# 0. 检查处理器是否存在
if [ ! -f "$PROCESSOR" ]; then
  echo "::error::Processor script not found at $PROCESSOR"
  exit 1
fi

# 1. 预清洗 sources.urls
if [ ! -f sources.urls ]; then
  echo "sources.urls not found, skip."
  exit 0
fi

CLEAN="${TMP_DIR}/sources.cleaned"
# 去掉 BOM，去掉行内注释，去掉首尾空白
awk 'NR==1{ sub(/^\xEF\xBB\xBF/,"") } { print }' sources.urls \
  | sed 's/\r$//' \
  | sed -E 's/[[:space:]]+#.*$//' \
  | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
  > "$CLEAN"

# 2. 解析 sources 生成任务清单 triplets.tsv (Policy | Type | URL)
TRIPLETS="${TMP_DIR}/triplets.tsv"
: > "$TRIPLETS"

current_policy="proxy"
current_type="domain"

while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  [[ "$line" =~ ^# ]] && continue

  # [policy: ...]
  if [[ "$line" =~ ^\[policy:[[:space:]]*([^\]]+)[[:space:]]*\]$ ]]; then
    pol_guess="${BASH_REMATCH[1]}"
    current_policy="$(normalize_policy "$pol_guess" || echo "proxy")"
    continue
  fi
  # [type: ...]
  if [[ "$line" =~ ^\[type:[[:space:]]*([^\]]+)[[:space:]]*\]$ ]]; then
    type_guess="${BASH_REMATCH[1]}"
    current_type="$(normalize_type "$type_guess" || echo "domain")"
    continue
  fi

  # 处理 URL 行 (含行内前缀 override)
  if [[ "$line" =~ https?:// ]]; then
    url_word="$(awk '{ for (i=1;i<=NF;i++) if ($i ~ /^https?:\/\//) { print $i; exit } }' <<< "$line")"
    prefix="${line%%$url_word*}"
    
    pol="$current_policy"
    typ="$current_type"
    
    # 解析行内前缀
    IFS=' ' read -r -a toks <<< "$prefix"
    for tk in "${toks[@]}"; do
      [[ -z "$tk" ]] && continue
      # policy=xxx
      if [[ "$tk" =~ ^policy[:=](.+)$ ]]; then
        v="$(normalize_policy "${BASH_REMATCH[1]}")"
        [[ -n "$v" ]] && pol="$v"
        continue
      fi
      # type=xxx
      if [[ "$tk" =~ ^type[:=](.+)$ ]]; then
        v="$(normalize_type "${BASH_REMATCH[1]}")"
        [[ -n "$v" ]] && typ="$v"
        continue
      fi
      # 简写 direct / domain 等
      v_pol="$(normalize_policy "$tk")"
      if [[ -n "$v_pol" ]]; then pol="$v_pol"; continue; fi
      v_typ="$(normalize_type "$tk")"
      if [[ -n "$v_typ" ]]; then typ="$v_typ"; continue; fi
    done
    
    pol="${pol:-proxy}"
    typ="${typ:-domain}"
    
    echo -e "${pol}\t${typ}\t${url_word}" >> "$TRIPLETS"
    continue
  fi
done < "$CLEAN"

if [ ! -s "$TRIPLETS" ]; then
  echo "No usable URLs found. Exiting."
  exit 0
fi

# 3. 构建“预期文件列表” (用于删除孤儿文件)
EXP="${TMP_DIR}/expected_files.list"
ACT="${TMP_DIR}/actual_files.list"
: > "$EXP"

while IFS=$'\t' read -r policy type url; do
  owner="$(get_owner_dir "$url")"
  fn="$(basename "$url")"
  rel_out="$(map_out_relpath "$policy" "$type" "$owner" "$fn")"
  echo "${SOURCE_DIR}/${rel_out}" >> "$EXP"
done < "$TRIPLETS"

# 清理旧文件 (在下载前清理，避免路径冲突，或者也可以在下载后清理)
if [ -d "$SOURCE_DIR" ]; then
  find "$SOURCE_DIR" -type f > "$ACT"
  sort -u "$ACT" -o "$ACT" || true
  sort -u "$EXP" -o "$EXP"
  comm -23 "$ACT" "$EXP" | while read -r f; do
    [ -n "$f" ] && echo "Prune orphan: $f" && rm -f "$f" || true
  done
fi

# 4. 循环：下载 -> 清洗 -> 保存
mkdir -p "$SOURCE_DIR"
fail_count=0

while IFS=$'\t' read -r policy type url; do
  owner="$(get_owner_dir "$url")"
  fn="$(basename "$url")"
  
  # 再次归一化确保安全
  pol_norm="$(normalize_policy "$policy")"; pol="${pol_norm:-proxy}"
  typ_norm="$(normalize_type "$type")";     typ="${typ_norm:-domain}"
  
  rel_out="$(map_out_relpath "$pol" "$typ" "$owner" "$fn")"
  out="${SOURCE_DIR}/${rel_out}"
  dir="$(dirname "$out")"
  mkdir -p "$dir"

  echo "---------------------------------------------------"
  echo "Target: [${pol}/${typ}] ${fn}"
  echo "Source: ${url}"

  # 4.1 下载
  if ! try_download "$url" "$out"; then
    echo "::warning::Download failed: $url"
    fail_count=$((fail_count+1))
    continue
  fi

  # 4.2 智能清洗 (调用 processor.py)
  # 确定 Python 脚本的处理模式
  proc_mode="domain"
  if [ "$typ" == "ipcidr" ]; then
    proc_mode="ipcidr"
  fi
  
  echo "Processing mode: ${proc_mode}"
  
  # 调用 Python: 输入重定向自下载文件，输出重定向到最终目标
  if python3 "$PROCESSOR" "$proc_mode" < "${out}.download" > "$out"; then
    line_count=$(wc -l < "$out" | tr -d ' ')
    echo "Success. Saved $line_count lines to ${rel_out}"
    rm -f "${out}.download"
  else
    echo "::error::Processing failed for $url"
    cat "${out}.download" | head -n 5 || true
    rm -f "${out}.download" "$out"
    fail_count=$((fail_count+1))
  fi

done < "$TRIPLETS"

# 5. 最终清理
cleanup

# 6. 严格模式检查
if [ "$fail_count" -gt 0 ]; then
  echo "::warning::Summary: $fail_count sources failed."
  if [ "$STRICT" = "true" ]; then
    echo "STRICT mode on. Failing job."
    exit 1
  fi
fi

# 7. Git 提交
if [[ -z $(git status -s) ]]; then
  echo "No changes detected."
  exit 0
fi

echo "Changes detected. Committing..."
git config user.name 'GitHub Actions Bot'
git config user.email 'actions@github.com'
git add -A
git commit -m "chore(daily-sync): Update rule sets (policy/type/source) for $(date +'%Y-%m-%d')"
git push
