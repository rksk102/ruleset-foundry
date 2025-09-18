#!/usr/bin/env bash
set -euo pipefail

# 配置
SOURCE_DIR="rulesets"
OUTPUT_DIR="merged-rules"
CONFIG_FILE="${CONFIG_FILE:-merge-config.yaml}"
TMP_DIR="${RUNNER_TEMP:-/tmp}/merge-tmp"
mkdir -p "$TMP_DIR"

echo "=== Rule Sets Merger & Copier ==="
echo "Source: $SOURCE_DIR"
echo "Output: $OUTPUT_DIR"
echo "Config: $CONFIG_FILE"
echo

# 步骤1：复制所有原始文件到 merged-rules（保持目录结构）
echo "Step 1: Copying all original files..."
if [ -d "$SOURCE_DIR" ]; then
  # 清空目标目录（避免遗留旧文件）
  rm -rf "$OUTPUT_DIR"
  mkdir -p "$OUTPUT_DIR"
  
  # 复制整个目录结构
  cp -r "$SOURCE_DIR"/* "$OUTPUT_DIR"/ 2>/dev/null || true
  
  file_count=$(find "$OUTPUT_DIR" -type f | wc -l)
  echo "  Copied $file_count files to $OUTPUT_DIR"
else
  echo "  Source directory $SOURCE_DIR not found!"
  exit 1
fi
echo

# 步骤2：如果没有配置文件，创建默认示例
if [ ! -f "$CONFIG_FILE" ]; then
  echo "Creating default merge config..."
  cat > "$CONFIG_FILE" <<'EOF'
# 合并配置
# name: 合并后的文件名（保存在 merged-rules/ 根目录）
# inputs: 要合并的文件模式列表（相对于 rulesets/ 的路径）
# description: 可选的描述信息

merges:
  # 合并所有广告拦截域名
  - name: all-adblock.txt
    description: "所有广告拦截域名规则"
    inputs:
      - block/domain/**/*.txt
  
  # 合并所有直连域名
  - name: all-direct.txt
    description: "所有直连域名规则"
    inputs:
      - direct/domain/**/*.txt
  
  # 合并所有代理域名
  - name: all-proxy.txt
    description: "所有代理域名规则"
    inputs:
      - proxy/domain/**/*.txt
  
  # 合并所有中国IP段
  - name: china-ip.txt
    description: "中国大陆IP段"
    inputs:
      - direct/ipcidr/**/*.txt
  
  # 合并所有国外IP段
  - name: global-ip.txt
    description: "国外IP段"
    inputs:
      - proxy/ipcidr/**/*.txt
  
  # 超级广告拦截（域名+IP）
  - name: ultimate-adblock.txt
    description: "终极广告拦截规则集"
    inputs:
      - block/domain/**/*.txt
      - block/ipcidr/**/*.txt
      - block/classical/**/*.txt
  
  # 中国直连完整版（域名+IP）
  - name: china-all.txt
    description: "中国大陆完整直连规则"
    inputs:
      - direct/domain/**/*.txt
      - direct/ipcidr/**/*.txt
  
  # 国外代理完整版（域名+IP）
  - name: global-all.txt
    description: "国外完整代理规则"
    inputs:
      - proxy/domain/**/*.txt
      - proxy/ipcidr/**/*.txt
  
  # 自定义：Loyalsoldier 全部规则
  - name: loyalsoldier-all.txt
    description: "Loyalsoldier 提供的所有规则"
    inputs:
      - "*/*/Loyalsoldier/*.txt"
EOF
fi

# 步骤3：执行合并任务
echo "Step 2: Processing merge tasks..."

