#!/usr/bin/env bash
set -euo pipefail

# 仅针对 merged-rules 目录生成 README
TZ="${TZ:-Asia/Shanghai}"
INPUT_REF="${INPUT_REF:-}"
INPUT_CDN="${INPUT_CDN:-jsdelivr}"

REPO="${GITHUB_REPOSITORY:-}"
if [ -z "$REPO" ]; then
  origin_url="$(git remote get-url origin 2>/dev/null || echo "")"
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

detect_behavior() {
  local f="$1"
  awk '
    BEGIN{total=0; domain_ok=1; ipcidr_ok=1}
    {
      line=$0
      sub(/^[ \t]+/,"",line); sub(/[ \t]+$/,"",line)
      if (line=="" || line ~ /^[#!]/) next
      total++
      if (line !~ /^(\*?[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+$/) domain_ok=0
      if (line !~ /^(([0-9]{1,3}\.){3}[0-9]{1,3}\/[0-9]{1,2}|[0-9A-Fa-f:]+\/[0-9]{1,3})$/) ipcidr_ok=0
    }
    END{
      if (total==0) { print "domain"; exit }
      if (domain_ok==1) { print "domain"; exit }
      if (ipcidr_ok==1) { print "ipcidr"; exit }
      print "classical"
    }
  ' "$f"
}

now_date="$(TZ="$TZ" date +'%Y-%m-%d')"
now_time="$(TZ="$TZ" date +'%Y-%m-%d %H:%M:%S %Z')"

if [ ! -d merged-rules ]; then
  echo "merged-rules directory not found. Nothing to do."
  exit 0
fi

TMP_README="$(mktemp)"
{
  echo "# Merged Rules Index"
  echo
  echo "- Build date: ${now_date}"
  echo "- Build time: ${now_time}"
  echo "- Repo: ${REPO}"
  echo "- Ref: ${REF}"
  echo "- CDN: ${CDN}"
  echo
  echo "本索引仅针对 merged-rules/。根目录为合并产物（建议优先引用），子目录中为未参与任何合并的镜像原文件（保留原始的 policy/type/owner 结构）。"
  echo

  echo "## 1) 合并产物（merged-rules 根目录，推荐引用）"
  if find merged-rules -maxdepth 1 -type f -print -quit | grep -q . ; then
    echo
    echo "| File | Behavior | URL |"
    echo "|---|---|---|"
    while IFS= read -r -d '' f; do
      file="$(basename "$f")"
      beh="$(detect_behavior "$f")"
      url="$(build_url "merged-rules/${file}")"
      printf "| %s | %s | %s |\n" "$file" "$beh" "$url"
    done < <(find merged-rules -maxdepth 1 -type f -print0 | sort -z)
    echo
    echo '示例（mihomo rule-providers）：'
    echo '```yaml'
    echo '# 将 <URL> 替换为上表对应链接'
    echo 'rule-providers:'
    echo '  Merged-Domain:'
    echo '    type: http'
    echo '    behavior: domain'
    echo '    format: text'
    echo '    url: <URL>'
    echo '    interval: 86400'
    echo ''
    echo '  Merged-IPCidr:'
    echo '    type: http'
    echo '    behavior: ipcidr'
    echo '    format: text'
    echo '    url: <URL>'
    echo '    interval: 86400'
    echo ''
    echo '  Merged-Classical:'
    echo '    type: http'
    echo '    behavior: classical'
    echo '    format: text'
    echo '    url: <URL>'
    echo '    interval: 86400'
    echo '```'
  else
    echo
    echo "_No merged files at merged-rules/ root_"
  fi
  echo

  echo "## 2) 未合并的镜像原文件（merged-rules/<policy>/<type>/<owner>/...）"
  if find merged-rules -mindepth 2 -type f -print -quit | grep -q . ; then
    echo
    echo "| Policy | Type | Owner | File | URL |"
    echo "|---|---|---|---|---|"
    while IFS= read -r -d '' f; do
      rel="${f#merged-rules/}"
      policy="$(echo "$rel" | cut -d/ -f1)"
      rtype="$(echo "$rel" | cut -d/ -f2)"
      owner="$(echo "$rel" | cut -d/ -f3)"
      file="$(basename "$f")"
      url="$(build_url "merged-rules/${policy}/${rtype}/${owner}/${file}")"
      printf "| %s | %s | %s | %s | %s |\n" "$policy" "$rtype" "$owner" "$file" "$url"
    done < <(find merged-rules -mindepth 2 -type f -print0 | sort -z)
    echo
    echo '示例（mihomo rule-providers，按路径中的 type 选择 behavior）：'
    echo '```yaml'
    echo '# type=domain -> behavior: domain'
    echo '# type=ipcidr -> behavior: ipcidr'
    echo '# type=classical -> behavior: classical'
    echo 'rule-providers:'
    echo '  Example-From-Mirrored:'
    echo '    type: http'
    echo '    behavior: domain   # 替换为对应类型'
    echo '    format: text'
    echo '    url: <URL>'
    echo '    interval: 86400'
    echo '```'
  else
    echo
    echo "_No mirrored unmerged files under merged-rules/_"
  fi
  echo
  echo "---"
  echo "_This README is auto-generated from merged-rules. Do not edit manually._"
} > "$TMP_README"

if [ -f README.md ]; then
  if cmp -s "$TMP_README" README.md; then
    echo "README.md unchanged."
    rm -f "$TMP_README"
    exit 0
  fi
fi

mv "$TMP_README" README.md

if [[ -n "$(git status --porcelain README.md)" ]]; then
  git config user.name 'GitHub Actions Bot'
  git config user.email 'actions@github.com'
  git add README.md
  git commit -m "docs(readme): auto-update (merged-rules) at ${now_time}"
  git push
else
  echo "No changes to commit for README.md."
fi
