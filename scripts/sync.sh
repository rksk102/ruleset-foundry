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
# 1. 环境清理
# =================================================
gh_group_start "Resetting Workspace"
echo -e "${INFO}[INIT]${NC} cleaning workspace..."

# 注意：这里我们先不急着删，等确认 sources.urls 存在再说
if [ ! -f "$SOURCES_FILE" ]; then
    echo -e "${ERR}[ERR]${NC} Sources file not found: $SOURCES_FILE"
    exit 1
fi

if [ -d "$RULES_DIR" ]; then
    echo "Removing existing directory..."
    rm -rf "$RULES_DIR"
fi
mkdir -p "$RULES_DIR"
echo -e "${OK}[OK]${NC} Directory '$RULES_DIR' created."
gh_group_end

# =================================================
# 2. 下载流程
# =================================================
gh_group_start "Downloading Sources"

# 读取 sources.urls，同时处理 Windows (\r\n) 和 Linux (\n) 换行符
# grep 过滤注释和空行
mapfile -t URLS < <(grep -v '^\s*#' "$SOURCES_FILE" | grep -v '^\s*$' | tr -d '\r')
TOTAL_URLS=${#URLS[@]}

echo -e "${INFO}[INFO]${NC} Found $TOTAL_URLS rules in config."

if [ "$TOTAL_URLS" -eq 0 ]; then
    echo -e "${ERR}[ERR]${NC} sources.urls appears to be empty or invalid!"
    gh_error "sources.urls contains no valid URLs"
    exit 1
fi

for line in "${URLS[@]}"; do
    # 读取 4 个参数 (使用 awk 增强兼容性，防止空格问题)
    policy=$(echo "$line" | awk '{print $1}')
    type=$(echo "$line" | awk '{print $2}')
    owner=$(echo "$line" | awk '{print $3}')
    url=$(echo "$line" | awk '{print $4}')
    
    if [[ -z "$url" ]]; then 
        echo -e "${WARN}[SKIP]${NC} Invalid line format: $line"
        continue
    fi

    filename=$(basename "$url")
    
    # 目标路径
    target_dir="$RULES_DIR/$policy/$type/$owner"
    target_file="$target_dir/$filename"
    
    mkdir -p "$target_dir"
    
    echo -e "${INFO}[DOWN]${NC} Fetching: $url"
    
    # 下载
    if curl -sSL --retry 3 --retry-delay 2 --connect-timeout 15 -o "$target_file" "$url"; then
        # 检查文件是否为空 (有些 404 可能会返回空文件或 HTML)
        if [ -s "$target_file" ]; then
            echo -e "${OK}[ OK ]${NC} Saved to $target_dir"
            STATS_SUCCESS=$((STATS_SUCCESS + 1))
        else
            echo -e "${ERR}[FAIL]${NC} File is empty."
            rm -f "$target_file"
            STATS_FAIL=$((STATS_FAIL + 1))
        fi
    else
        echo -e "${ERR}[FAIL]${NC} Curl failed."
        echo "::warning::Download failed: $url"
        STATS_FAIL=$((STATS_FAIL + 1))
    fi
done

gh_group_end

# =================================================
# 3. 安全检查与摘要
# =================================================
echo "::notice::Download logic finished. Success: $STATS_SUCCESS, Failed: $STATS_FAIL"

# 【安全刹车】
# 如果 0 个文件下载成功，说明出大问题了（网络断了 or 配置错了 or 格式不对）
# 此时必须报错退出，防止 Workflow 继续运行并将“空文件夹”提交到 Git，导致仓库内容被清空。
if [ "$STATS_SUCCESS" -eq 0 ]; then
    echo -e "${ERR}[CRITICAL]${NC} Zero files downloaded! Aborting workflow to protect repository."
    gh_error "Safety Stop: No rules were downloaded. Check sources.urls formatting or network."
    exit 1
fi

# 如果有部分失败，不中断，但给予警告
if [ $STATS_FAIL -gt 0 ]; then
    echo -e "${WARN}[WARN]${NC} Some downloads failed, but proceeding with valid files."
fi
