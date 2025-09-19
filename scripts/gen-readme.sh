#!/usr/bin/env bash
set -euo pipefail

# 仅针对 merged-rules 目录生成 README（遍历所有文件，按路径分类展示，样式优化）
TZ="${TZ:-Asia/Shanghai}"
INPUT_REF="${INPUT_REF:-}"
INPUT_CDN="${INPUT_CDN:-jsdelivr}"

# 解析仓库名
REPO="${GITHUB_REPOSITORY:-}"
if [ -z "$REPO" ]; then
  origin_url="$(git remote get-url origin 2>/dev/null || echo "")"
  REPO="$(printf "%s" "$origin_url" \
    | sed -E 's#^git@github.com:([^/]+)/([^/]+)(\.git)?$#\1/\2#' \
    | sed -E 's#^https?://github.com/([^/]+)/([^/]+)(\.git)?$#\1/\2#')"
  REPO="${REPO:-owner/repo}"
fi

REF="${INPUT_REF:-main}"
CDN="${INPUT_CDN:-jsdelivr}"

# 构建链接（支持 jsdelivr/raw）
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

# 必须存在 merged-rules
if [ ! -d merged-rules ]; then
  echo "merged-rules directory not found. Nothing to do."
  exit 0
fi

# 统计总文件数（包含子目录与符号链接）
TOTAL_FILES="$(find merged-rules -mindepth 1 \( -type f -o -type l \) -print | wc -l | awk '{print $1+0}')"

TMP_README="$(mktemp)"
{
  echo "# Merged Rules Catalog"
  echo
  echo "> 自动生成的 merged-rules 索引：列出该目录下的全部规则文件（包含子目录），并依据路径结构标注分类。"
  echo
  echo "- Build date: ${now_date}"
  echo "- Build time: ${now_time}"
  echo "- Repo: ${REPO}"
  echo "- Ref: \`${REF}\`"
  echo "- Links via: \`${CDN}\`"
  echo "- Files total: ${TOTAL_FILES}"
  echo
  echo "字段说明："
  echo "- Policy: 路径第 1 段（block/direct/proxy）；根目录文件标记为 merged"
  echo "- Type: 路径第 2 段（domain/ipcidr/classical）；根目录文件为 -"
  echo "- Owner: 路径第 3 段；根目录文件为 -"
  echo
  echo "## All files under merged-rules/"
  if find merged-rules -mindepth 1 \( -type f -o -type l \) -print -quit | grep -q . ; then
    echo
    echo "| Policy | Type | Owner | File | Path | Link |"
    echo "|:--|:--|:--|:--|:--|:--|"
    # 列出包含子目录的全部文件，支持符号链接；按路径排序
    while IFS= read -r -d '' f; do
      rel="${f#merged-rules/}"
      # 解析路径段（>=4 段时取前 3 段作为 Policy/Type/Owner）
      # 示例：merged-rules/block/domain/Loyalsoldier/xxx.txt
      policy="$(printf '%s' "$rel" | awk -F/ 'NF>=4{print $1}')"
      rtype="$(printf '%s' "$rel" | awk -F/ 'NF>=4{print $2}')"
      owner="$(printf '%s' "$rel" | awk -F/ 'NF>=4{print $3}')"
      file="$(basename "$f")"
      if [ -z "$policy" ]; then
        policy="merged"; rtype="-"; owner="-"
      fi
      url="$(build_url "merged-rules/${rel}")"
      printf "| %s | %s | %s | \`%s\` | \`%s\` | [Open](%s) |\n" \
        "$policy" "$rtype" "$owner" "$file" "$rel" "$url"
    done < <(find merged-rules -mindepth 1 \( -type f -o -type l \) -print0 | LC_ALL=C sort -z)
  else
    echo
    echo "_No files found under merged-rules/_"
  fi
  echo
  echo "---"
  echo "_This README is auto-generated from merged-rules. Do not edit manually._"
} > "$TMP_README"

# 若未变化则退出
if [ -f README.md ]; then
  if cmp -s "$TMP_README" README.md; then
    echo "README.md unchanged."
    rm -f "$TMP_README"
    exit 0
  fi
fi

mv "$TMP_README" README.md

# 自提交（与工作流外层提交步骤不冲突）
if [[ -n "$(git status --porcelain README.md)" ]]; then
  git config user.name 'GitHub Actions Bot'
  git config user.email 'actions@github.com'
  git add README.md
  git commit -m "docs(readme): auto-update (merged-rules) at ${now_time}"
  git push || true
else
  echo "No changes to commit for README.md."
fi
