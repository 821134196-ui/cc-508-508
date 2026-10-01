#!/usr/bin/env python3
"""单条命令启动回拨承诺台。

    python3 run.py            # 重置数据库、灌入含逾期样例的演示数据并启动 Web
    python3 run.py --keep-db  # 保留现有数据库，仅启动
    python3 run.py --no-serve # 只建样例不启动（用于快速验证）

可选环境变量：CALLBACK_HOST / CALLBACK_PORT / CALLBACK_DATA_DIR
"""
from __future__ import annotations

import argparse
import sys

from callback import config
from callback.seed import seed
from callback.web import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回拨承诺管理系统（模拟电话/短信）")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--keep-db", action="store_true", help="保留现有数据库")
    parser.add_argument("--no-serve", action="store_true", help="只建样例不启动")
    args = parser.parse_args(argv)

    config.ensure_dirs()
    if not args.keep_db:
        seed(reset=True)
    if args.no_serve:
        print("[run] --no-serve，已完成建样例。")
        return 0
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
