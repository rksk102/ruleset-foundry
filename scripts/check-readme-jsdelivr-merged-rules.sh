#!/usr/bin/env bash
set -euo pipefail

REPO="${GITHUB_REPOSITORY:-}"
if [ -z "$REPO" ]; then
  # 回退从 origin URL 推断 owner/repo
  REPO="$(git remote get-url origin 2>/dev/null | sed -n 's#.*github.com[:/]\([^/]\+/\([^/.]\+\)\)\(.git\)\{0,1\}$#\1#p')"
fi

# 可配置输入（由工作流传入，或用默认值）
REF="${INPUT_REF:-main}"                    # 链接里的 @ref
CHECK_TIMEOUT="${INPUT_CHECK_TIMEOUT:-6}"   # 每个链接超时（秒）
CHECK_RETRIES="${INPUT_CHECK_RETRIES:-0}"   # 每个链接重试次数
PARALLEL="${INPUT_PARALLEL:-8}"             # 并发数
OUTPUT_FILE="${INPUT_OUTPUT_FILE:-README_LINKS_CHECK.md}"

# 根据当前仓库/分支构造“要检查的前缀”
BASE_PREFIX="https://cdn.jsdelivr.net/gh/${REPO}@${REF}/merged-rules/"

updated_at="$(date +'%Y-%m-%d %H:%M:%S %Z')"

if [ ! -f README.md ]; then
  echo "::error::README.md not found."
  exit 1
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# 1) 提取 README 中所有 URL
# 1.1 [text](url)
grep -oE '\[[^]]*\]\((https?://[^)[:space:]]+)\)' README.md \
  | sed -E 's/.*\((https?:\/\/[^)[:space:]]+)\).*/\1/' > "${tmpdir}/u1.txt" || true

# 1.2 <https://...>
grep -oE '<https?://[^>[:space:]]+>' README.md \
  | sed -E 's/[<>]//g' > "${tmpdir}/u2.txt" || true

# 1.3 裸链接
grep -oE 'https?://[A-Za-z0-9._~:/?#\[\]@!$&'"'"'()*+,;=%-]+' README.md > "${tmpdir}/u3.txt" || true

# 2) 合并去重，并仅保留以 BASE_PREFIX 开头的链接
cat "${tmpdir}/u1.txt" "${tmpdir}/u2.txt" "${tmpdir}/u3.txt" 2>/dev/null \
  | sort -u > "${tmpdir}/urls_all.txt"

awk -v base="$BASE_PREFIX" 'index($0, base)==1' "${tmpdir}/urls_all.txt" \
  > "${tmpdir}/urls.txt"

COUNT=$(wc -l < "${tmpdir}/urls.txt" | tr -d '[:space:]')
echo "Total URLs extracted: $(wc -l < "${tmpdir}/urls_all.txt" | tr -d '[:space:]'), filtered by base prefix: ${COUNT}"
echo "Base prefix: ${BASE_PREFIX}"

# HEAD 检查函数（2xx/3xx 视为 ok）
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

# 3) 若没有匹配链接，也要产出一份报告并追加摘要
if [ "$COUNT" -eq 0 ]; then
  {
    echo "# README Links Check Report (Project A - jsDelivr merged-rules)"
    echo
    echo "> 生成时间：${updated_at}"
    echo
    echo "- 仓库：${REPO}"
    echo "- 分支/标签：${REF}"
    echo "- 基础前缀：${BASE_PREFIX}"
    echo
    echo "未在 README 中匹配到以该前缀开头的链接。"
  } > "${OUTPUT_FILE}"

  {
    echo
    echo "## 链接可用性检查（README 内 jsDelivr merged-rules） @ ${updated_at}"
    echo
    echo "- 分支/标签：${REF}"
    echo "- 基础前缀：${BASE_PREFIX}"
    echo
    echo "未匹配到该前缀的链接。"
  } >> README.md

  echo "No URLs matching base prefix. Report written to ${OUTPUT_FILE} and summary appended to README.md"
  exit 0
fi

# 4) 并行检查
# 注意：这些 URL 不含空白字符，直接逐行传给 xargs 即可
cat "${tmpdir}/urls.txt" | xargs -n1 -P "${PARALLEL}" -I{} bash -c '
  url="$1"
  res=$(check_url "$url")
  printf "%s\t%s\n" "$url" "$res"
' _ > "${tmpdir}/results.tsv"

# 5) 输出独立报告文件
{
  echo "# README Links Check Report (Project A - jsDelivr merged-rules)"
  echo
  echo "> 生成时间：${updated_at}"
  echo
  echo "- 仓库：${REPO}"
  echo "- 分支/标签：${REF}"
  echo "- 基础前缀：${BASE_PREFIX}"
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

# 6) 将摘要“追加”到 README 末尾，不影响前面的内容
{
  echo
  echo "## 链接可用性检查（README 内 jsDelivr merged-rules） @ ${updated_at}"
  echo
  echo "- 分支/标签：${REF}"
  echo "- 基础前缀：${BASE_PREFIX}"
  echo "- 完整报告见：./${OUTPUT_FILE}"
  echo
  echo "| 链接 | 状态 | HTTP |"
  echo "| --- | --- | --- |"
  head -n 100 "${tmpdir}/results.tsv" | while IFS=$'\t' read -r url status code; do
    badge=$([ "$status" = "ok" ] && echo "✅" || echo "❌")
    echo "| ${url} | ${badge} | ${code} |"
  done
  total_lines=$(wc -l < "${tmpdir}/results.tsv" | tr -d '[:space:]')
  if [ "$total_lines" -gt 100 ]; then
    echo "| ... | ... | ... |"
  fi
} >> README.md

echo "Report written to ${OUTPUT_FILE} and summary appended to README.md"
