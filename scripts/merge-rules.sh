name: "Build MRS from TXT"

on:
  push:
    branches: [ main ]
    paths:
      - 'rulesets/**'
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: build-mrs
  cancel-in-progress: true

jobs:
  build-mrs:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4

      # 如你的文件里已经有下载工具的步骤，可以保留；
      # 但兜底步骤要放在 Convert 之前。

      - name: Setup Go (for fallback install)
        uses: actions/setup-go@v5
        with:
          go-version: '1.21'

      # 兜底安装步骤：放在“Convert TXT to MRS”之前
      - name: Ensure mrs-generator present
        shell: bash
        env:
          GOPROXY: https://proxy.golang.org,direct
        run: |
          set -euo pipefail
          if [ -x "tools/mrs-generator" ]; then
            echo "Using existing tools/mrs-generator"
            exit 0
          fi

          echo "tools/mrs-generator not found, trying to install via Go proxy..."
          # setup-go 已经装好 go 了，这里只需要调用
          go version

          # 尝试多个候选模块路径
          candidates=(
            "github.com/MetaCubeX/mrs-generator@v1.1.0"
            "github.com/MetaCubeX/mrs-generator@latest"
            "github.com/KOP-XIAO/mrs-generator@latest"
            "github.com/Mihomo-Rules/mrs-generator@latest"
          )
          ok=""
          for mod in "${candidates[@]}"; do
            echo "Trying: $mod"
            if go install "$mod"; then
              ok="$mod"
              break
            fi
          done
          if [ -z "$ok" ]; then
            echo "::error::Failed to install mrs-generator from Go proxy with all candidates."
            exit 1
          fi

          # 将安装的二进制复制到 tools/
          BIN="$(go env GOPATH)/bin/mrs-generator"
          if [ ! -x "$BIN" ]; then
            echo "::error::Installed binary not found at $BIN"
            exit 1
          fi
          mkdir -p tools
          cp "$BIN" tools/mrs-generator
          chmod +x tools/mrs-generator
          echo "Installed mrs-generator from $ok"

      - name: Convert TXT to MRS
        shell: bash
        run: |
          set -euo pipefail
          if [ ! -x "tools/mrs-generator" ]; then
            echo "::error::tools/mrs-generator not found or not executable."
            exit 1
          fi

          find rulesets -type f -name "*.txt" | while read -r in_file; do
            out_file=$(echo "$in_file" | sed 's|^rulesets/|mrs-rules/|' | sed 's|\.txt$|.mrs|')
            echo "Converting: $in_file  ==>  $out_file"
            mkdir -p "$(dirname "$out_file")"
            ./tools/mrs-generator -i "$in_file" -o "$out_file"
          done

      - name: Commit and Push MRS files
        shell: bash
        run: |
          set -euo pipefail
          if [[ -z $(git status --porcelain mrs-rules) ]]; then
            echo "No changes to MRS files. Nothing to commit."
            exit 0
          fi
          git config user.name 'GitHub Actions Bot'
          git config user.email 'actions@github.com'
          git add mrs-rules/
          git commit -m "chore(mrs): Auto-build MRS files"
          git push
