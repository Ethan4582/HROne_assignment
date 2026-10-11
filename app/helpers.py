import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional
from app.db import round_half_up

IST = timezone(timedelta(hours=5, minutes=30))
GRACE_PERIOD_SECONDS = 600
OVERTIME_MINIMUM_MINUTES = 30
HALF_DAY_HOURS_THRESHOLD = 4.50

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}


def epoch_ms_to_datetime(ms: int) -> datetime:
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc).replace(microsecond=0)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    in_ist = punch_in.astimezone(IST).replace(microsecond=0)
    cal_date = in_ist.date()
    p_time = in_ist.time()

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    if is_overnight and p_time < time(end_h, end_m, 0):
        return (cal_date - timedelta(days=1)).isoformat()
    return cal_date.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, att_date_str: str) -> int:
    att_date = date.fromisoformat(att_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    start_dt_ist = datetime.combine(att_date, time(start_h, start_m, 0), tzinfo=IST)

    in_ist = punch_in.astimezone(IST).replace(microsecond=0)
    delta_sec = (in_ist - start_dt_ist).total_seconds()

    if delta_sec > GRACE_PERIOD_SECONDS:
        return int(delta_sec // 60)
    return 0


def compute_overtime_minutes(punch_out: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    att_date = date.fromisoformat(att_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    end_date = att_date + timedelta(days=1) if (end_h, end_m) <= (start_h, start_m) else att_date
    end_dt_ist = datetime.combine(end_date, time(end_h, end_m, 0), tzinfo=IST)
    out_ist = punch_out.astimezone(IST).replace(microsecond=0)
    delta_sec = (out_ist - end_dt_ist).total_seconds()

    if delta_sec <= 0:
        return 0
    ot_minutes = int(delta_sec // 60)
    return ot_minutes if ot_minutes >= OVERTIME_MINIMUM_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    in_sec = int(punch_in.timestamp())
    out_sec = int(punch_out.timestamp())
    diff_sec = out_sec - in_sec
    hours_d = Decimal(diff_sec) / Decimal(3600)
    return float(hours_d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < HALF_DAY_HOURS_THRESHOLD


def calculate_working_days(joined_on_str: str, month_str: str) -> int:
    year, m = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_month = f"{month_str}-01"
    end_month = f"{month_str}-{last_day:02d}"

    if joined_on_str > end_month:
        return 0

    cur_date_str = max(start_month, joined_on_str)
    cur_date = date.fromisoformat(cur_date_str)
    end_date = date.fromisoformat(end_month)

    working_days = 0
    while cur_date <= end_date:
        if cur_date.weekday() < 5:
            working_days += 1
        cur_date += timedelta(days=1)
    return working_days


def format_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_out = []
    for h in doc.get("history", []):
        changes_out = {}
        for k, v in h.get("changes", {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                f_val = datetime_to_epoch_ms(f_val) if isinstance(f_val, datetime) else f_val
                t_val = datetime_to_epoch_ms(t_val) if isinstance(t_val, datetime) else t_val
            changes_out[k] = {"from": f_val, "to": t_val}

        history_out.append({
            "at": datetime_to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_out,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out,
    }
