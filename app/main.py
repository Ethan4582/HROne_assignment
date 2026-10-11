from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
import os
from typing import Any, Dict, Literal, Optional

from fastapi import FastAPI, HTTPException, Path, Query
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.db import client, db, round_half_up
from app.helpers import (
    ALL_STATUSES,
    GRACE_PERIOD_SECONDS,
    HALF_DAY_HOURS_THRESHOLD,
    IST,
    OVERTIME_MINIMUM_MINUTES,
    PRESENCE_STATUSES,
    attendance_date,
    calculate_working_days,
    compute_half_day,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    datetime_to_epoch_ms,
    epoch_ms_to_datetime,
    format_attendance_record,
)
from app.models import EmployeeCreate, EpochMs, PunchInRequest, PunchOutRequest, RegularizeRequest
from app.pipelines import (
    build_attendance_list_query,
    build_department_summary_pipeline,
    build_department_trend_pipeline,
    build_employee_monthly_pipeline,
    build_late_leaderboard_pipeline,
)

__all__ = [
    "app",
    "db",
    "client",
    "IST",
    "round_half_up",
    "attendance_date",
    "compute_late_minutes",
    "compute_overtime_minutes",
    "compute_work_hours",
    "compute_half_day",
    "calculate_working_days",
    "datetime_to_epoch_ms",
    "epoch_ms_to_datetime",
    "format_attendance_record",
    "EmployeeCreate",
    "PunchInRequest",
    "PunchOutRequest",
    "RegularizeRequest",
    "EpochMs",
    "build_attendance_list_query",
    "build_department_summary_pipeline",
    "build_department_trend_pipeline",
    "build_employee_monthly_pipeline",
    "build_late_leaderboard_pipeline",
]


def create_indexes():
    """Ensure indexes exist on startup."""
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

    punch_in_dt = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
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

    punch_out_dt = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)

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
    if date_from and date_to and date_from > date_to:
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

    attendance_pct = round_half_up((present_days / working_days) * 100, 4) if working_days > 0 else None

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
        avg_wh = round_half_up(row["work_hours_sum"] / row["work_hours_count"], 2) if row["work_hours_count"] > 0 else None
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
        explain_cmd = {
            "find": coll,
            "filter": q,
            "sort": dict(sort_order),
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
