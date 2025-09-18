#!/usr/bin/env bash
set -euo pipefail

# 配置（可由工作流 inputs 覆盖）
TZ="${TZ:-Asia/Shanghai}"
INPUT_REF="${INPUT_REF:-}"          # 例如 rules-2025-09-18；为空则默认 main
INPUT_CDN="${INPUT_CDN:-jsdelivr}"  # jsdelivr 或 raw

REPO="${GITHUB_REPOSITORY:-}"
if [ -z "$REPO" ]; then
  # 从 git remote 推断
  origin_url="$(git remote get-url origin 2>/dev/null || echo "")"
  # 支持 https://github.com/owner/repo.git 或 git@github.com:owner/repo.git
  REPO="$(echo "$origin_url" \
    | sed -E 's#^git@github.com:([^/]+)/([^/]+)(\.git)?$#\1/\2#' \
    | sed -E 's#^https?://github.com/([^/]+)/([^/]+)(\.git)?$#\1/\2#')"
  REPO="${REPO:-owner/repo}"
fi

REF="${INPUT_REF:-main}"
CDN="${INPUT_CDN:-jsdelivr}"

build_url() {
  local path="$1"
  case "$CDN" in
    jsdelivr) echo "https://cdn.jsdelivr.net/gh/${REPO}@${REF}/${path}" ;;
    raw)      echo "https://raw.githubusercontent.com/${REPO}/${REF}/${path}" ;;
    *)        echo "https://cdn.jsdelivr.net/gh/${REPO}@${REF}/${path}" ;;
  esac
}

now_date="$(TZ="$TZ" date +'%Y-%m-%d')"
now_time="$(TZ="$TZ" date +'%Y-%m-%d %H:%M:%S %Z')"

TMP_README="$(mktemp)"
{
  echo "# Rule Sets Index"
  echo
  echo "- Build date: ${now_date}"
  echo "- Build time: ${now_time}"
  echo "- Repo: ${REPO}"
  echo "- Ref: ${REF}"
  echo "- CDN: ${CDN}"
  echo
  echo "说明：下表列出了每个规则文件的拉取直链，可直接用于 mihomo 的 rule-providers。目录结构为 rulesets/<policy>/<type>/<owner>/<file>。"
  echo

  if [ -d rulesets ] && find rulesets -type f -print -quit | grep -q . ; then
    echo "## Text Rule Sets (rulesets/)"
    echo
    echo "| Policy | Type | Owner | File | URL |"
    echo "|---|---|---|---|---|"
    while IFS= read -r -d '' f; do
      rel="${f#rulesets/}"
      policy="$(echo "$rel" | cut -d/ -f1)"
      rtype="$(echo "$rel" | cut -d/ -f2)"
      owner="$(echo "$rel" | cut -d/ -f3)"
      file="$(basename "$f")"
      url="$(build_url "rulesets/${policy}/${rtype}/${owner}/${file}")"
      printf "| %s | %s | %s | %s | %s |\n" "$policy" "$rtype" "$owner" "$file" "$url"
    done < <(find rulesets -type f -print0 | sort -z)
    echo
    echo '示例（mihomo rule-providers）：'
    echo '```yaml'
    echo '# 选取表格中的某个 URL 替换 <URL>'
    echo 'rule-providers:'
    echo '  Example-Domain:'
    echo '    type: http'
    echo '    behavior: domain      # 对应 type=domain'
    echo '    format: text'
    echo '    url: <URL>'
    echo '    interval: 86400'
    echo ''
    echo '  Example-IPCidr:'
    echo '    type: http'
    echo '    behavior: ipcidr      # 对应 type=ipcidr'
    echo '    format: text'
    echo '    url: <URL>'
    echo '    interval: 86400'
    echo ''
    echo '  Example-Classical:'
    echo '    type: http'
    echo '    behavior: classical   # 对应 type=classical'
    echo '    format: text'
    echo '    url: <URL>'
    echo '    interval: 86400'
    echo '```'
    echo
  else
    echo "## Text Rule Sets (rulesets/)"
    echo
    echo "_No files found under rulesets/_"
    echo
  fi

  if [ -d mrs-rules ] && find mrs-rules -type f -name '*.mrs' -print -quit | grep -q . ; then
    echo "## MRS Rule Sets (mrs-rules/)"
    echo
    echo "| Policy | Type | Owner | File | URL |"
    echo "|---|---|---|---|---|"
    while IFS= read -r -d '' f; do
      rel="${f#mrs-rules/}"
      policy="$(echo "$rel" | cut -d/ -f1)"
      rtype="$(echo "$rel" | cut -d/ -f2)"
      owner="$(echo "$rel" | cut -d/ -f3)"
      file="$(basename "$f")"
      url="$(build_url "mrs-rules/${policy}/${rtype}/${owner}/${file}")"
      printf "| %s | %s | %s | %s | %s |\n" "$policy" "$rtype" "$owner" "$file" "$url"
    done < <(find mrs-rules -type f -name '*.mrs' -print0 | sort -z)
    echo
    echo '示例（mihomo rule-providers，MRS 二进制）：'
    echo '```yaml'
    echo 'rule-providers:'
    echo '  Example-MRS-Domain:'
    echo '    type: http'
    echo '    behavior: domain'
    echo '    format: mrs'
    echo '    url: <URL>'
    echo '    interval: 86400'
    echo ''
    echo '  Example-MRS-IPCidr:'
    echo '    type: http'
    echo '    behavior: ipcidr'
    echo '    format: mrs'
    echo '    url: <URL>'
    echo '    interval: 86400'
    echo ''
    echo '  Example-MRS-Classical:'
    echo '    type: http'
    echo '    behavior: classical'
    echo '    format: mrs'
    echo '    url: <URL>'
    echo '    interval: 86400'
    echo '```'
    echo
  fi

  echo "---"
  echo "_This README is auto-generated. Do not edit manually._"
} > "$TMP_README"

# 覆盖写入 README.md（仅当有变化时）
if [ -f README.md ]; then
  if cmp -s "$TMP_README" README.md; then
    echo "README.md unchanged."
    rm -f "$TMP_README"
    exit 0
  fi
fi

mv "$TMP_README" README.md

# 提交变更（仅 README）
if [[ -n "$(git status --porcelain README.md)" ]]; then
  git config user.name 'GitHub Actions Bot'
  git config user.email 'actions@github.com'
  git add README.md
  git commit -m "docs(readme): auto-update index at ${now_time}"
  git push
else
  echo "No changes to commit for README.md."
fi