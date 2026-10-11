from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest

from app.main import (
    IST,
    attendance_date,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    compute_half_day,
    round_half_up,
    calculate_working_days,
)


def test_round_half_up():
    # Verify half-up rounding (away from zero), not banker's rounding (half-to-even)
    assert round_half_up(2.675, 2) == 2.68
    assert round_half_up(2.525, 2) == 2.53
    assert round_half_up(2.685, 2) == 2.69
    assert round_half_up(0.005, 2) == 0.01
    assert round_half_up(10.12345, 4) == 10.1235
    assert round_half_up(10.12344, 4) == 10.1234


def test_attendance_date_normal_shift():
    # Normal shift 09:30 - 18:30 IST
    # Punch in at 09:28 IST on 2026-07-06 (03:58 UTC)
    dt_utc = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    assert attendance_date(dt_utc, "09:30", "18:30") == "2026-07-06"

    # Punch in late afternoon 18:00 IST on 2026-07-06 (12:30 UTC)
    dt_late = datetime(2026, 7, 6, 12, 30, 0, tzinfo=timezone.utc)
    assert attendance_date(dt_late, "09:30", "18:30") == "2026-07-06"


def test_attendance_date_overnight_shift():
    # Overnight shift 22:00 - 06:00 IST
    # Punch in at 21:55 IST on 2026-07-06 (16:25 UTC on 2026-07-06)
    dt_evening = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert attendance_date(dt_evening, "22:00", "06:00") == "2026-07-06"

    # Early-morning punch in at 01:15 IST on 2026-07-07 (19:45 UTC on 2026-07-06)
    # Earlier than shift_end (06:00) -> belongs to shift that started previous day (2026-07-06)
    dt_early_morning = datetime(2026, 7, 6, 19, 45, 0, tzinfo=timezone.utc)
    assert attendance_date(dt_early_morning, "22:00", "06:00") == "2026-07-06"

    # Punch in at 05:59:59 IST on 2026-07-07 -> belongs to 2026-07-06
    dt_just_before = datetime(2026, 7, 7, 0, 29, 59, tzinfo=timezone.utc)  # 00:29:59 UTC = 05:59:59 IST
    assert attendance_date(dt_just_before, "22:00", "06:00") == "2026-07-06"

    # Punch in at exactly 06:00:00 IST on 2026-07-07 -> not earlier than shift_end, belongs to 2026-07-07
    dt_exact_end = datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc)  # 00:30:00 UTC = 06:00:00 IST
    assert attendance_date(dt_exact_end, "22:00", "06:00") == "2026-07-07"


def test_compute_late_minutes_standard():
    # Shift 09:30. Grace = 10 min.
    # Early punch-in 09:15:00 IST
    dt_early = datetime(2026, 7, 6, 3, 45, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_early, "09:30", "2026-07-06") == 0

    # On-time punch-in 09:30:00 IST
    dt_ontime = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_ontime, "09:30", "2026-07-06") == 0

    # Exactly at grace cutoff: 09:40:00 IST (10 min exactly -> 0)
    dt_grace_exact = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_grace_exact, "09:30", "2026-07-06") == 0

    # One second past grace: 09:40:01 IST (strictly > 10 min -> 10 minutes late)
    dt_grace_plus_one = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_grace_plus_one, "09:30", "2026-07-06") == 10

    # 45 minutes late: 10:15:00 IST (strictly > 10 min -> 45 minutes late)
    dt_late_45 = datetime(2026, 7, 6, 4, 45, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_late_45, "09:30", "2026-07-06") == 45

    # 10:15:59 IST -> floor of 45 min 59 sec = 45
    dt_late_45_59 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_late_45_59, "09:30", "2026-07-06") == 45


