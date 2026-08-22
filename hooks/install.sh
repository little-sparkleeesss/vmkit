#!/usr/bin/env bash
# hooks/install.sh —— 启用本仓库的 pre-commit 钩子（幂等）
#   通过 core.hooksPath 指向 hooks/，钩子脚本随仓库版本管理（可审查、可随分支演进）。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chmod +x "$ROOT/hooks/pre-commit"
git -C "$ROOT" config core.hooksPath hooks
echo "pre-commit 钩子已启用（core.hooksPath=hooks）"
echo "验证：git commit 前自动跑 hooks/pre-commit；空白 / py 语法 / 密钥 / 冲突标记 / 换行检测"
echo "跳过：git commit --no-verify"
