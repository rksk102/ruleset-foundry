#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# 配置区域
# ==============================================================================

STRICT="${STRICT:-false}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROCESSOR="${SCRIPT_DIR}/lib/processor.py"
SOURCE_DIR="rulesets"
TMP_DIR="${RUNNER_TEMP:-/tmp}/sync-tmp"
mkdir -p "$TMP_DIR"

# ==============================================================================
# 通用函数
# ==============================================================================

cleanup() {
  rm -rf "$TMP_DIR"
  if [ -d "$SOURCE_DIR" ]; then
    find "$SOURCE_DIR" -type f -name "*.download" -delete 2>/dev/null || true
    find "$SOURCE_DIR" -type d -empty -delete 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

FORCE_TXT_EXTS="list"
force_txt_ext() {
  local ext="${1,,}"
  for t in $FORCE_TXT_EXTS; do
    if [[ "$ext" == "$t" ]]; then return 0; fi
  done
  return 1
}

normalize_policy() {
  local p="${1,,}"
  case "$p" in
    reject|block|deny|ad|ads|adblock|拦截|拒绝|屏蔽|广告) echo "block" ;;
    direct|bypass|no-proxy|直连|直连规则)               echo "direct" ;;
    proxy|proxied|forward|代理|代理规则)               echo "proxy" ;;
    *) echo "" ;;
  esac
}

normalize_type() {
  local t="${1,,}"
  case "$t" in
    domain|domains|domainset) echo "domain" ;;
    ip|ipcidr|ip-cidr|cidr)   echo "ipcidr" ;;
    classical|classic|mix|mixed|general|all) echo "classical" ;;
    *) echo "" ;;
  esac
}

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

get_owner_dir() {
  local url="$1"
  local host
  host=$(echo "$url" | awk -F/ '{print $3}')
  if [ "$host" = "raw.githubusercontent.com" ]; then
    echo "$url" | awk -F/ '{print $4}'
  elif [ "$host" = "cdn.jsdelivr.net" ]; then
    local p4
    p4=$(echo "$url" | awk -F/ '{print $4}' || echo "")
    if [ "$p4" = "gh" ]; then
      echo "$url" | awk -F/ '{print $5}'
    else
      echo "$host"
    fi
  else
    echo "$host"
  fi
}

