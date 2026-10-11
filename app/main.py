import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, BeforeValidator, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# ==============================================================================
# SECTION 1: CONFIG & DATABASE CLIENT
# ==============================================================================
load_dotenv(override=False)

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

# ==============================================================================
# SECTION 2: CONSTANTS & STATUSES
# ==============================================================================
IST = timezone(timedelta(hours=5, minutes=30))
GRACE_PERIOD_SECONDS = 600  # 10 minutes strictly
OVERTIME_MINIMUM_MINUTES = 30
HALF_DAY_HOURS_THRESHOLD = 4.50

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

# ==============================================================================
# SECTION 3: PURE HELPER FUNCTIONS (Business Rules R1 - R8)
# ==============================================================================
def round_half_up(value: Optional[float | Decimal | int], decimals: int) -> Optional[float]:
    """
    R8 Rounding: Round half-up (away from zero, not banker's rounding).
    """
    if value is None:
        return None
    d = Decimal(str(value)) if not isinstance(value, Decimal) else value
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def epoch_ms_to_datetime(ms: int) -> datetime:
    """
    R1: Instants are truncated to whole seconds before any calculation,
    and stored as UTC timezone-aware datetime.
    """
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc).replace(microsecond=0)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """
    Convert a BSON datetime to epoch milliseconds.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Dates and shifts:
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is strictly earlier than shift_end belongs to the
    shift that started the previous day.
    """
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
    """
    R2 Late threshold:
    Grace period is 10 minutes. A punch-in is late only if strictly more than
    10:00 after shift_start. When late, late_minutes = whole minutes (floor)
    elapsed since shift_start. Otherwise 0.
    """
    att_date = date.fromisoformat(att_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    start_dt_ist = datetime.combine(att_date, time(start_h, start_m, 0), tzinfo=IST)

    in_ist = punch_in.astimezone(IST).replace(microsecond=0)
    delta_sec = (in_ist - start_dt_ist).total_seconds()

    if delta_sec > GRACE_PERIOD_SECONDS:
        return int(delta_sec // 60)
    return 0


def compute_overtime_minutes(punch_out: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    """
    R3 Overtime:
    overtime_minutes = whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    att_date = date.fromisoformat(att_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    if (end_h, end_m) <= (start_h, start_m):
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    end_dt_ist = datetime.combine(end_date, time(end_h, end_m, 0), tzinfo=IST)
    out_ist = punch_out.astimezone(IST).replace(microsecond=0)
    delta_sec = (out_ist - end_dt_ist).total_seconds()

    if delta_sec <= 0:
        return 0
    ot_minutes = int(delta_sec // 60)
    return ot_minutes if ot_minutes >= OVERTIME_MINIMUM_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R4 Work hours:
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    in_sec = int(punch_in.timestamp())
    out_sec = int(punch_out.timestamp())
    diff_sec = out_sec - in_sec
    hours_d = Decimal(diff_sec) / Decimal(3600)
    return float(hours_d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 Half days:
    half_day = true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < HALF_DAY_HOURS_THRESHOLD


def calculate_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7 Working days:
    Mon-Fri, no holiday calendar. For an employee who joined mid-month,
    working days are counted from joined_on.
    """
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
        if cur_date.weekday() < 5:  # Mon=0..Fri=4
            working_days += 1
        cur_date += timedelta(days=1)
    return working_days


def format_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Format an internal attendance document into OpenAPI AttendanceRecord.
    Truncates/converts all dates and nested history to epoch ms.
    """
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

# ==============================================================================
# SECTION 4: PYDANTIC MODELS & VALIDATION
# ==============================================================================
def validate_epoch_ms(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Epoch milliseconds must be an integer, not float or string")
    if v < 100_000_000_000 or v > 4_102_444_800_000:
        raise ValueError("Epoch milliseconds out of valid range (100000000000 to 4102444800000)")
    return v

EpochMs = Annotated[int, BeforeValidator(validate_epoch_ms)]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_end")
    @classmethod
    def validate_shifts(cls, v, info):
        if "shift_start" in info.data and v == info.data["shift_start"]:
            raise ValueError("shift_start and shift_end must differ")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid calendar date for joined_on")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

# ==============================================================================
# SECTION 5: STARTUP & IDEMPOTENT INDEX CREATION
# ==============================================================================
def create_indexes():
    """
    Creates all nine indexes idempotently at application startup:
    - employees.emp_code (unique)
    - employees.department
    - attendance_logs.(emp_code, date) (unique)
    - attendance_logs.(date, emp_code) (compound: date desc, emp_code asc)
    - attendance_logs.emp_code
    """
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index("emp_code")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield

app = FastAPI(title="Attendance & Analytics API", lifespan=lifespan)

# ==============================================================================
# SECTION 6: EXPLAIN QUERY / PIPELINE BUILDERS
# ==============================================================================
def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[str, Dict[str, Any], List[Tuple[str, int]], int, int]:
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    sort_order = [("date", DESCENDING), ("emp_code", ASCENDING)]
    skip = (page - 1) * page_size
    return "attendance_logs", q, sort_order, skip, page_size


def build_employee_monthly_pipeline(emp_code: str, month: str) -> Tuple[str, List[Dict[str, Any]]]:
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0
                                ]
                            },
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]
    return "attendance_logs", pipeline


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$logs.date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6]
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1.0
                                ]
                            },
                            0.0
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            0.0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$headcount_set"},
                "present_days": 1,
                "work_hours_sum": 1,
                "work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}}
    ]
    return "employees", pipeline


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$match": {"rank": {"$lte": limit}}
        },
        {
            "$sort": {"total_late_minutes": -1, "emp_code": 1}
        }
    ])
    return "attendance_logs", pipeline


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> Tuple[str, List[Dict[str, Any]]]:
    from_dt = datetime.fromisoformat(from_str).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_str).replace(tzinfo=timezone.utc)
    to_dt_bound = to_dt + timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_bound]
                }
            }
        },
        {
            "$project": {
                "date_dt": 1,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": "$date_dt"}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_day"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "dept_emps"
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "headcount": {"$size": "$dept_emps"},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {"$sort": {"date": 1}}
    ]
    return "attendance_logs", pipeline

# ==============================================================================
# SECTION 7: ENDPOINTS
# ==============================================================================

# --- System ---
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# --- Employees ---
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    created_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": created_at_dt,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee {body.emp_code} already exists")

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(created_at_dt),
    }


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)

    items = []
    for d in cursor:
        items.append({
            "emp_code": d["emp_code"],
            "name": d["name"],
            "email": d["email"],
            "department": d["department"],
            "shift_start": d["shift_start"],
            "shift_end": d["shift_end"],
            "joined_on": d["joined_on"],
            "created_at": datetime_to_epoch_ms(d.get("created_at")),
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --- Attendance ---
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_in_dt = datetime.now(timezone.utc).replace(microsecond=0)

    att_date = attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Closes the employee's most recent record whose punch_in is at or before punched_at
    candidate = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_dt}
        },
        sort=[("punch_in", DESCENDING)]
    )
    if not candidate:
        raise HTTPException(status_code=404, detail="No punch-in found for this employee")

    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    punch_in_dt = candidate["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    delta_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if delta_sec <= 0 or delta_sec > 86400:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    half_d = compute_half_day(work_hrs)
    ot_mins = compute_overtime_minutes(punch_out_dt, emp["shift_start"], emp["shift_end"], candidate["date"])

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": candidate["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "half_day": half_d,
                "overtime_minutes": ot_mins,
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record was punched out by a concurrent request")

    return format_attendance_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to:
        if date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    if status and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")

    _, q, sort_order, skip, limit = build_attendance_list_query(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status=status,
        page=page,
        page_size=page_size
    )

    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(sort_order).skip(skip).limit(limit)

    items = [format_attendance_record(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="punch_in and punch_out cannot be provided when setting ABSENT or LEAVE")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail=f"No attendance record found for {emp_code} on {date}")

    new_status = body.status if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = epoch_ms_to_datetime(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        new_punch_out = epoch_ms_to_datetime(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    if new_status in PRESENCE_STATUSES and new_punch_in is None:
        raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

    if new_punch_in is not None:
        r1_date = attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if r1_date != date:
            raise HTTPException(status_code=422, detail=f"punch_in would change attendance date to {r1_date}, but record date is {date}")

    if new_punch_in is not None and new_punch_out is not None:
        delta_sec = (new_punch_out - new_punch_in).total_seconds()
        if delta_sec <= 0 or delta_sec > 86400:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_half_day = compute_half_day(new_work_hours)
            new_overtime_minutes = compute_overtime_minutes(new_punch_out, emp["shift_start"], emp["shift_end"], date)
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

    old_punch_in = record.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = record.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    changes: Dict[str, Any] = {}
    if record.get("status") != new_status:
        changes["status"] = {"from": record.get("status"), "to": new_status}
    if old_punch_in != new_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if old_punch_out != new_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if record.get("work_hours") != new_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if record.get("late_minutes", 0) != new_late_minutes:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}
    if record.get("overtime_minutes", 0) != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if record.get("half_day", False) != new_half_day:
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    curr_history_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": curr_history_len}
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent modification conflict")

    return format_attendance_record(updated)


# --- Analytics ---
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = calculate_working_days(emp["joined_on"], month)

    _, pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        agg = res[0]
        present_days = round_half_up(agg["present_days"], 2)
        leave_days = agg["leave_days"]
        late_count = agg["late_count"]
        total_late_minutes = agg["total_late_minutes"]
        total_overtime_minutes = agg["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 4)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    _, pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for row in cursor:
        avg_wh = None
        if row["work_hours_count"] > 0:
            avg_wh = round_half_up(row["work_hours_sum"] / row["work_hours_count"], 2)

        items.append({
            "department": row["department"],
            "headcount": row["headcount"],
            "present_days": round_half_up(row["present_days"], 2),
            "avg_work_hours": avg_wh,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def leaderboard_late(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    _, pipeline = build_late_leaderboard_pipeline(month, limit, department)
    cursor = db.attendance_logs.aggregate(pipeline)

    items = []
    for row in cursor:
        items.append({
            "rank": row["rank"],
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    dept_exists = db.employees.count_documents({"department": department})
    if dept_exists == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

    _, pipeline = build_department_trend_pipeline(department, from_date, to_date)
    cursor = db.attendance_logs.aggregate(pipeline)

    items = []
    for row in cursor:
        items.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": round_half_up(row["present_count"], 2),
            "late_count": row["late_count"],
            "attendance_rate": round_half_up(row["attendance_rate"], 4) if row["attendance_rate"] is not None else None,
            "moving_avg_7d": round_half_up(row["moving_avg_7d"], 4) if row["moving_avg_7d"] is not None else None,
        })
    return {"department": department, "items": items}


# --- Admin / Explain ---
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def admin_explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"] = Path(...),
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: int = Query(10),
    page: int = Query(1),
    page_size: int = Query(20),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    target_month = month or "2026-07"
    target_emp = emp_code or "EMP0001"
    target_from = from_date or "2026-07-01"
    target_to = to_date or "2026-07-31"

    if endpoint == "attendance_list":
        coll, q, sort_order, skip, p_size = build_attendance_list_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=status,
            page=page,
            page_size=page_size
        )
        sort_dict = dict(sort_order)
        explain_cmd = {
            "find": coll,
            "filter": q,
            "sort": sort_dict,
            "skip": skip,
            "limit": p_size
        }
        res = db.command("explain", explain_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": coll, "explain": res}

    elif endpoint == "employee_monthly":
        coll, pipeline = build_employee_monthly_pipeline(target_emp, target_month)
        explain_cmd = {
            "aggregate": coll,
            "pipeline": pipeline,
            "cursor": {}
        }
        res = db.command("explain", explain_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": coll, "explain": res}

    elif endpoint == "department_summary":
        coll, pipeline = build_department_summary_pipeline(target_month, department)
        explain_cmd = {
            "aggregate": coll,
            "pipeline": pipeline,
            "cursor": {}
        }
        res = db.command("explain", explain_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": coll, "explain": res}

    elif endpoint == "late_leaderboard":
        coll, pipeline = build_late_leaderboard_pipeline(target_month, limit, department)
        explain_cmd = {
            "aggregate": coll,
            "pipeline": pipeline,
            "cursor": {}
        }
        res = db.command("explain", explain_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": coll, "explain": res}

    elif endpoint == "department_trend":
        dept = department or "Engineering"
        coll, pipeline = build_department_trend_pipeline(dept, target_from, target_to)
        explain_cmd = {
            "aggregate": coll,
            "pipeline": pipeline,
            "cursor": {}
        }
        res = db.command("explain", explain_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": coll, "explain": res}

    raise HTTPException(status_code=400, detail="Unsupported endpoint")
