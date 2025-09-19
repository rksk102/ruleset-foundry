#!/usr/bin/env bash
set -euo pipefail

# 可选严格模式：任一源失败就让 Job 失败；默认 false
STRICT="${STRICT:-true}"

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

  # 段落头：[policy: 任意字符直到 ] ]
  if [[ "$line" =~ ^\[policy:[[:space:]]*([^\]]+)[[:space:]]*\]$ ]]; then
    pol_guess="${BASH_REMATCH[1]}"
    pol_norm="$(normalize_policy "$pol_guess")"
    current_policy="${pol_norm:-proxy}"
    continue
  fi
  # 段落头：[type: 任意字符直到 ] ]
  if [[ "$line" =~ ^\[type:[[:space:]]*([^\]]+)[[:space:]]*\]$ ]]; then
    type_guess="${BASH_REMATCH[1]}"
    type_norm="$(normalize_type "$type_guess")"
    current_type="${type_norm:-domain}"
    continue
  fi

  # 如果该行含有 URL，则解析前缀 token（次序任意、可含键值对）
  if [[ "$line" =~ https?:// ]]; then
    # 提取第一个 URL 词（假设 URL 中不含空格）
    url_word="$(awk '{ for (i=1;i<=NF;i++) if ($i ~ /^https?:\/\//) { print $i; exit } }' <<< "$line")"
    # 取 URL 前缀（token 区域）
    prefix="${line%%$url_word*}"

    pol="$current_policy"
    typ="$current_type"

    # 按空格切分前缀，逐一识别 token（policy/type 或键值对）
    # 使用 read -a 保持对中文 token 的处理
    IFS=' ' read -r -a toks <<< "$prefix"
    for tk in "${toks[@]}"; do
      [[ -z "$tk" ]] && continue
      # 键值对：policy=xxx 或 policy:xxx
      if [[ "$tk" =~ ^policy[:=](.+)$ ]]; then
        v="${BASH_REMATCH[1]}"
        v_norm="$(normalize_policy "$v")"
        [[ -n "$v_norm" ]] && pol="$v_norm"
        continue
      fi
      if [[ "$tk" =~ ^type[:=](.+)$ ]]; then
        v="${BASH_REMATCH[1]}"
        v_norm="$(normalize_type "$v")"
        [[ -n "$v_norm" ]] && typ="$v_norm"
        continue
      fi
      # 简写 token：拦截/直连/代理 或 domain/ipcidr/classical
      v_pol="$(normalize_policy "$tk")"
      if [[ -n "$v_pol" ]]; then pol="$v_pol"; continue; fi
      v_typ="$(normalize_type "$tk")"
      if [[ -n "$v_typ" ]]; then typ="$v_typ"; continue; fi
    done

    # 回落默认
    pol="${pol:-$current_policy}"
    typ="${typ:-$current_type}"

    echo -e "${pol}\t${typ}\t${url_word}" >> "$TRIPLETS"
    continue
  fi

  # 其他行忽略
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
  pol_norm="$(normalize_policy "$policy")"; typ_norm="$(normalize_type "$type")"
  pol="${pol_norm:-proxy}"; typ="${typ_norm:-domain}"
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
  pol_norm="$(normalize_policy "$policy")"; typ_norm="$(normalize_type "$type")"
  pol="${pol_norm:-proxy}"; typ="${typ_norm:-domain}"
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
