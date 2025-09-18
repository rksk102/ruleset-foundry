#!/usr/bin/env bash
set -euo pipefail

# 可选严格模式：任一源失败就让 Job 失败；默认 false
STRICT="${STRICT:-false}"

SOURCE_DIR="rulesets"
TMP_DIR="${RUNNER_TEMP:-/tmp}/sync-tmp"
mkdir -p "$TMP_DIR"

# 退出/中断时清理所有下载残留与空目录（更彻底）
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

# 类型归一化
canonicalize_type() {
  local t="${1,,}"
  case "$t" in
    domain|domains|domainset) echo "domain" ;;
    ip|ipcidr|ip-cidr|cidr)   echo "ipcidr" ;;
    classical|classic|mix|mixed|general|all) echo "classical" ;;
    *) echo "$t" ;;
  esac
}

is_type_token() {
  local t
  t="$(canonicalize_type "$1")"
  [[ -n "$t" ]]
}

# 输出相对路径（类型/来源/文件名.映射扩展）
map_out_relpath() {
  local type="$1"; local owner="$2"; local fn="$3"
  local ext="${fn##*.}"
  local base="${fn%.*}"
  local mapped="$fn"
  if force_txt_ext "$ext"; then
    mapped="${base}.txt"
  fi
  echo "${type}/${owner}/${mapped}"
}

# 1) 预清洗 sources.urls：去 BOM/CR、去行尾内联注释、去首尾空白（保留 [type: ...] 行）
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

# 2) 解析类型与 URL，生成 pairs.tsv（格式：type<TAB>url）
PAIRS="${TMP_DIR}/pairs.tsv"
: > "$PAIRS"

current_type="domain"