try_download() {
  local url="$1"; local out="$2"
  local code
  
  code=$(curl -sL --connect-timeout 10 --retry 2 --create-dirs -o "${out}.download" -w "%{http_code}" "$url" || true)
  
  if [ "$code" -ge 200 ] && [ "$code" -lt 300 ] && [ -s "${out}.download" ]; then
    echo "OK  ($code): $url"
    return 0
  fi
  echo "Warn ($code): $url"

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

if [ ! -f "$PROCESSOR" ]; then
  echo "::error::Processor script not found at $PROCESSOR"
  exit 1
fi

if [ ! -f sources.urls ]; then
  echo "sources.urls not found, skip."
  exit 0
fi

CLEAN="${TMP_DIR}/sources.cleaned"
awk 'NR==1{ sub(/^\xEF\xBB\xBF/,"") } { print }' sources.urls \
  | sed 's/\r$//' \
  | sed -E 's/[[:space:]]+#.*$//' \
  | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
  > "$CLEAN"

TRIPLETS="${TMP_DIR}/triplets.tsv"
: > "$TRIPLETS"

current_policy="proxy"
current_type="domain"

while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  [[ "$line" =~ ^# ]] && continue

  if [[ "$line" =~ ^\[policy:[[:space:]]*([^\]]+)[[:space:]]*\]$ ]]; then
    pol_guess="${BASH_REMATCH[1]}"
    current_policy="$(normalize_policy "$pol_guess" || echo "proxy")"
    continue
  fi
  if [[ "$line" =~ ^\[type:[[:space:]]*([^\]]+)[[:space:]]*\]$ ]]; then
    type_guess="${BASH_REMATCH[1]}"
    current_type="$(normalize_type "$type_guess" || echo "domain")"
    continue
  fi

  if [[ "$line" =~ https?:// ]]; then
    url_word="$(awk '{ for (i=1;i<=NF;i++) if ($i ~ /^https?:\/\//) { print $i; exit } }' <<< "$line")"
    prefix="${line%%$url_word*}"
    
    pol="$current_policy"
    typ="$current_type"
    
    IFS=' ' read -r -a toks <<< "$prefix"
    for tk in "${toks[@]}"; do
      [[ -z "$tk" ]] && continue
      if [[ "$tk" =~ ^policy[:=](.+)$ ]]; then
        v="$(normalize_policy "${BASH_REMATCH[1]}")"
        [[ -n "$v" ]] && pol="$v"
        continue
      fi
      if [[ "$tk" =~ ^type[:=](.+)$ ]]; then
        v="$(normalize_type "${BASH_REMATCH[1]}")"
        [[ -n "$v" ]] && typ="$v"
        continue
      fi
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

EXP="${TMP_DIR}/expected_files.list"
ACT="${TMP_DIR}/actual_files.list"
: > "$EXP"

while IFS=$'\t' read -r policy type url; do
  owner="$(get_owner_dir "$url")"
  fn="$(basename "$url")"
  rel_out="$(map_out_relpath "$policy" "$type" "$owner" "$fn")"
  echo "${SOURCE_DIR}/${rel_out}" >> "$EXP"
done < "$TRIPLETS"

if [ -d "$SOURCE_DIR" ]; then
  find "$SOURCE_DIR" -type f > "$ACT"
  sort -u "$ACT" -o "$ACT" || true
  sort -u "$EXP" -o "$EXP"
  comm -23 "$ACT" "$EXP" | while read -r f; do
    [ -n "$f" ] && echo "Prune orphan: $f" && rm -f "$f" || true
  done
fi

mkdir -p "$SOURCE_DIR"
fail_count=0

while IFS=$'\t' read -r policy type url; do
  owner="$(get_owner_dir "$url")"
  fn="$(basename "$url")"
  
  pol_norm="$(normalize_policy "$policy")"; pol="${pol_norm:-proxy}"
  typ_norm="$(normalize_type "$type")";     typ="${typ_norm:-domain}"
  
  rel_out="$(map_out_relpath "$pol" "$typ" "$owner" "$fn")"
  out="${SOURCE_DIR}/${rel_out}"
  dir="$(dirname "$out")"
  mkdir -p "$dir"

  echo ""
  echo "## Target: [${pol}/${typ}] ${fn}"
  echo "Source: ${url}"

  if ! try_download "$url" "$out"; then
    echo "::warning::Download failed: $url"
    fail_count=$((fail_count+1))
    continue
  fi

  proc_mode="domain"
  if [ "$typ" == "ipcidr" ]; then
    proc_mode="ipcidr"
  fi
  
  echo "Processing mode: ${proc_mode}"
  
  if python3 "$PROCESSOR" "$proc_mode" < "${out}.download" > "$out"; then
    line_count=$(wc -l < "$out" | tr -d ' ')
    echo "Success. Saved $line_count lines to ${rel_out}"
    rm -f "${out}.download"
  else
    echo "::error::Processing failed for $url"
    if [ -f "${out}.download" ]; then
       head -n 5 "${out}.download" || true
    fi
    rm -f "${out}.download" "$out"
    fail_count=$((fail_count+1))
  fi

done < "$TRIPLETS"

cleanup

if [ "$fail_count" -gt 0 ]; then
  echo "::warning::Summary: $fail_count sources failed."
  if [ "$STRICT" = "true" ]; then
    echo "STRICT mode on. Failing job."
    exit 1
  fi
fi

# 7. Git 提交
git config user.name 'GitHub Actions Bot'
git config user.email 'actions@github.com'

# 先 add 所有变更
git add -A

# 再次检查是否有实际内容变更 (避免 git commit 报错)
if git diff-index --quiet HEAD; then
  echo "No content changes detected. Skipping commit."
  exit 0
fi

echo "Changes detected. Committing..."
git commit -m "chore(daily-sync): Update rule sets (policy/type/source) for $(date +'%Y-%m-%d')"
git push