def test_compute_late_minutes_overnight():
    # Overnight shift 22:00 - 06:00, attendance date = 2026-07-06.
    # Punch in at 22:10:00 IST -> 0 late
    dt_grace = datetime(2026, 7, 6, 16, 40, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_grace, "22:00", "2026-07-06") == 0

    # Punch in at 22:10:01 IST -> 10 min late
    dt_grace_plus = datetime(2026, 7, 6, 16, 40, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_grace_plus, "22:00", "2026-07-06") == 10

    # Punch in at 01:00:00 IST on 2026-07-07 (belongs to 2026-07-06 shift)
    # Elapsed from 22:00 = 3 hours = 180 minutes
    dt_overnight_late = datetime(2026, 7, 6, 19, 30, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_overnight_late, "22:00", "2026-07-06") == 180


def test_compute_overtime():
    # Normal shift 09:30 - 18:30 on 2026-07-06.
    # Punch out at 18:35:00 IST (5 min OT < 30 -> 0)
    dt_ot_5 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(dt_ot_5, "09:30", "18:30", "2026-07-06") == 0

    # Punch out at 18:59:59 IST (29 min 59 sec < 30 -> 0)
    dt_ot_29 = datetime(2026, 7, 6, 13, 29, 59, tzinfo=timezone.utc)
    assert compute_overtime_minutes(dt_ot_29, "09:30", "18:30", "2026-07-06") == 0

    # Punch out at 19:00:00 IST (exactly 30 min -> 30)
    dt_ot_30 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(dt_ot_30, "09:30", "18:30", "2026-07-06") == 30

    # Punch out at 19:10:00 IST (40 min -> 40)
    dt_ot_40 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(dt_ot_40, "09:30", "18:30", "2026-07-06") == 40


def test_compute_overtime_overnight():
    # Overnight shift 22:00 - 06:00 on 2026-07-06. Shift end is 2026-07-07 06:00 IST (00:30 UTC).
    # Punch out at 06:20:00 IST (20 min OT < 30 -> 0)
    dt_ot_early = datetime(2026, 7, 7, 0, 50, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(dt_ot_early, "22:00", "06:00", "2026-07-06") == 0

    # Punch out at 06:40:00 IST (40 min OT >= 30 -> 40)
    dt_ot_40 = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(dt_ot_40, "22:00", "06:00", "2026-07-06") == 40


def test_compute_work_hours():
    # Verify half-up rounding (R4)
    # 9630 seconds = 2.675 hours -> 2.68
    in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    out_dt = in_dt + timedelta(seconds=9630)
    assert compute_work_hours(in_dt, out_dt) == 2.68

    # 32820 seconds = 9.116666... hours -> 9.12
    out_sample1 = in_dt + timedelta(seconds=32820)
    assert compute_work_hours(in_dt, out_sample1) == 9.12

    # 32700 seconds = 9.083333... hours -> 9.08
    out_sample2 = in_dt + timedelta(seconds=32700)
    assert compute_work_hours(in_dt, out_sample2) == 9.08

    # 31500 seconds = 8.75 hours -> 8.75
    out_sample7 = in_dt + timedelta(seconds=31500)
    assert compute_work_hours(in_dt, out_sample7) == 8.75


def test_compute_half_day():
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False  # boundary at 4.50
    assert compute_half_day(4.51) is False
    assert compute_half_day(8.0) is False
    assert compute_half_day(None) is False


def test_calculate_working_days():
    # July 2026: July 1 is Wed, July 31 is Fri. Total Mon-Fri = 23 days.
    # Employee joined before July:
    assert calculate_working_days("2026-01-01", "2026-07") == 23

    # Employee joined mid-month on Monday, July 6:
    # Misses July 1 (Wed), July 2 (Thu), July 3 (Fri) = 3 days.
    # 23 - 3 = 20 working days.
    assert calculate_working_days("2026-07-06", "2026-07") == 20

    # Employee joined on Saturday, July 4:
    # Also starts on Monday, July 6 -> 20 working days.
    assert calculate_working_days("2026-07-04", "2026-07") == 20

    # Employee joined next month (August 2026):
    assert calculate_working_days("2026-08-01", "2026-07") == 0
