#!/usr/bin/env bash
set -e

# =================================================
# 0. 环境探测
# =================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# 自动寻找配置文件
if [ -f "$REPO_ROOT/sources.urls" ]; then
    SOURCES_FILE="$REPO_ROOT/sources.urls"
elif [ -f "$SCRIPT_DIR/sources.urls" ]; then
    SOURCES_FILE="$SCRIPT_DIR/sources.urls"
else
    echo "::error::sources.urls file not found!"
    exit 1
fi

RULES_DIR="rulesets"
STATS_SUCCESS=0
STATS_FAIL=0

# 颜色定义
INFO="\033[1;34m"
OK="\033[1;32m"
ERR="\033[1;31m"
NC="\033[0m"

gh_group_start() { echo "::group::🔹 $1"; }
gh_group_end() { echo "::endgroup::"; }

# =================================================
# 1. 清理工作区
# =================================================
gh_group_start "Resetting Workspace"
echo -e "${INFO}[INIT]${NC} Cleaning '$RULES_DIR'..."
if [ -d "$RULES_DIR" ]; then rm -rf "$RULES_DIR"; fi
mkdir -p "$RULES_DIR"
gh_group_end

# =================================================
# 2. 智能解析与下载
# =================================================
gh_group_start "Parsing & Downloading"

# 初始化状态变量
current_policy=""
current_type=""

echo -e "${INFO}[CONF]${NC} Reading: $SOURCES_FILE"

# 逐行读取
while IFS= read -r line || [ -n "$line" ]; do
    # 1. 清洗行 (去除 Windows 换行符，去除首尾空格)
    line=$(echo "$line" | tr -d '\r' | xargs)

    # 2. 忽略空行和注释
    if [[ -z "$line" ]] || [[ "$line" == \#* ]]; then continue; fi

    # 3. 检测 [policy:xxx]
    if [[ "$line" =~ ^\[policy:(.+)\]$ ]]; then
        current_policy="${BASH_REMATCH[1]}"
        echo -e "   👉 Set Policy: ${INFO}$current_policy${NC}"
        continue
    fi

    # 4. 检测 [type:xxx]
    if [[ "$line" =~ ^\[type:(.+)\]$ ]]; then
        current_type="${BASH_REMATCH[1]}"
        echo -e "   👉 Set Type:   ${INFO}$current_type${NC}"
        continue
    fi

    # 5. 忽略非 URL 的行 (比如 "已检查")
    if [[ "$line" != http* ]]; then
        continue
    fi

    # 6. 此时 line 只能是 URL 了，开始处理
    url="$line"

    # 检查状态是否就绪
    if [[ -z "$current_policy" ]] || [[ -z "$current_type" ]]; then
        echo -e "${ERR}[SKIP]${NC} URL found but Policy or Type is undefined. Line: $line"
        continue
    fi

    # --- 智能提取 Owner (作者名) ---
    # 逻辑：去除 https://，去除 gh-proxy 前缀，然后取第2个字段
    # 例子: https://github.com/User/Repo -> User
    # 例子: https://gh-proxy.com/https://github.com/User/Repo -> User
    
    clean_url="${url/https:\/\/gh-proxy.com\//}" # 去除代理前缀
    clean_url="${clean_url/https:\/\//}"          # 去除协议头
    
    # 提取所有者 (默认取路径的第一段，例如 github.com/Owner/...)
    # 大多数 github 链接是 domain/owner/repo
    owner=$(echo "$clean_url" | awk -F'/' '{print $2}')
    
    # 如果提取失败（比如域名不是 github），给个默认值
    if [[ -z "$owner" ]] || [[ "$owner" == "raw" ]] || [[ "$owner" == "refs" ]]; then
        owner="Unknown"
    fi

    filename=$(basename "$clean_url")
    
    # 构建目录: rulesets/reject/domain/MetaCubeX/
    target_dir="$RULES_DIR/$current_policy/$current_type/$owner"
    target_file="$target_dir/$filename"

    mkdir -p "$target_dir"
    echo -e "${INFO}[DOWN]${NC} $filename ($owner)"

    # 下载
    if curl -sSL --retry 3 --connect-timeout 15 -o "$target_file" "$url"; then
         if [ -s "$target_file" ]; then
            STATS_SUCCESS=$((STATS_SUCCESS + 1))
         else
            rm -f "$target_file"
            echo -e "${ERR}[FAIL]${NC} Empty file."
            STATS_FAIL=$((STATS_FAIL + 1))
         fi
    else
         echo -e "${ERR}[FAIL]${NC} Network error."
         STATS_FAIL=$((STATS_FAIL + 1))
    fi

done < "$SOURCES_FILE"

gh_group_end

# =================================================
# 3. 结算
# =================================================
echo "::notice::Processed. Success: $STATS_SUCCESS, Failed: $STATS_FAIL"

if [ "$STATS_SUCCESS" -eq 0 ]; then
    echo -e "${ERR}[CRITICAL]${NC} Zero files downloaded! Check sources.urls content."
    # 只有当真的一条都没下下来时，才报错停止，防止误删仓库
    exit 1
fi