# 解析并执行合并
process_merges() {
  local in_merge=0
  local current_name=""
  local current_desc=""
  local current_inputs=()
  local task_count=0
  
  while IFS= read -r line; do
    # 跳过注释和空行
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$(echo "$line" | sed 's/[[:space:]]//g')" ]] && continue
    
    # 检测新的 merge 任务
    if [[ "$line" =~ ^[[:space:]]*-[[:space:]]*name:[[:space:]]*(.+) ]]; then
      # 执行上一个任务
      if [ -n "$current_name" ] && [ ${#current_inputs[@]} -gt 0 ]; then
        execute_merge "$current_name" "$current_desc" "${current_inputs[@]}"
        task_count=$((task_count + 1))
      fi
      # 开始新任务
      current_name="${BASH_REMATCH[1]}"
      current_desc=""
      current_inputs=()
      in_merge=1
      continue
    fi
    
    # 获取描述
    if [ "$in_merge" -eq 1 ] && [[ "$line" =~ description:[[:space:]]*\"(.+)\" ]]; then
      current_desc="${BASH_REMATCH[1]}"
      continue
    fi
    
    # 收集 inputs
    if [ "$in_merge" -eq 1 ] && [[ "$line" =~ ^[[:space:]]*-[[:space:]]*(.+) ]]; then
      local input="${BASH_REMATCH[1]}"
      # 去除引号
      input="${input//\"/}"
      current_inputs+=("$input")
      continue
    fi
  done < "$CONFIG_FILE"
  
  # 执行最后一个任务
  if [ -n "$current_name" ] && [ ${#current_inputs[@]} -gt 0 ]; then
    execute_merge "$current_name" "$current_desc" "${current_inputs[@]}"
    task_count=$((task_count + 1))
  fi
  
  echo
  echo "Processed $task_count merge tasks."
}

# 执行单个合并任务
execute_merge() {
  local name="$1"
  local desc="$2"
  shift 2
  local inputs=("$@")
  
  echo "----------------------------------------"
  echo "Merging: $name"
  [ -n "$desc" ] && echo "Description: $desc"
  echo "Input patterns: ${#inputs[@]} pattern(s)"
  
  local tmp_all="${TMP_DIR}/merge_$$.txt"
  : > "$tmp_all"
  
  local file_count=0
  local line_count_before=0
  
  # 收集所有匹配文件的内容
  for pattern in "${inputs[@]}"; do
    # 在 SOURCE_DIR 下查找匹配文件
    while IFS= read -r -d '' file; do
      if [ -f "$file" ]; then
        rel_path="${file#${SOURCE_DIR}/}"
        echo "  + $rel_path"
        cat "$file" >> "$tmp_all"
        file_count=$((file_count + 1))
      fi
    done < <(find "$SOURCE_DIR" -path "${SOURCE_DIR}/${pattern}" -type f -print0 2>/dev/null || true)
  done
  
  if [ "$file_count" -eq 0 ]; then
    echo "  ! No files matched. Skip."
    return
  fi
  
  line_count_before=$(wc -l < "$tmp_all" 2>/dev/null || echo 0)
  
  # 净化 + 去重 + 排序
  local output="${OUTPUT_DIR}/${name}"
  grep -v '^[[:space:]]*$' "$tmp_all" 2>/dev/null \
  | grep -v '^[[:space:]]*#' \
  | grep -v '^[[:space:]]*!' \
  | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
  | awk '!seen[$0]++' \
  | sort -u \
  > "$output" || echo -n > "$output"
  
  local line_count_after=$(wc -l < "$output" 2>/dev/null || echo 0)
  
  echo "  Result: $file_count files → $line_count_before lines → $line_count_after unique lines"
  echo "  Output: $output"
  
  # 清理临时文件
  rm -f "$tmp_all"
}

# 执行合并
process_merges

echo
echo "=== All tasks completed ==="

# 提交变更
if [[ -z $(git status -s) ]]; then
  echo "No changes to commit."
  exit 0
fi

git config user.name 'GitHub Actions Bot'
git config user.email 'actions@github.com'
git add -A
git commit -m "chore(merge): Update merged-rules at $(date +'%Y-%m-%d %H:%M:%S %Z')"
git push