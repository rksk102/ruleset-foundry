#!/usr/bin/env bash
set -e

# =================================================
# 配置
# =================================================
RULES_DIR="rulesets"
SOURCES_FILE="sources.urls"

# 计数器
STATS_SUCCESS=0
STATS_FAIL=0

# 颜色定义
INFO="\033[1;34m"
OK="\033[1;32m"
WARN="\033[1;33m"
ERR="\033[1;31m"
NC="\033[0m"

# GitHub Actions 辅助函数
gh_group_start() { echo "::group::🔹 $1"; }
gh_group_end() { echo "::endgroup::"; }
gh_error() { echo "::error file=$SOURCES_FILE::$1"; }

# =================================================
# 1. 环境清理 (强制重置)
# =================================================
gh_group_start "Resetting Workspace"
echo -e "${INFO}[INIT]${NC} Wiping directory: $RULES_DIR"

if [ -d "$RULES_DIR" ]; then
    rm -rf "$RULES_DIR"
fi
mkdir -p "$RULES_DIR"
echo -e "${OK}[OK]${NC} Directory clean and ready."
gh_group_end

# =================================================
# 2. 下载流程
# =================================================
gh_group_start "Downloading Sources"

if [ ! -f "$SOURCES_FILE" ]; then
    echo -e "${ERR}[ERR]${NC} Sources file not found: $SOURCES_FILE"
    gh_error "Missing sources.urls file"
    exit 1
fi

# 读取 sources.urls
mapfile -t URLS < <(grep -v '^\s*#' "$SOURCES_FILE" | grep -v '^\s*$')
TOTAL_URLS=${#URLS[@]}

echo -e "${INFO}[INFO]${NC} Processing $TOTAL_URLS sources..."

for line in "${URLS[@]}"; do
    # 读取 4 个参数
    read -r policy type owner url <<< "$line"
    
    if [[ -z "$url" ]]; then continue; fi

    filename=$(basename "$url")
    
    # 目标路径: rulesets/policy/type/owner/file
    target_dir="$RULES_DIR/$policy/$type/$owner"
    target_file="$target_dir/$filename"
    
    mkdir -p "$target_dir"
    
    echo -e "${INFO}[DOWN]${NC} $owner ($type) -> $filename"
    
    if curl -sSL --retry 3 --retry-delay 2 --connect-timeout 15 -o "$target_file" "$url"; then
        if [ -s "$target_file" ]; then
            echo -e "${OK}[ OK ]${NC} Success."
            STATS_SUCCESS=$((STATS_SUCCESS + 1))
        else
            echo -e "${ERR}[FAIL]${NC} Empty file downloaded."
            rm "$target_file"
            STATS_FAIL=$((STATS_FAIL + 1))
        fi
    else
        echo -e "${ERR}[FAIL]${NC} Download error: $url"
        echo "::warning::Failed to download: $url"
        STATS_FAIL=$((STATS_FAIL + 1))
    fi
done

gh_group_end

# =================================================
# 3. 摘要输出
# =================================================
echo "::notice::Download phase complete. Success: $STATS_SUCCESS, Failed: $STATS_FAIL"

if [ $STATS_SUCCESS -eq 0 ] && [ $STATS_FAIL -gt 0 ]; then
    exit 1
fi
