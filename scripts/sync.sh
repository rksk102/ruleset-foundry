#!/usr/bin/env bash
set -euo pipefail

# 可选严格模式：任一源失败就让 Job 失败；默认 false
STRICT="${STRICT:-false}"

SOURCE_DIR="rulesets"
TMP_DIR="${RUNNER_TEMP:-/tmp}/sync-tmp"
mkdir -p "$TMP_DIR"

# 1) 预清洗 sources.urls：去 BOM/CR、去行尾内联注释、去首尾空白、过滤空行与整行注释
if [ ! -f sources.urls ]; then
  echo "sources.urls not found, skip."
  exit 0
fi

URLS="${TMP_DIR}/urls.cleaned"
awk 'NR==1{ sub(/^\xEF\xBB\xBF/,"") } { print }' sources.urls \
| sed 's/\r$//' \
| sed -E 's/[[:space:]]+#.*$//' \
| sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
| grep -v -E '^#|^$' \
> "$URLS"

if [ ! -s "$URLS" ]; then
  echo "No usable URLs after cleaning. Skip."
  exit 0
fi

# 2) 生成净化器（awk），避免 YAML 引号问题
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

# 3) 辅助函数
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
  # 第一次下载
  code=$(curl -sL --create-dirs -o "${out}.download" -w "%{http_code}" "$url" || true)
  if [ "$code" -ge 200 ] && [ "$code" -lt 300 ]; then
    echo "OK  ($code): $url"
    echo "$url" > "${out}.source"
    return 0
  fi
  echo "Warn ($code): $url"

  # 已知纠错：Loyalsoldier/clash-rules 的 /release/ruleset/ -> /release/
  if [[ "$url" == https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/ruleset/* ]]; then
    local alt="${url/\/release\/ruleset\//\/release\/}"
    echo "Retry with corrected URL: $alt"
    code=$(curl -sL -o "${out}.download" -w "%{http_code}" "$alt" || true)
    if [ "$code" -ge 200 ] && [ "$code" -lt 300 ]; then
      echo "OK  ($code): $alt"
      echo "$alt" > "${out}.source"
      return 0
    else
      echo "Fail($code): $alt"
    fi
  fi

  rm -f "${out}.download"
  return 1
}

# 4) 构建期望文件列表并清理“孤儿”文件
EXP="${TMP_DIR}/expected_files.list"
ACT="${TMP_DIR}/actual_files.list"
: > "$EXP"; : > "$ACT"

while IFS= read -r url; do
  owner="$(get_owner_dir "$url")"
  fn="$(basename "$url")"
  echo "${SOURCE_DIR}/${owner}/${fn}" >> "$EXP"
done < "$URLS"

if [ -d "$SOURCE_DIR" ]; then
  find "$SOURCE_DIR" -type f > "$ACT"
fi

sort -u "$ACT" -o "$ACT" || true
sort -u "$EXP" -o "$EXP"

# 删除“孤儿”文件
comm -23 "$ACT" "$EXP" | while read -r f; do
  [ -n "$f" ] && echo "Prune: $f" && rm -f "$f" || true
done

# 5) 拉取并净化每个规则文件
mkdir -p "$SOURCE_DIR"
fail_count=0

while IFS= read -r url; do
  owner="$(get_owner_dir "$url")"
  fn="$(basename "$url")"
  out="${SOURCE_DIR}/${owner}/${fn}"

  echo "Fetch -> ${url}"
  if ! try_download "$url" "$out"; then
    echo "::warning::Download failed for $url"
    fail_count=$((fail_count+1))
    continue
  fi

  # 使用 awk 净化器（适配 mihomo-core）
  awk -f "$SAN_AWK" "${out}.download" > "$out"
  rm -f "${out}.download"
  echo "Saved: $out"
done < "$URLS"

# 6) 清空空目录
[ -d "$SOURCE_DIR" ] && find "$SOURCE_DIR" -type d -empty -delete || true

# 7) 失败汇总 + 严格模式
if [ "$fail_count" -gt 0 ]; then
  echo "::warning::Total failed sources: $fail_count"
  if [ "$STRICT" = "true" ]; then
    echo "STRICT mode enabled. Failing the job."
    exit 1
  fi
fi

# 8) 提交变更（仅在有变更时）
if [[ -z $(git status -s) ]]; then
  echo "No changes."
  exit 0
fi

git config user.name 'GitHub Actions Bot'
git config user.email 'actions@github.com'
git add -A
git commit -m "chore(daily-sync): Update rule sets for $(date +'%Y-%m-%d')"
git push
