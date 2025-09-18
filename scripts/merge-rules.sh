#!/usr/bin/env bash
set -euo pipefail

# 基本配置
SOURCE_DIR="${SOURCE_DIR:-rulesets}"
OUTPUT_DIR="${OUTPUT_DIR:-merged-rules}"
CONFIG_FILE="${CONFIG_FILE:-merge-config.yaml}"

# 临时目录
TMP_DIR="${RUNNER_TEMP:-/tmp}/merge-tmp"
mkdir -p "$TMP_DIR"

# 启用 ** 递归通配与空匹配不报错
shopt -s globstar nullglob

# 规范化路径函数（优先 realpath，兼容 readlink -f）
canon() {
  local p="$1"
  if command -v realpath >/dev/null 2>&1; then
    realpath -m "$p"
  else
    readlink -f "$p"
  fi
}

# 记录参与合并的“源文件（规范化绝对路径）”
MERGED_FILES_LIST="${TMP_DIR}/merged_files.list"
: > "$MERGED_FILES_LIST"

# 规范化源目录与输出目录
SRC_ABS="$(canon "$SOURCE_DIR")"
OUT_ABS="$(canon "$OUTPUT_DIR")"

echo "=== Rule Sets Merger (fixed) ==="
echo "Source: $SRC_ABS"
echo "Output: $OUT_ABS"
echo "Config: $( [ -f "$CONFIG_FILE" ] && canon "$CONFIG_FILE" || echo "$CONFIG_FILE (will create sample)" )"
echo

# 若无配置，生成示例
if [ ! -f "$CONFIG_FILE" ]; then
  cat > "$CONFIG_FILE" <<'EOF'
merges:
  - name: all-adblock.txt
    description: "所有广告拦截域名规则"
    inputs:
      - block/domain/**/*.txt

  - name: all-direct.txt
    description: "所有直连域名规则"
    inputs:
      - direct/domain/**/*.txt

  - name: all-proxy.txt
    description: "所有代理域名规则"
    inputs:
      - proxy/domain/**/*.txt

  - name: china-ip.txt
    description: "中国大陆IP段"
    inputs:
      - direct/ipcidr/**/*.txt

  - name: ultimate-adblock.txt
    description: "终极广告拦截（域名+IP+classical）"
    inputs:
      - block/domain/**/*.txt
      - block/ipcidr/**/*.txt
      - block/classical/**/*.txt
EOF
  echo "Created sample $CONFIG_FILE"
fi

# 清空输出目录
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

