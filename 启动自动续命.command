#!/bin/bash
# ZCode 自动续命监视器 - macOS 启动器(双击运行,与 Windows 端 启动自动续命.bat 对等)
# Python 探测级联:已有 python3(Homebrew/uv 托管/已装 CLT 的系统桩)→
# 都没有则自动安装 uv 托管版 Python(astral.sh 官方源,免管理员,装到用户目录)。
# 本文件必须保持 LF 行结束(gitattributes 已钉死),双击即用 Terminal 执行。
set -u
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# 仅当 Xcode CLT 已安装时才允许试探 /usr/bin/python3,
# 否则它会弹"安装开发者工具"的 GUI 对话框(体验极差)。
CLT_OK=0
[ -d /Library/Developer/CommandLineTools ] && CLT_OK=1

PY=""
for cand in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
            "$HOME"/.local/share/uv/python/cpython-3.12*/bin/python3; do
  if { command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; } \
     && "$cand" -c "import sys" >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ] && [ "$CLT_OK" = "1" ] && /usr/bin/python3 -c "import sys" >/dev/null 2>&1; then
  PY=/usr/bin/python3
fi

if [ -z "$PY" ]; then
  echo "未检测到可用的 Python 3,将自动安装 uv 官方托管版 Python:"
  echo "  - uv 安装脚本来源: https://astral.sh (约 1MB,装到 ~/.local/bin)"
  echo "  - Python 由 uv 从其 GitHub 官方发布页下载(免管理员,不碰系统目录)"
  echo "  - 全部可整目录删除,不修改系统设置"
  printf '按任意键继续,Ctrl+C 取消...'
  read -r -n 1 -s
  echo ""
  curl -LsSf https://astral.sh/uv/install.sh | sh \
    || { echo "uv 安装失败。请手动安装 Python 3 后重试: https://www.python.org/downloads/"; exit 1; }
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  uv python install 3.12 \
    || { echo "Python 安装失败,请检查网络后重试,或手动安装 Python 3。"; exit 1; }
  PY="$(uv python find 3.12)"
fi

echo "============================================"
echo " ZCode 自动续命监视器 (macOS)"
echo " 解释器: $PY"
echo " 最小化本窗口即可,勿关闭;重复双击不会重复启动"
echo "============================================"
exec "$PY" zcode_resume_watcher.py
