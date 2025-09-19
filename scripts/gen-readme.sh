#!/usr/bin/env bash
set -euo pipefail

# 环境变量与默认值
TZ="${TZ:-Asia/Shanghai}"
INPUT_REF="${INPUT_REF:-}"
INPUT_CDN="${INPUT_CDN:-jsdelivr}"
DEBUG="${DEBUG:-0}"

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
ALT_CDN="raw"; if [ "$CDN" = "raw" ]; then ALT_CDN="jsdelivr"; fi

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

# 路径标准化为相对 merged-rules/ 的路径
normalize_rel() {
  # 输入可能是绝对/相对/包含 merged-rules/ 的路径；输出为 merged-rules/ 内部相对路径
  local p="$1"
  p="$(printf '%s' "$p" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
  [ -z "$p" ] && { echo ""; return; }
  p="$(printf '%s' "$p" | sed -E 's#^\.\/##; s#^/+##')"
  # 去掉任意前缀直到 merged-rules/
  if printf '%s' "$p" | grep -q 'merged-rules/'; then
    p="$(printf '%s' "$p" | sed -E 's#.*merged-rules/##')"
  fi
  echo "$p"
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
      if (line !~ /^(([0-9]{1,3}\.){3}[0-9]{1,2}\/[0-9]{1,2}|[0-9A-Fa-f:]+\/[0-9]{1,3})$/) ipcidr_ok=0
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
line_count_total() { wc -l < "$1" | awk '{print $1+0}'; }
line_count_effective() {
  awk '
    BEGIN{eff=0}
    {
      line=$0
      sub(/^[ \t]+/,"",line); sub(/[ \t]+$/,"",line)
      if (line=="" || line ~ /^[#!;]/) next
      eff++
    }
    END{ print eff+0 }
  ' "$1"
}

# 文件大小（通过 wc -c 跟随符号链接）
file_size_bytes() { wc -c < "$1" | awk '{print $1+0}'; }
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

# 必须存在 merged-rules
if [ ! -d merged-rules ]; then
  echo "merged-rules directory not found. Nothing to do."
  exit 0
fi

# 调试：列出目录结构
if [ "$DEBUG" = "1" ]; then
  echo "[DEBUG] tree of merged-rules (depth 2):"
  find merged-rules -maxdepth 2 -printf "%y %p\n" 2>/dev/null || true
fi

# 准备集合文件
USED_REL="$(mktemp)"
OUT_REL="$(mktemp)"
: > "$USED_REL"
: > "$OUT_REL"

# 读取 merge-map.tsv（若存在，用其第一列识别合并产出；第二列可作为已使用镜像）
if [ -f "merge-map.tsv" ]; then
  if [ "$DEBUG" = "1" ]; then echo "[DEBUG] merge-map.tsv found"; fi
  # 第一列：输出（可能是相对或包含 merged-rules/）
  awk -F'\t' 'NF>=1{print $1}' merge-map.tsv | sed -e 's/\r$//' | while read -r outp; do
    [ -z "$outp" ] && continue
    rel="$(normalize_rel "$outp")"
    # 输出通常在根目录，确保不带前缀目录
    printf "%s\n" "$rel"
  done | sort -u > "$OUT_REL.tmp" || true
  mv "$OUT_REL.tmp" "$OUT_REL"

  # 第二列：输入（用作 used 集合备选）
  awk -F'\t' 'NF>=2{print $2}' merge-map.tsv | sed -e 's/\r$//' | while read -r inp; do
    [ -z "$inp" ] && continue
    rel="$(normalize_rel "$inp")"
    printf "%s\n" "$rel"
  done | sort -u > "$USED_REL.tmp" || true
  mv "$USED_REL.tmp" "$USED_REL"
fi

# 读取 merge-used.list（若存在，用它覆盖 USED 集合，更精确）
if [ -f "merge-used.list" ]; then
  if [ "$DEBUG" = "1" ]; then echo "[DEBUG] merge-used.list found (override USED)"; fi
  : > "$USED_REL"
  sed -e 's/\r$//' merge-used.list | while read -r line; do
    line="$(printf '%s' "$line" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
    [ -z "$line" ] && continue
    case "$line" in \#* ) continue ;; esac
    rel="$(normalize_rel "$line")"
    printf "%s\n" "$rel"
  done | sort -u > "$USED_REL.tmp" || true
  mv "$USED_REL.tmp" "$USED_REL"
fi

# 统计集合大小
OUT_COUNT=0; USED_COUNT=0
[ -s "$OUT_REL" ] && OUT_COUNT="$(wc -l < "$OUT_REL" | awk '{print $1+0}')"
[ -s "$USED_REL" ] && USED_COUNT="$(wc -l < "$USED_REL" | awk '{print $1+0}')"
if [ "$DEBUG" = "1" ]; then
  echo "[DEBUG] outputs listed: $OUT_COUNT"
  echo "[DEBUG] used mirrored listed: $USED_COUNT"
fi

# 计算“参与合并的规则（合并产出）”的实际文件列表：
# 优先使用 OUT_REL；若为空则回退为根目录全部文件
OUT_FILES_TMP="$(mktemp)"
if [ -s "$OUT_REL" ]; then
  while read -r rel; do
    [ -z "$rel" ] && continue
    # 只展示位于 merged-rules 根目录下的输出（rel 不包含 /）
    if printf '%s\n' "$rel" | grep -q '/'; then
      # 如果配置里写了子路径，这里忽略，目标是根目录的合并产出文件
      continue
    fi
    fpath="merged-rules/$rel"
    [ -e "$fpath" ] && printf "%s\0" "$fpath"
  done < "$OUT_REL" > "$OUT_FILES_TMP"
fi
# 回退
if [ ! -s "$OUT_FILES_TMP" ]; then
  find merged-rules -maxdepth 1 \( -type f -o -type l \) -print0 | LC_ALL=C sort -z > "$OUT_FILES_TMP" || true
fi

# 统计根目录合并产出总览
total_files=0
total_bytes=0
total_lines_eff=0
count_domain=0
count_ipcidr=0
count_classical=0
while IFS= read -r -d '' f; do
  [ -f "$f" ] || [ -L "$f" ] || continue
  total_files=$((total_files+1))
  sz="$(file_size_bytes "$f")"; total_bytes=$((total_bytes+sz))
  le="$(line_count_effective "$f")"; total_lines_eff=$((total_lines_eff+le))
  beh="$(detect_behavior "$f")"
  case "$beh" in
    domain) count_domain=$((count_domain+1));;
    ipcidr) count_ipcidr=$((count_ipcidr+1));;
    *)      count_classical=$((count_classical+1));;
  esac