# 简易 YAML 解析：识别 - name: / description: / inputs: - 模式
process_merges() {
  local in_task=0 in_inputs=0
  local current_name="" current_desc=""
  local inputs=()

  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue

    if [[ "$line" =~ ^[[:space:]]*-[[:space:]]*name:[[:space:]]*(.+)$ ]]; then
      if [ -n "$current_name" ] && [ ${#inputs[@]} -gt 0 ]; then
        execute_merge "$current_name" "$current_desc" "${inputs[@]}"
      fi
      in_task=1; in_inputs=0
      current_name="$(echo "${BASH_REMATCH[1]}" | sed 's/^"[[:space:]]*//; s/[[:space:]]*"$//')"
      current_desc=""
      inputs=()
      continue
    fi

    if [ $in_task -eq 1 ] && [[ "$line" =~ ^[[:space:]]*description:[[:space:]]*(.+)$ ]]; then
      current_desc="$(echo "${BASH_REMATCH[1]}" | sed 's/^"[[:space:]]*//; s/[[:space:]]*"$//')"
      continue
    fi

    if [ $in_task -eq 1 ] && [[ "$line" =~ ^[[:space:]]*inputs:[[:space:]]*$ ]]; then
      in_inputs=1
      continue
    fi

    if [ $in_task -eq 1 ] && [ $in_inputs -eq 1 ] && [[ "$line" =~ ^[[:space:]]*-[[:space:]]*(.+)$ ]]; then
      local pat="$(echo "${BASH_REMATCH[1]}" | sed 's/^"[[:space:]]*//; s/[[:space:]]*"$//')"
      inputs+=("$pat")
      continue
    fi

    if [ $in_task -eq 1 ] && [ $in_inputs -eq 1 ] && ! [[ "$line" =~ ^[[:space:]]*-[[:space:]]*.+$ ]]; then
      in_inputs=0
      continue
    fi
  done < "$CONFIG_FILE"

  if [ -n "$current_name" ] && [ ${#inputs[@]} -gt 0 ]; then
    execute_merge "$current_name" "$current_desc" "${inputs[@]}"
  fi
}

execute_merge() {
  local name="$1"; local desc="$2"; shift 2
  local input_patterns=("$@")

  echo "----------------------------------------"
  echo "Merging: $name"
  [ -n "$desc" ] && echo "Description: $desc"

  local tmp_all="${TMP_DIR}/merge_$$_all.txt"
  : > "$tmp_all"

  local matched_files=()
  local pat
  for pat in "${input_patterns[@]}"; do
    for file in "$SOURCE_DIR"/$pat; do
      [ -f "$file" ] && matched_files+=("$(canon "$file")")
    done
  done

  if [ ${#matched_files[@]} -gt 0 ]; then
    printf "%s\n" "${matched_files[@]}" | sort -u > "${TMP_DIR}/matched_$$_uniq.list"
  else
    echo "  ! No files matched. Skip task."
    return
  fi

  local cnt_files
  cnt_files=$(wc -l < "${TMP_DIR}/matched_$$_uniq.list" | tr -d ' ')
  echo "  Matched files: $cnt_files"

  while IFS= read -r absf; do
    local rel="${absf#$SRC_ABS/}"
    echo "  + $rel"
    cat "$absf" >> "$tmp_all"
    echo "$absf" >> "$MERGED_FILES_LIST"
  done < "${TMP_DIR}/matched_$$_uniq.list"

  local before=0 after=0
  [ -s "$tmp_all" ] && before=$(wc -l < "$tmp_all" | tr -d ' ')

  local out_file="$OUTPUT_DIR/$name"
  mkdir -p "$(dirname "$out_file")"
  grep -v '^[[:space:]]*$' "$tmp_all" 2>/dev/null \
    | grep -v '^[[:space:]]*[#!]' \
    | sed 's/^[[:space:]]\+//; s/[[:space:]]\+$//' \
    | awk 'NF' \
    | sort -u \
    > "$out_file"

  [ -s "$out_file" ] && after=$(wc -l < "$out_file" | tr -d ' ')
  echo "  Result: $cnt_files files -> $before lines -> $after unique lines"
  echo "  Output: $(canon "$out_file")"

  rm -f "$tmp_all" "${TMP_DIR}/matched_$$_uniq.list"
}

# 执行合并
process_merges

echo
echo "Step 2: Copy unmerged source files ..."

ALL_FILES_LIST="${TMP_DIR}/all_files.list"
: > "$ALL_FILES_LIST"
while IFS= read -r -d '' f; do
  echo "$(canon "$f")" >> "$ALL_FILES_LIST"
done < <(find "$SOURCE_DIR" -type f -print0)
sort -u -o "$ALL_FILES_LIST" "$ALL_FILES_LIST"

if [ -s "$MERGED_FILES_LIST" ]; then
  sort -u -o "$MERGED_FILES_LIST" "$MERGED_FILES_LIST"
else
  : > "$MERGED_FILES_LIST"
fi

UNMERGED_FILES_LIST="${TMP_DIR}/unmerged_files.list"
comm -23 "$ALL_FILES_LIST" "$MERGED_FILES_LIST" > "$UNMERGED_FILES_LIST" || true

copied=0
while IFS= read -r absf; do
  [ -z "$absf" ] && continue
  rel="${absf#$SRC_ABS/}"
  dest="$OUTPUT_DIR/$rel"
  mkdir -p "$(dirname "$dest")"
  cp "$absf" "$dest"
  copied=$((copied + 1))
done < "$UNMERGED_FILES_LIST"

echo "  Copied unmerged files: $copied"
find "$OUTPUT_DIR" -type d -empty -delete 2>/dev/null || true

echo
echo "=== Summary ==="
echo "Merged outputs at root: $(find "$OUTPUT_DIR" -maxdepth 1 -type f | wc -l | tr -d ' ')"
echo "Mirrored unmerged files: $(find "$OUTPUT_DIR" -mindepth 2 -type f | wc -l | tr -d ' ')"
echo "Total files in $OUTPUT_DIR: $(find "$OUTPUT_DIR" -type f | wc -l | tr -d ' ')"

# 提交变更
if [[ -z $(git status -s) ]]; then
  echo "No changes to commit."
  exit 0
fi

git config user.name 'GitHub Actions Bot'
git config user.email 'actions@github.com'
git add -A
git commit -m "chore(merge): update merged-rules (fix exclude merged sources) at $(date +'%Y-%m-%d %H:%M:%S %Z')"
git push