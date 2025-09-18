#!/usr/bin/env bash
set -euo pipefail

# 可选严格模式：任一源失败就让 Job 失败；默认 false
STRICT="${STRICT:-false}"

SOURCE_DIR="rulesets"
TMP_DIR="${RUNNER_TEMP:-/tmp}/sync-tmp"
mkdir -p "$TMP_DIR"

# 退出/中断时清理所有下载残留与空目录
cleanup() {
  if [ -d "$SOURCE_DIR" ]; then
    find "$SOURCE_DIR" -type f \( -name "*.download" -o -name "*.source" \) -delete 2>/dev/null || true
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

# 归一化：规则类型（policy：block/direct/proxy）和 type 类型（domain/ipcidr/classical）
normalize_policy() {
  local p="${1,,}"
  case "$p" in
    reject|block|deny|ad|ads|adblock|拦截|拒绝|屏蔽|广告) echo "block" ;;
    direct|bypass|no-proxy|直连|直连规则)               echo "direct" ;;
    proxy|proxied|forward|代理|代理规则)               echo "proxy" ;;
    *) echo "" ;; # 未识别返回空串
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
is_policy_token() { [[ -n "$(normalize_policy "$1")" ]]; }
is_type_token()   { [[ -n "$(normalize_type   "$1")" ]]; }

# 输出相对路径：<policy>/<type>/<owner>/<文件名[映射ext]>
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

# 1) 预清洗 sources.urls：去 BOM/CR、行尾内联注释、首尾空白（保留 [policy:] 和 [type:] 段落头）
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

# 2) 解析：生成 triplets.tsv（policy \t type \t url）
TRIPLETS="${TMP_DIR}/triplets.tsv"
: > "$TRIPLETS"

current_policy="proxy"
current_type="domain"

