"""路径与全局配置，均可用环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CALLBACK_DATA_DIR", WORKSPACE / "data"))
DB_PATH = Path(os.environ.get("CALLBACK_DB", DATA_DIR / "callback.db"))
REC_DIR = Path(os.environ.get("CALLBACK_REC_DIR", DATA_DIR / "recordings"))

HOST = os.environ.get("CALLBACK_HOST", "127.0.0.1")
PORT = int(os.environ.get("CALLBACK_PORT", "8000"))

# 再次预约距承诺时刻的最小缓冲：留给坐席准备，也避免约到“当下”
RESCHEDULE_LEAD_SECONDS = int(os.environ.get("CALLBACK_LEAD_SECONDS", "120"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REC_DIR.mkdir(parents=True, exist_ok=True)
