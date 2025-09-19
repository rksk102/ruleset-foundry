#!/usr/bin/env bash
set -euo pipefail

REPO="${GITHUB_REPOSITORY:-}"
if [ -z "$REPO" ]; then
  REPO="$(git remote get-url origin 2>/dev/null | sed -n 's#.*github.com[:/]\([^/]\+/\([^/.]\+\)\)\(.git\)\{0,1\}$#\1#p')"
fi

# 可配置输入（由工作流传入，或使用默认值）
REF="${INPUT_REF:-main}"
CHECK_TIMEOUT="${INPUT_CHECK_TIMEOUT:-6}"      # 每个链接超时（秒）
CHECK_RETRIES="${INPUT_CHECK_RETRIES:-0}"      # 每个链接重试次数
PARALLEL="${INPUT_PARALLEL:-8}"                # 并行协程数
OUTPUT_FILE="${INPUT_OUTPUT_FILE:-README_LINKS_CHECK.md}"

# 过滤：仅检查匹配 INCLUDE_REGEX 的链接，且不匹配 EXCLUDE_REGEX 的链接
# 默认仅检查“merged-rules”目录的 jsDelivr/raw 链接（你可改为 ".*" 检查全部链接）
DEFAULT_INCLUDE_REGEX='(cdn\.jsdelivr\.net/gh/.+@.*/merged-rules/|raw\.githubusercontent\.com/.+/.+/.+/merged-rules/)'
INCLUDE_REGEX="${INPUT_INCLUDE_REGEX:-$DEFAULT_INCLUDE_REGEX}"
EXCLUDE_REGEX="${INPUT_EXCLUDE_REGEX:-}"

updated_at="$(date +'%Y-%m-%d %H:%M:%S %Z')"

if [ ! -f README.md ]; then
  echo "::error::README.md not found."
  exit 1
fi

# 提取 README 中的链接
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# 1) Markdown 形式的链接 [text](url)
grep -oE '\[[^]]*\]\((https?://[^)[:space:]]+)\)' README.md \
  | sed -E 's/.*\((https?:\/\/[^)[:space:]]+)\).*/\1/' > "${tmpdir}/urls1.txt" || true

# 2) 尖括号形式 <https://...>
grep -oE '<https?://[^>[:space:]]+>' README.md \
  | sed -E 's/[<>]//g' > "${tmpdir}/urls2.txt" || true

# 3) 裸链接 https://...（尽量匹配常见字符）
grep -oE 'https?://[A-Za-z0-9._~:/?#\[\]@!$&'"'"'()*+,;=%-]+' README.md > "${tmpdir}/urls3.txt" || true

# 汇总去重
cat "${tmpdir}/urls1.txt" "${tmpdir}/urls2.txt" "${tmpdir}/urls3.txt" 2>/dev/null \
  | sed -E 's/[)]+$//' \
  | sort -u > "${tmpdir}/urls_all.txt"

# 应用包含/排除过滤
awk -v inc="$INCLUDE_REGEX" -v exc="$EXCLUDE_REGEX" '
  BEGIN {
    use_inc = (inc != "")
    use_exc = (exc != "")
  }
  {
    url = $0
    ok = 1
    if (use_inc) {
      if (url !~ inc) ok = 0
    }
    if (ok && use_exc) {
      if (url ~ exc) ok = 0
    }
    if (ok) print url
  }
' "${tmpdir}/urls_all.txt" > "${tmpdir}/urls.txt"

COUNT=$(wc -l < "${tmpdir}/urls.txt" | tr -d '[:space:]')
echo "Total URLs extracted: $(wc -l < "${tmpdir}/urls_all.txt" | tr -d '[:space:]'), after filter: ${COUNT}"

# 检查函数（HEAD，2xx/3xx 视为可用）
check_url() {
  local url="$1"
  local code
  code=$(curl -sS -I -o /dev/null --max-time "${CHECK_TIMEOUT}" --retry "${CHECK_RETRIES}" --retry-delay 1 -L -w '%{http_code}' "$url" || echo "000")
  case "$code" in
    2*|3*) echo -e "ok\t${code}" ;;
    *)     echo -e "fail\t${code}" ;;
  esac
}

export -f check_url
export CHECK_TIMEOUT CHECK_RETRIES

# 如果没有匹配的链接，也生成一份空报告
if [ "$COUNT" -eq 0 ]; then
  {
    echo "# README Links Check Report (Project A)"
    echo
    echo "> 生成时间：${updated_at}"
    echo
    echo "- 分支/标签：${REF}"
    echo "- 过滤规则：INCLUDE='${INCLUDE_REGEX}' EXCLUDE='${EXCLUDE_REGEX}'"
    echo
    echo "未匹配到需要检查的链接。"
  } > "${OUTPUT_FILE}"

  {
    echo
    echo "## 链接可用性检查（README 内链接） @ ${updated_at}"
    echo
    echo "- 分支/标签：${REF}"
    echo "- 过滤规则：INCLUDE='${INCLUDE_REGEX}' EXCLUDE='${EXCLUDE_REGEX}'"
    echo
    echo "未匹配到需要检查的链接。"
  } >> README.md

  echo "Report written to ${OUTPUT_FILE} and appended summary to README.md"
  exit 0
fi

# 并行检查
printf "%s\0" $(cat "${tmpdir}/urls.txt") | xargs -0 -n1 -P "${PARALLEL}" -I{} bash -c '
  url="$1"
  res=$(check_url "$url")
  printf "%s\t%s\n" "$url" "$res"
' _ > "${tmpdir}/results.tsv"

# 生成独立报告文件
{
  echo "# README Links Check Report (Project A)"
  echo
  echo "> 生成时间：${updated_at}"
  echo
  echo "- 分支/标签：${REF}"
  echo "- 过滤规则：INCLUDE='${INCLUDE_REGEX}' EXCLUDE='${EXCLUDE_REGEX}'"
  echo "- 超时/重试：${CHECK_TIMEOUT}s / ${CHECK_RETRIES}"
  echo "- 并行度：${PARALLEL}"
  echo
  echo "| 链接 | 状态 | HTTP |"
  echo "| --- | --- | --- |"
  while IFS=$'\t' read -r url status code; do
    badge=$([ "$status" = "ok" ] && echo "✅" || echo "❌")
    echo "| ${url} | ${badge} | ${code} |"
  done < "${tmpdir}/results.tsv"
} > "${OUTPUT_FILE}"

# 生成摘要并“追加”到 README 末尾（不影响前面的内容）
{
  echo
  echo "## 链接可用性检查（README 内链接） @ ${updated_at}"
  echo
  echo "- 分支/标签：${REF}"
  echo "- 过滤规则：INCLUDE='${INCLUDE_REGEX}' EXCLUDE='${EXCLUDE_REGEX}'"
  echo "- 完整报告见：./${OUTPUT_FILE}"
  echo
  echo "| 链接 | 状态 | HTTP |"
  echo "| --- | --- | --- |"
  # 为避免 README 过长，只展示前 100 条
  head -n 100 "${tmpdir}/results.tsv" | while IFS=$'\t' read -r url status code; do
    badge=$([ "$status" = "ok" ] && echo "✅" || echo "❌")
    echo "| ${url} | ${badge} | ${code} |"
  done
  # 如果总数超出，提示省略
  total_lines=$(wc -l < "${tmpdir}/results.tsv" | tr -d '[:space:]')
  if [ "$total_lines" -gt 100 ]; then
    echo "| ... | ... | ... |"
  fi
} >> README.md

echo "Report written to ${OUTPUT_FILE} and appended summary to README.md"
