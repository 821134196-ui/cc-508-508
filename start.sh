#!/usr/bin/env bash
# 单条本地命令启动客服回拨承诺管理系统。
# 首次启动自动建库并注入含"逾期样例"的演示数据（SQLite + 本地虚构录音 + 模拟电话/短信）。
#   ./start.sh            # 首次注入样例；之后保留数据
#   RESET=1 ./start.sh    # 重置数据库并重新注入样例
#   PORT=8080 ./start.sh  # 指定端口
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
DB="callback/data/callback.db"

RESET_FLAG=""
if [[ "${RESET:-0}" == "1" || ! -f "$DB" ]]; then
  RESET_FLAG="--reset"
fi

PYTHON="${PYTHON:-python3}"
echo "启动客服回拨承诺管理系统 http://${HOST}:${PORT} （Ctrl+C 停止）"
exec "$PYTHON" -m callback --host "$HOST" --port "$PORT" $RESET_FLAG
