# Attendance & Workforce Analytics Engine

A production-ready attendance processing and workforce analytics service built with **FastAPI** and **MongoDB 6.0+**. Fully conforms to the OpenAPI 3.0.3 specification and implements strict business rules (R1–R10) with sub-second rounding, overnight shift support, atomic concurrency guarantees, and IXSCAN-covered aggregation pipelines.

---

## Features & Implementation

### 1. Robust Core Business Rules
- **R1 Dates & Shifts**: Shift times and attendance dates are in Indian Standard Time (IST, UTC+05:30). Sub-second timestamps are truncated to whole seconds. Overnight shifts (`shift_end <= shift_start`, e.g. 22:00–06:00) that punch in before `shift_end` are attributed to the previous calendar day's shift.
- **R2 Late Grace**: Strict 10:00 grace threshold. Punches within 10:00 (<= 600s) have 0 late minutes; punches strictly > 600s compute floor minutes from `shift_start`.
- **R3 Overtime**: Overtime requires a minimum of 30 elapsed minutes after `shift_end`. Overnight shifts correctly adjust `shift_end` to the following calendar day.
- **R4 & R8 Half-Up Rounding**: Implements `Decimal` `ROUND_HALF_UP` (away from zero) for work hours, attendance percentages, and moving averages, eliminating Python banker's rounding discrepancies.
- **R5 Half-Day**: Work hours strictly less than 4.50 are marked as `half_day: true` (counting as 0.5 present days).
- **R7 Working Days**: Mon–Fri working calendar with automatic proration from `joined_on` for mid-month hires.
- **R9 Concurrency**: Atomic compound unique indexes `(emp_code, date)` and atomic `find_one_and_update` prevent double-punching, race conditions, and phantom records.

### 2. High-Performance MongoDB Aggregations (No Python Loops)
- **Employee Monthly Summary**: Single-stage aggregation on `attendance_logs` using index scan `(emp_code, date)` computing working days, presence, leave, late, and overtime totals.
- **Department Summary**: Anchors on `employees` with `$lookup` and `preserveNullAndEmptyArrays: true` to ensure zero-log employees are counted in department headcount.
- **Late Leaderboard**: Computes standard competition ranking (1, 2, 2, 4) using `$setWindowFields` and `$rank`, handling ties gracefully.
- **Department Trend**: Calendar gap-filling via `$densify` with explicit datetime bounds, dynamic headcount lookup per day, and 7-day trailing moving average via `$setWindowFields`.
- **Explain Endpoint (`/admin/explain/{endpoint}`)**: Returns execution plans verified to use `IXSCAN` and eliminate `COLLSCAN` across all 5 primary query paths.

---

## Tech Stack & Requirements

- **Python**: 3.12+
- **FastAPI**: Modern asynchronous API framework
- **Pydantic**: Strict data validation and schema serialization
- **MongoDB**: 6.0+ (utilizes `$densify`, `$setWindowFields`, `$dateFromString`, and `$dateAdd`)
- **PyMongo**: Thread-safe driver handling connection pooling and explain metrics

---

## Quickstart

### Option A: Local Python & MongoDB

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment** (Optional, defaults provided):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. **Seed Sample Data**:
   ```bash
   python sample_seed.py
   ```

4. **Start the API Server**:
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```
   Interactive OpenAPI docs available at: `http://localhost:8000/docs`

### Option B: Docker Compose

```bash
docker-compose up -d
```
Starts MongoDB 6.0 on port 27017 and FastAPI service on port 8000.

---

## Running the Test Suite

Execute all pure helper unit tests and API integration tests:

```bash
python -m pytest tests/ -v
```

---

## API Endpoints Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Verifies service and MongoDB database connectivity (`ping`) |
| `POST` | `/employees` | Register new employee (unique `emp_code`, shift validation) |
| `GET` | `/employees` | List employees with pagination and department filtering |
| `POST` | `/attendance/punch-in` | Punch in employee (R1 IST date, R2 late minutes, 409 on duplicate) |
| `POST` | `/attendance/punch-out` | Close attendance record (R3 overtime, R4 work hours, R5 half-day) |
| `GET` | `/attendance` | List attendance logs with multi-field filtering and sorting |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize attendance record with audit history trail |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee attendance summary and percentage |
| `GET` | `/analytics/departments/summary` | Department-level aggregation and average work hours |
| `GET` | `/analytics/leaderboard/late` | Competition ranking leaderboard of late employees |
| `GET` | `/analytics/departments/{department}/trend` | Daily trend with gap-filling (`$densify`) and 7-day moving average |
| `GET` | `/admin/explain/{endpoint}` | Execution plan analysis verifying `IXSCAN` and zero `COLLSCAN` |

---

## Indexes & Performance Guarantees

All indexes are created idempotently at application startup via the FastAPI lifespan handler:

1. `employees.emp_code` (Unique): Point lookups and `$lookup` target.
2. `employees.department`: Powers department summary queries.
3. `attendance_logs.(emp_code, date)` (Unique Compound): Concurrency protection for daily attendance.
4. `attendance_logs.(date, emp_code)` (Compound): Range scans, sorting, and pagination.
5. `attendance_logs.emp_code`: Foreign key join acceleration.
