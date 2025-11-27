#!/usr/bin/env bash
set -e

# 路径定义
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERGER_PY="${SCRIPT_DIR}/lib/merger.py"
CONFIG_FILE="merge-config.yaml"
DIST_DIR="merged-rules"  # <--- 修改为 merged-rules

# 检查依赖
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: $CONFIG_FILE not found in root directory!"
    exit 1
fi

# 创建目录
mkdir -p "$DIST_DIR"

# 执行合并
echo "Starting rule merger..."
python3 "$MERGER_PY"

echo "Success: Rules mirrored and merged into '$DIST_DIR/'"