while IFS= read -r line; do
  # 跳过空行/纯注释
  [[ -z "$line" ]] && continue
  [[ "$line" =~ ^# ]] && continue

  # 段落头：[policy: xxx]
  if [[ "$line" =~ ^\[policy:[[:space:]]*([A-Za-z0-9_\-一-龥]+)[[:space:]]*\]$ ]]; then
    local np
    np="$(normalize_policy "${BASH_REMATCH[1]}")"
    current_policy="${np:-proxy}"
    continue
  fi
  # 段落头：[type: yyy]
  if [[ "$line" =~ ^\[type:[[:space:]]*([A-Za-z0-9_\-一-龥]+)[[:space:]]*\]$ ]]; then
    local nt
    nt="$(normalize_type "${BASH_REMATCH[1]}")"
    current_type="${nt:-domain}"
    continue
  fi

  # 前缀：两个 token + URL（顺序可互换）
  if [[ "$line" =~ ^([A-Za-z0-9_\-一-龥]+)[[:space:]]+([A-Za-z0-9_\-一-龥]+)[[:space:]]+(https?://.+)$ ]]; then
    t1="${BASH_REMATCH[1]}"; t2="${BASH_REMATCH[2]}"; url="${BASH_REMATCH[3]}"
    pol=""; typ=""
    if is_policy_token "$t1"; then pol="$(normalize_policy "$t1")"; fi
    if is_type_token   "$t1"; then typ="$(normalize_type   "$t1")"; fi
    if is_policy_token "$t2" && [[ -z "$pol" ]]; then pol="$(normalize_policy "$t2")"; fi
    if is_type_token   "$t2" && [[ -z "$typ" ]]; then typ="$(normalize_type   "$t2")"; fi
    [[ -z "$pol" ]] && pol="$current_policy"
    [[ -z "$typ" ]] && typ="$current_type"
    echo -e "${pol}\t${typ}\t${url}" >> "$TRIPLETS"
    continue
  fi

  # 前缀：一个 token + URL（另一个维度沿用当前值）
  if [[ "$line" =~ ^([A-Za-z0-9_\-一-龥]+)[[:space:]]+(https?://.+)$ ]]; then
    tok="${BASH_REMATCH[1]}"; url="${BASH_REMATCH[2]}"
    pol="$current_policy"; typ="$current_type"
    if is_policy_token "$tok"; then pol="$(normalize_policy "$tok")"; fi
    if is_type_token   "$tok"; then typ="$(normalize_type   "$tok")"; fi
    echo -e "${pol}\t${typ}\t${url}" >> "$TRIPLETS"
    continue
  fi

  # 键值：policy=.. type=.. URL（可只写一个，另一个沿用当前段落）
  if [[ "$line" =~ ^([^ ]+)[[:space:]]+([^ ]+)[[:space:]]+(https?://.+)$ ]]; then
    kv1="${BASH_REMATCH[1]}"; kv2="${BASH_REMATCH[2]}"; url="${BASH_REMATCH[3]}"
    pol="$current_policy"; typ="$current_type"
    if [[ "$kv1" =~ ^policy[[:space:]]*[:=][[:space:]]*([A-Za-z0-9_\-一-龥]+)$ ]]; then tmp="$(normalize_policy "${BASH_REMATCH[1]}")"; [[ -n "$tmp" ]] && pol="$tmp"; fi
    if [[ "$kv1" =~ ^type[[:space:]]*[:=][[:space:]]*([A-Za-z0-9_\-一-龥]+)$ ]];   then tmp="$(normalize_type   "${BASH_REMATCH[1]}")"; [[ -n "$tmp" ]] && typ="$tmp"; fi
    if [[ "$kv2" =~ ^policy[[:space:]]*[:=][[:space:]]*([A-Za-z0-9_\-一-龥]+)$ ]]; then tmp="$(normalize_policy "${BASH_REMATCH[1]}")"; [[ -n "$tmp" ]] && pol="$tmp"; fi
    if [[ "$kv2" =~ ^type[[:space:]]*[:=][[:space:]]*([A-Za-z0-9_\-一-龥]+)$ ]];   then tmp="$(normalize_type   "${BASH_REMATCH[1]}")"; [[ -n "$tmp" ]] && typ="$tmp"; fi
    echo -e "${pol}\t${typ}\t${url}" >> "$TRIPLETS"
    continue
  fi

  # 仅 URL：使用当前段落默认
  if [[ "$line" =~ ^https?://.+$ ]]; then
    echo -e "${current_policy}\t${current_type}\t${line}" >> "$TRIPLETS"
    continue
  fi
done < "$CLEAN"

if [ ! -s "$TRIPLETS" ]; then
  echo "No usable URLs after parsing. Skip."
  exit 0
fi

# 3) 净化器（awk）
SAN_AWK="${TMP_DIR}/sanitize.awk"
cat > "$SAN_AWK" <<'AWK'
BEGIN { first=1 }
{
  if (first) {
    sub(/^\xEF\xBB\xBF/, "")
    sub(/\r$/, "")
    if ($0 ~ /^[[:space:]]*payload:[[:space:]]*$/) { first=0; next }
    first=0
  }
  sub(/\r$/, "")

  line = $0
  tmp = line
  sub(/^[[:space:]]+/, "", tmp)
  if (tmp ~ /^#/ || tmp ~ /^!/) next

  sub(/[[:space:]]+#.*$/, "", line)
  sub(/^[[:space:]]*-[[:space:]]*/, "", line)

  if (line ~ /^'.*'$/) { line = substr(line, 2, length(line)-2) }
  if (line ~ /^".*"$/) { line = substr(line, 2, length(line)-2) }

  sub(/，.*$/, "", line)

  sub(/^[[:space:]]+/, "", line)
  sub(/[[:space:]]+$/, "", line)

  gsub(/[[:space:]]*,[[:space:]]*/, ",", line)

  if (line == "") next
  print line
}
AWK

# 4) 来源目录名解析
get_owner_dir() {
  local url="$1"
  local host
  host=$(echo "$url" | awk -F/ '{print $3}')
  if [ "$host" = "raw.githubusercontent.com" ]; then
    echo "$url" | awk -F/ '{print $4}'
  elif [ "$host" = "cdn.jsdelivr.net" ]; then
    # https://cdn.jsdelivr.net/gh/<owner>/<repo>@<ref>/...
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

# 5) 下载（含 Loyalsoldier 路径纠错）
try_download() {
  local url="$1"; local out="$2"
  local code
  code=$(curl -sL --create-dirs -o "${out}.download" -w "%{http_code}" "$url" || true)
  if [ "$code" -ge 200 ] && [ "$code" -lt 300 ] && [ -s "${out}.download" ]; then
    echo "OK  ($code): $url"
    return 0
  fi
  echo "Warn ($code): $url"

  if [[ "$url" == https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/ruleset/* ]]; then
    local alt="${url/\/release\/ruleset\//\/release\/}"
    echo "Retry with corrected URL: $alt"
    code=$(curl -sL -o "${out}.download" -w "%{http_code}" "$alt" || true)
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

# 6) 构建期望文件列表并清理“孤儿”
EXP="${TMP_DIR}/expected_files.list"
ACT="${TMP_DIR}/actual_files.list"
: > "$EXP"; : > "$ACT"

while IFS=$'\t' read -r policy type url; do
  owner="$(get_owner_dir "$url")"
  fn="$(basename "$url")"
  # 规范化，确保只有合法值进入目录
  pol="$(normalize_policy "$policy")"; typ="$(normalize_type "$type")"
  pol="${pol:-proxy}"; typ="${typ:-domain}"
  rel_out="$(map_out_relpath "$pol" "$typ" "$owner" "$fn")"
  echo "${SOURCE_DIR}/${rel_out}" >> "$EXP"
done < "$TRIPLETS"

if [ -d "$SOURCE_DIR" ]; then
  find "$SOURCE_DIR" -type f > "$ACT"
fi

sort -u "$ACT" -o "$ACT" || true
sort -u "$EXP" -o "$EXP"

comm -23 "$ACT" "$EXP" | while read -r f; do
  [ -n "$f" ] && echo "Prune: $f" && rm -f "$f" || true
done

# 7) 拉取并净化（写入 <policy>/<type>/<owner>/<文件>）
mkdir -p "$SOURCE_DIR"
fail_count=0

while IFS=$'\t' read -r policy type url; do
  owner="$(get_owner_dir "$url")"
  fn="$(basename "$url")"
  pol="$(normalize_policy "$policy")"; typ="$(normalize_type "$type")"
  pol="${pol:-proxy}"; typ="${typ:-domain}"
  rel_out="$(map_out_relpath "$pol" "$typ" "$owner" "$fn")"
  out="${SOURCE_DIR}/${rel_out}"

  echo "Fetch [${pol}/${typ}] -> ${url}"
  mkdir -p "$(dirname "$out")"
  if ! try_download "$url" "$out"; then
    echo "::warning::Download failed for $url"
    fail_count=$((fail_count+1))
    continue
  fi

  awk -f "$SAN_AWK" "${out}.download" > "$out"
  rm -f "${out}.download"
  echo "Saved: $out"
done < "$TRIPLETS"

# 8) 清空空目录 + 兜底清理一切残留
cleanup

# 9) 失败汇总 + 严格模式
if [ "$fail_count" -gt 0 ]; then
  echo "::warning::Total failed sources: $fail_count"
  if [ "$STRICT" = "true" ]; then
    echo "STRICT mode enabled. Failing the job."
    exit 1
  fi
fi

# 10) 提交变更（仅在有变更时）
if [[ -z $(git status -s) ]]; then
  echo "No changes."
  exit 0
fi

git config user.name 'GitHub Actions Bot'
git config user.email 'actions@github.com'
git add -A
git commit -m "chore(daily-sync): Update rule sets (policy/type/source) for $(date +'%Y-%m-%d')"
git push