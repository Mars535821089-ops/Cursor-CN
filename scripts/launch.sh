#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
ACTION="${1:-status}"
shift || true
PYTHON="${CURSOR_ZH_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for CANDIDATE in "$(command -v python3 || true)" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [ -n "$CANDIDATE" ] && [ -x "$CANDIDATE" ] || continue
    if [ "$CANDIDATE" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
    if "$CANDIDATE" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1; then
      PYTHON="$CANDIDATE"
      break
    fi
  done
fi
if [ -z "$PYTHON" ]; then
  echo "需要 Python 3.9 或以上版本。请先从 https://www.python.org/downloads/macos/ 安装。"
  echo "不会自动安装 Xcode、使用 sudo 或修改系统权限。"
  exit 1
fi
echo "Cursor-汉化中文补丁 v1.0.0"
case "$ACTION" in
  install)
    echo "仅支持已验证的 macOS Apple Silicon / Cursor 3.24.18 构建。"
    echo "会保留完整原版并为隔离副本本地签名；首次启动可能需要你确认系统权限。"
    exec "$PYTHON" scripts/install.py install --yes --quit-cursor "$@"
    ;;
  status) exec "$PYTHON" scripts/install.py status "$@" ;;
  restore-latest) exec "$PYTHON" scripts/recover_latest.py "$@" ;;
  *) echo "不支持的操作：$ACTION"; exit 1 ;;
esac
