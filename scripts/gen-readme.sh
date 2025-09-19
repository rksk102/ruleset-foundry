#!/usr/bin/env bash
set -euo pipefail

# 环境变量与默认值
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
  local kind="${2:-$CDN}"
  case "$kind" in
    jsdelivr) echo "https://cdn.jsdelivr.net/gh/${REPO}@${REF}/${path}" ;;
    raw)      echo "https://raw.githubusercontent.com/${REPO}/${REF}/${path}" ;;
    *)        echo "https://cdn.jsdelivr.net/gh/${REPO}@${REF}/${path}" ;;
  esac
}

# 检测 behavior（domain/ipcidr/classical）
detect_behavior() {
  local f="$1"
  awk '
    BEGIN{total=0; domain_ok=1; ipcidr_ok=1}
    {
      line=$0
      sub(/^[ \t]+/,"",line); sub(/[ \t]+$/,"",line)
      if (line=="" || line ~ /^[#!;]/) next
      total++
      # 纯 FQDN（不含通配符）
      if (line !~ /^([A-Za-z0-9-]+\.)+[A-Za-z0-9-]+$/) domain_ok=0
      # IPv4/IPv6 CIDR
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

# 统计行数
line_count_total() {
  local f="$1"
  wc -l < "$f" | awk '{print $1+0}'
}
line_count_effective() {
  local f="$1"
  awk '
    BEGIN{eff=0}
    {
      line=$0
      sub(/^[ \t]+/,"",line); sub(/[ \t]+$/,"",line)
      if (line=="" || line ~ /^[#!;]/) next
      eff++
    }
    END{ print eff+0 }
  ' "$f"
}

# 文件大小（兼容 Linux/macOS）
file_size_bytes() {
  local f="$1"
  if stat -c%s "$f" >/dev/null 2>&1; then
    stat -c%s "$f"
  else
    stat -f%z "$f" 2>/dev/null || echo 0
  fi
}
human_size() {
  awk 'function human(x){
    split("B KB MB GB TB PB",u," ")
    i=1
    while (x>=1024 && i<6) {x/=1024; i++}
    return (i==1 ? x : sprintf("%.1f",x)) " " u[i]
  }
  {print human($1)}'
}

# 时间
now_date="$(TZ="$TZ" date +'%Y-%m-%d')"
now_time="$(TZ="$TZ" date +'%Y-%m-%d %H:%M:%S %Z')"

# 仅处理 merged-rules
if [ ! -d merged-rules ]; then
  echo "merged-rules directory not found. Nothing to do."
  exit 0
fi

# 根目录汇总
total_files=0
total_bytes=0
total_lines_eff=0
count_domain=0
count_ipcidr=0
count_classical=0

while IFS= read -r -d '' f; do
  total_files=$((total_files+1))
  sz="$(file_size_bytes "$f")"; total_bytes=$((total_bytes+sz))
  le="$(line_count_effective "$f")"; total_lines_eff=$((total_lines_eff+le))
  beh="$(detect_behavior "$f")"
  case "$beh" in
    domain) count_domain=$((count_domain+1));;
    ipcidr) count_ipcidr=$((count_ipcidr+1));;
    *)      count_classical=$((count_classical+1));;
  esac
done < <(find merged-rules -maxdepth 1 -type f -print0 | sort -z)

total_bytes_h="$(printf "%s\n" "$total_bytes" | human_size)"

ALT_CDN="raw"
if [ "$CDN" = "raw" ]; then ALT_CDN="jsdelivr"; fi

# 生成 README
TMP_README="$(mktemp)"
{
  echo "# Merged Rules Index"
  echo
  echo "> 自动生成的合并规则索引，仅覆盖 merged-rules/ 目录。"
  echo
  echo "- 构建日期：${now_date}"
  echo "- 构建时间：${now_time}"
  echo "- 仓库：${REPO}"
  echo "- 引用 Ref：${REF}"
  echo "- 默认链接：${CDN}（每条目同时提供 ${ALT_CDN} 备用链接）"
  echo
  echo "快速统计："
  echo "- 根目录文件数：${total_files}（domain: ${count_domain}｜ipcidr: ${count_ipcidr}｜classical: ${count_classical}）"
  echo "- 有效规则行总计：${total_lines_eff}"
  echo "- 总体积：${total_bytes_h}"
  echo
  echo "目录"
  echo "- [合并产物（推荐引用）](#1-合并产物merged-rules-根目录推荐引用)"
  echo "- [未合并的镜像原文件](#2-未合并的镜像原文件merged-rulespolicytypeowner)"
  echo "- [使用示例](#使用示例)"
  echo "- [注记](#注记)"
  echo

  echo "## 1) 合并产物（merged-rules 根目录，推荐引用）"
  if find merged-rules -maxdepth 1 -type f -print -quit | grep -q . ; then
    echo
    echo "| File | Behavior | Lines (eff/total) | Size | Links |"
    echo "|---|---|---:|---:|---|"
    while IFS= read -r -d '' f; do
      file="$(basename "$f")"
      beh="$(detect_behavior "$f")"
      le="$(line_count_effective "$f")"
      lt="$(line_count_total "$f")"
      szb="$(file_size_bytes "$f")"
      szh="$(printf "%s\n" "$szb" | human_size)"
      url_main="$(build_url "merged-rules/${file}" "$CDN")"
      url_alt="$(build_url "merged-rules/${file}" "$ALT_CDN")"
      printf "| %s | %s | %s/%s | %s | [%s](%s) / [%s](%s) |\n" \
        "$file" "$beh" "$le" "$lt" "$szh" "$CDN" "$url_main" "$ALT_CDN" "$url_alt"
    done < <(find merged-rules -maxdepth 1 -type f -print0 | sort -z)
  else
    echo
    echo "_No merged files at merged-rules/ root_"
  fi
  echo

  echo "## 2) 未合并的镜像原文件（merged-rules/<policy>/<type>/<owner>/...）"
  if find merged-rules -mindepth 2 -type f -print -quit | grep -q . ; then
    echo
    echo "| Policy | Type | Owner | File | Lines (eff/total) | Size | Links |"
    echo "|---|---|---|---|---:|---:|---|"
    while IFS= read -r -d '' f; do
      rel="${f#merged-rules/}"
      policy="$(echo "$rel" | cut -d/ -f1)"
      rtype="$(echo "$rel" | cut -d/ -f2)"
      owner="$(echo "$rel" | cut -d/ -f3)"
      file="$(basename "$f")"
      le="$(line_count_effective "$f")"
      lt="$(line_count_total "$f")"
      szb="$(file_size_bytes "$f")"
      szh="$(printf "%s\n" "$szb" | human_size)"
      url_main="$(build_url "merged-rules/${policy}/${rtype}/${owner}/${file}" "$CDN")"
      url_alt="$(build_url "merged-rules/${policy}/${rtype}/${owner}/${file}" "$ALT_CDN")"
      printf "| %s | %s | %s | %s | %s/%s | %s | [%s](%s) / [%s](%s) |\n" \
        "$policy" "$rtype" "$owner" "$file" "$le" "$lt" "$szh" "$CDN" "$url_main" "$ALT_CDN" "$url_alt"
    done < <(find merged-rules -mindepth 2 -type f -print0 | sort -z)
  else
    echo
    echo "_No mirrored unmerged files under merged-rules/_"
  fi
  echo

  echo "## 使用示例"
  echo "<details><summary>mihomo rule-providers（合并产物示例）</summary>"
  echo
  echo '```yaml'
  echo '# 将 <URL> 替换为上表对应链接（可用 jsdelivr/raw 任一）'
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
  echo
  echo "</details>"
  echo
  echo "<details><summary>mihomo rule-providers（镜像原文件示例）</summary>"
  echo
  echo '```yaml'
  echo '# 按路径中的 type 选择 behavior:'
  echo '# type=domain   -> behavior: domain'
  echo '# type=ipcidr   -> behavior: ipcidr'
  echo '# type=classical-> behavior: classical'
  echo 'rule-providers:'
  echo '  Example-From-Mirrored:'
  echo '    type: http'
  echo '    behavior: domain   # 替换为对应类型'
  echo '    format: text'
  echo '    url: <URL>'
  echo '    interval: 86400'
  echo '```'
  echo
  echo "</details>"
  echo
  echo "---"
  echo "## 注记"
  echo "- README 由工作流自动生成；请勿手动编辑。"
  echo "- 若需切换为原始 Raw 链接，可使用“Links”列中的备用链接。"
  echo "- 行数统计的“有效行”已排除空行与以 #/;/! 开头的注释。"
} > "$TMP_README"

# 若未变化则退出（保持原有逻辑）
if [ -f README.md ]; then
  if cmp -s "$TMP_README" README.md; then
    echo "README.md unchanged."
    rm -f "$TMP_README"
    exit 0
  fi
fi

mv "$TMP_README" README.md

# 保留脚本内自提交（你的工作流也有提交步骤；二者不冲突）
if [[ -n "$(git status --porcelain README.md)" ]]; then
  git config user.name 'GitHub Actions Bot'
  git config user.email 'actions@github.com'
  git add README.md
  git commit -m "docs(readme): auto-update (merged-rules) at ${now_time}"
  git push || true
else
  echo "No changes to commit for README.md."
fi
