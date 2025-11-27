#!/usr/bin/env bash
set -e

# 定义路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERGER_PY="${SCRIPT_DIR}/lib/merger.py"
CONFIG_FILE="merge-config.yaml"

# 检查配置
if [ ! -f "$CONFIG_FILE" ]; then
    echo "::error file=$CONFIG_FILE::Configuration file not found!"
    exit 1
fi

# 安装依赖 (以防万一 workflow 没装，作为双重保险，或者本地运行用)
if ! python3 -c "import yaml" &> /dev/null; then
    echo "Installing PyYAML..."
    pip3 install PyYAML -q
fi

# 强制 Python 使用无缓冲 I/O 和 UTF-8
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8

# 运行
python3 "$MERGER_PY"
