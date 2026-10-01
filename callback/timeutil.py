"""时间解析与禁扰时段判定。

承诺时刻统一存为本地 ISO（YYYY-MM-DDTHH:MM）。禁扰时段用每天的
“墙钟时间” HH:MM 表达，支持跨午夜（如 22:00–08:00）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

DATE_FMT = "%Y-%m-%dT%H:%M"


def parse_dt(value: str) -> datetime:
    return datetime.strptime(value, DATE_FMT)


def fmt_dt(value: datetime) -> str:
    return value.strftime(DATE_FMT)


def parse_hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def minutes_of_day(value: str) -> int:
    h, m = parse_hm(value)
    return h * 60 + m


def in_quiet_hours(moment: datetime, quiet_from: str | None, quiet_to: str | None) -> bool:
    """moment 是否落在禁扰区间内。未设置禁扰时段则恒为 False。

    支持跨午夜：from > to 时表示 [from,24:00)∪[00:00,to)。端点：
    区间按左闭右开处理，恰好在 quiet_to 时刻呼出不算打扰。
    """
    if not quiet_from or not quiet_to:
        return False
    start = minutes_of_day(quiet_from)
    end = minutes_of_day(quiet_to)
    cur = moment.hour * 60 + moment.minute
    if start == end:
        return False  # 长度为 0 的区间视为无禁扰
    if start < end:
        return start <= cur < end
    # 跨午夜
    return cur >= start or cur < end


def countdown_text(promised_at: str, now_dt: datetime) -> str:
    """界面倒计时文案：已逾期显示“超时 X”，未到显示“剩余 X”。"""
    target = parse_dt(promised_at)
    delta: timedelta = target - now_dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return "超时 " + _human_duration(-secs)
    return "剩余 " + _human_duration(secs)


def _human_duration(secs: int) -> str:
    if secs < 60:
        return f"{secs} 秒"
    mins, sec = divmod(secs, 60)
    if mins < 60:
        return f"{mins} 分 {sec:02d} 秒" if sec else f"{mins} 分钟"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours} 小时 {mins:02d} 分"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时"
