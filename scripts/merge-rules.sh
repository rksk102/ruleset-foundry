#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERGER_PY="${SCRIPT_DIR}/lib/merger.py"
DIST_DIR="merged-rules"

# 清理旧的生成目录（可选，防止保留了删掉的文件）
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

echo "Starting Merge Process..."

# 调用 Python 脚本
python3 "$MERGER_PY"

echo "Success! Check the '$DIST_DIR' directory."