while IFS= read -r line; do
  # 跳过空行/纯注释行
  [[ -z "$line" ]] && continue
  [[ "$line" =~ ^# ]] && continue

  # 段落头：[type: domain]
  if [[ "$line" =~ ^\[type:[[:space:]]*([A-Za-z0-9_-]+)[[:space:]]*\]$ ]]; then
    current_type="$(canonicalize_type "${BASH_REMATCH[1]}")"
    [[ -z "$current_type" ]] && current_type="domain"
    continue
  fi

  # 前缀方式：domain https://...
  if [[ "$line" =~ ^([A-Za-z0-9_-]+)[[:space:]]+(https?://.+)$ ]]; then
    tok="${BASH_REMATCH[1]}"
    url="${BASH_REMATCH[2]}"
    if is_type_token "$tok"; then
      t="$(canonicalize_type "$tok")"
      echo -e "${t}\t${url}" >> "$PAIRS"
      continue
    fi
  fi

  # 显式键值：type=domain https://...
  if [[ "$line" =~ ^type[[:space:]]*[:=][[:space:]]*([A-Za-z0-9_-]+)[[:space:]]+(https?://.+)$ ]]; then
    t="$(canonicalize_type "${BASH_REMATCH[1]}")"
    url="${BASH_REMATCH[2]}"
    echo -e "${t}\t${url}" >> "$PAIRS"
    continue
  fi

  # 仅 URL：使用 current_type
  if [[ "$line" =~ ^https?://.+$ ]]; then
    echo -e "${current_type}\t${line}" >> "$PAIRS"
    continue
  fi

  # 其他内容忽略
done < "$CLEAN"

if [ ! -s "$PAIRS" ]; then
  echo "No usable URLs after parsing. Skip."
  exit 0
fi

# 3) 生成净化器（awk）
SAN_AWK="${TMP_DIR}/sanitize.awk"
cat > "$SAN_AWK" <<'AWK'
BEGIN { first=1 }
{
  # 首行：去 BOM、去 CR、若为 payload: 则删除该行
  if (first) {
    sub(/^\xEF\xBB\xBF/, "")
    sub(/\r$/, "")
    if ($0 ~ /^[[:space:]]*payload:[[:space:]]*$/) { first=0; next }
    first=0
  }
  # 去 CR
  sub(/\r$/, "")

  line = $0

  # 整行注释（去首空格后以 # 或 ! 开头）
  tmp = line
  sub(/^[[:space:]]+/, "", tmp)
  if (tmp ~ /^#/ || tmp ~ /^!/) next

  # 行尾内联注释（空白 + # 之后）
  sub(/[[:space:]]+#.*$/, "", line)

  # YAML 列表项前缀 "- "
  sub(/^[[:space:]]*-[[:space:]]*/, "", line)

  # 去包裹引号
  if (line ~ /^'.*'$/) { line = substr(line, 2, length(line)-2) }
  if (line ~ /^".*"$/) { line = substr(line, 2, length(line)-2) }

  # 去中文逗号后的备注
  sub(/，.*$/, "", line)

  # Trim 首尾空白
  sub(/^[[:space:]]+/, "", line)
  sub(/[[:space:]]+$/, "", line)

  # 规范逗号两侧空格
  gsub(/[[:space:]]*,[[:space:]]*/, ",", line)

  # 跳过空行
  if (line == "") next

  print line
}
AWK

# 4) 辅助函数
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

try_download() {
  local url="$1"; local out="$2"
  local code
  code=$(curl -sL --create-dirs -o "${out}.download" -w "%{http_code}" "$url" || true)
  if [ "$code" -ge 200 ] && [ "$code" -lt 300 ] && [ -s "${out}.download" ]; then
    echo "OK  ($code): $url"
    return 0
  fi
  echo "Warn ($code): $url"

  # 已知纠错：Loyalsoldier/clash-rules 的 /release/ruleset/ -> /release/
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

# 5) 构建期望文件列表（类型/来源/文件名.映射扩展）并清理“孤儿”
EXP="${TMP_DIR}/expected_files.list"
ACT="${TMP_DIR}/actual_files.list"
: > "$EXP"; : > "$ACT"

while IFS=$'\t' read -r type url; do
  owner="$(get_owner_dir "$url")"
  fn="$(basename "$url")"
  rel_out="$(map_out_relpath "$type" "$owner" "$fn")"
  echo "${SOURCE_DIR}/${rel_out}" >> "$EXP"
done < "$PAIRS"

if [ -d "$SOURCE_DIR" ]; then
  find "$SOURCE_DIR" -type f > "$ACT"
fi

sort -u "$ACT" -o "$ACT" || true
sort -u "$EXP" -o "$EXP"

comm -23 "$ACT" "$EXP" | while read -r f; do
  [ -n "$f" ] && echo "Prune: $f" && rm -f "$f" || true
done

# 6) 拉取并净化每个规则文件（写入 类型/来源/ 映射后的文件名）
mkdir -p "$SOURCE_DIR"
fail_count=0

while IFS=$'\t' read -r type url; do
  owner="$(get_owner_dir "$url")"
  fn="$(basename "$url")"
  rel_out="$(map_out_relpath "$type" "$owner" "$fn")"
  out="${SOURCE_DIR}/${rel_out}"

  echo "Fetch [$type] -> ${url}"
  mkdir -p "$(dirname "$out")"
  if ! try_download "$url" "$out"; then
    echo "::warning::Download failed for $url"
    fail_count=$((fail_count+1))
    continue
  fi

  awk -f "$SAN_AWK" "${out}.download" > "$out"
  rm -f "${out}.download"
  echo "Saved: $out"
done < "$PAIRS"

# 7) 清空空目录 + 兜底清理一切残留
cleanup

# 8) 失败汇总 + 严格模式
if [ "$fail_count" -gt 0 ]; then
  echo "::warning::Total failed sources: $fail_count"
  if [ "$STRICT" = "true" ]; then
    echo "STRICT mode enabled. Failing the job."
    exit 1
  fi
fi

# 9) 提交变更（仅在有变更时）
if [[ -z $(git status -s) ]]; then
  echo "No changes."
  exit 0
fi

git config user.name 'GitHub Actions Bot'
git config user.email 'actions@github.com'
git add -A
git commit -m "chore(daily-sync): Update rule sets (typed) for $(date +'%Y-%m-%d')"
git push