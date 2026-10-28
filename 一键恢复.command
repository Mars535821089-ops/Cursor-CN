#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
bash scripts/launch.sh restore-latest "$@"
echo "处理结束。按回车关闭窗口。"
read -r _
