# DECISIONS.md

### 1. Indexes
We created five targeted indexes:
- `employees.emp_code` (unique): Guarantees primary identity uniqueness and speeds up point lookups and `$lookup` foreign targets.
- `employees.department`: Powers `department_summary` matching and `department_trend` filtering via `IXSCAN`.
- `attendance_logs.(emp_code, date)` (unique compound): Enforces at most one attendance record per employee per day at the database engine level.
- `attendance_logs.(date, emp_code)` (compound): Powers `GET /attendance` filtering and sorting (`date` DESC, `emp_code` ASC) directly from the index tree without in-memory sorting.
- `attendance_logs.emp_code`: Serves pipeline lookups joining attendance to employee records.

*Considered and rejected*: A standalone index on `(emp_code, punch_in)`. Daily attendance is partitioned by `(emp_code, date)`, which already gives high selectivity. An extra index would add write amplification during peak morning punch-in bursts without meaningful read benefit.

### 2. Punch-in Race
When two identical punch-in requests arrive at the exact same instant, both concurrently fetch the employee shift and compute the identical attendance date. Both then attempt `db.attendance_logs.insert_one(doc)`. MongoDB's WiredTiger engine serializes index key insertion on the unique `(emp_code, date)` compound index. Exactly one request succeeds (HTTP 201 Created); the second triggers a `DuplicateKeyError` (code 11000). The FastAPI handler catches this exception and returns HTTP 409 Conflict (`"already punched in for this date"`). No unhandled 500 error or duplicate document can ever occur.

### 3. Ties
The leaderboard computes standard competition ranking (1, 2, 2, 4) using `$setWindowFields` with `$rank`. When two employees tie at the cutoff threshold (e.g., both rank 10 with `limit=10`), both receive rank 10. The pipeline filters using `{"$match": {"rank": {"$lte": limit}}}`, which deliberately returns both tied employees (11 total records). The output is ordered by `total_late_minutes` descending, then `emp_code` ascending for deterministic pagination and display.

### 4. Headcount
The `department_summary` pipeline anchors on the `employees` collection (filtered by `joined_on <= end_date`), not `attendance_logs`. It performs a `$lookup` to `attendance_logs` for that calendar month and unwinds with `preserveNullAndEmptyArrays: true`. Employees with zero attendance records retain a document with `logs: null`. The group stage collects all employee codes with `{"$addToSet": "$emp_code"}` and projects `headcount: {"$size": "$headcount_set"}`. Thus, every hired employee is counted in the department headcount regardless of activity.

### 5. One Thing to Change at 100x Scale
At 100x scale (~10 million attendance records and 50,000 employees), real-time aggregation with cross-collection `$lookup` and `$densify` would introduce unacceptable CPU and memory pressure. I would introduce incremental pre-aggregation using MongoDB change streams or asynchronous event workers into dedicated summary collections (`department_daily_stats` and `employee_monthly_stats`). Analytics queries would then execute O(1) index-covered point reads rather than multi-stage aggregations over raw log streams.
