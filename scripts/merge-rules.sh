#!/usr/bin/env bash
set -e

# =================================================
# 配置与路径
# =================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERGER_PY="${SCRIPT_DIR}/lib/merger.py"
CONFIG_FILE="merge-config.yaml"

# 颜色定义
ERR="\033[1;31m"
INFO="\033[1;34m"
NC="\033[0m"

# =================================================
# 1. 检查前置条件
# =================================================
echo -e "${INFO}[INIT]${NC} Starting Merge Process..."

if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${ERR}[ERROR]${NC} Configuration file '$CONFIG_FILE' not found in root directory!"
    echo "::error file=$CONFIG_FILE::Config file missing"
    exit 1
fi

# =================================================
# 2. 检查并安装依赖 (PyYAML)
# =================================================
# 简单的检查，如果在 CI 环境通常已经安装，本地运行则自动补全
if ! python3 -c "import yaml" &> /dev/null; then
    echo -e "${INFO}[DEP]${NC} PyYAML not found. Installing..."
    pip3 install PyYAML -q
else
    echo -e "${INFO}[DEP]${NC} Dependencies matched."
fi

# =================================================
# 3. 运行合并脚本
# =================================================
# 设置 Python 环境变量以确保实时输出和 UTF-8 编码
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8

echo -e "${INFO}[RUN]${NC} Executing merger logic..."

# 调用 Python 脚本
# 注意：merger.py 现在会自动清理 merged-rules 文件夹内的内容
python3 "$MERGER_PY"

# 捕获 Python 脚本的退出状态
RET_CODE=$?

if [ $RET_CODE -eq 0 ]; then
    echo -e "${INFO}[DONE]${NC} Merge finished successfully."
else
    echo -e "${ERR}[FAIL]${NC} Merge script encountered errors."
    exit $RET_CODE
fi