done < "$OUT_FILES_TMP"
total_bytes_h="$(printf "%s\n" "$total_bytes" | human_size)"

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
  echo "- 合并产出文件数（根目录）：${total_files}（domain: ${count_domain}｜ipcidr: ${count_ipcidr}｜classical: ${count_classical}）"
  echo "- 合并产出有效规则行总计：${total_lines_eff}"
  echo "- 合并产出总体积：${total_bytes_h}"
  echo
  echo "目录"
  echo "- [参与合并的规则（合并产出，推荐引用）](#1-参与合并的规则合并产出推荐引用)"
  echo "- [未参与合并的镜像原文件](#2-未参与合并的镜像原文件)"
  echo "- [使用示例](#使用示例)"
  echo "- [注记](#注记)"
  echo

  echo "## 1) 参与合并的规则（合并产出，推荐引用）"
  if [ -s "$OUT_FILES_TMP" ]; then
    echo
    echo "| File | Behavior | Lines (eff/total) | Size | Links |"
    echo "|---|---|---:|---:|---|"
    while IFS= read -r -d '' f; do
      [ -f "$f" ] || [ -L "$f" ] || continue
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
    done < "$OUT_FILES_TMP"
  else
    echo
    echo "_No merged outputs found at merged-rules/ root_"
  fi
  echo

  echo "## 2) 未参与合并的镜像原文件"
  # 构建未参与集合：镜像原文件总集 - USED_REL
  # 如果没有 USED_REL（既没有 merge-used.list 也没有 merge-map.tsv），则全部视为“未参与”（提示说明）
  MIRROR_LIST="$(mktemp)"
  find merged-rules -mindepth 2 \( -type f -o -type l \) -print0 | LC_ALL=C sort -z > "$MIRROR_LIST"

  if [ ! -s "$MIRROR_LIST" ]; then
    echo
    echo "_No mirrored unmerged files under merged-rules/_"
  else
    if [ ! -s "$USED_REL" ]; then
      echo
      echo "> 注意：未找到 merge-used.list/merge-map.tsv，无法区分已参与/未参与，以下为全部镜像原文件。"
    fi
    echo
    echo "| Policy | Type | Owner | File | Lines (eff/total) | Size | Links |"
    echo "|---|---|---|---|---:|---:|---|"
    while IFS= read -r -d '' f; do
      rel="${f#merged-rules/}"
      # 若存在 USED_REL 并包含该 rel，则跳过（说明已参与过合并）
      if [ -s "$USED_REL" ] && grep -Fqx "$rel" "$USED_REL"; then
        continue
      fi
      policy="$(printf '%s' "$rel" | cut -d/ -f1)"
      rtype="$(printf '%s' "$rel" | cut -d/ -f2)"
      owner="$(printf '%s' "$rel" | cut -d/ -f3)"
      file="$(basename "$f")"
      le="$(line_count_effective "$f")"
      lt="$(line_count_total "$f")"
      szb="$(file_size_bytes "$f")"
      szh="$(printf "%s\n" "$szb" | human_size)"
      url_main="$(build_url "merged-rules/${policy}/${rtype}/${owner}/${file}" "$CDN")"
      url_alt="$(build_url "merged-rules/${policy}/${rtype}/${owner}/${file}" "$ALT_CDN")"
      printf "| %s | %s | %s | %s | %s/%s | %s | [%s](%s) / [%s](%s) |\n" \
        "$policy" "$rtype" "$owner" "$file" "$le" "$lt" "$szh" "$CDN" "$url_main" "$ALT_CDN" "$url_alt"
    done < "$MIRROR_LIST"
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
  echo "---"
  echo "## 注记"
  echo "- 本 README 仅展示：合并产出（根目录）与“未参与合并”的镜像原文件。已参与合并的镜像原文件不再展示。"
  echo "- README 由工作流自动生成；请勿手动编辑。"
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

# 保留脚本内自提交（与工作流的提交步骤不冲突）
if [[ -n "$(git status --porcelain README.md)" ]]; then
  git config user.name 'GitHub Actions Bot'
  git config user.email 'actions@github.com'
  git add README.md
  git commit -m "docs(readme): auto-update (merged-rules) at ${now_time}"
  git push || true
else
  echo "No changes to commit for README.md."
fi
