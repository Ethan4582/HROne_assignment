# Architecture Decisions

### 1. Indexes

I created five targeted indexes at startup:
- `employees.emp_code` (unique): Prevents duplicate IDs and backs single-document lookups.
- `employees.department`: Covers department filters and pipeline stages.
- `attendance_logs.(emp_code, date)` (unique compound): Guarantees one attendance record per employee per calendar day at the database layer.
- `attendance_logs.(date, emp_code)` (compound): Backs `GET /attendance` filtering and sorting without in-memory sort stages.
- `attendance_logs.emp_code`: Powers joins from employees to logs.

I considered an index on `(emp_code, punch_in)` and rejected it. The compound index on `(emp_code, date)` already gives enough selectivity for employee queries. An extra punch-in index adds write overhead during morning check-ins with no real read benefit.

### 2. Punch-in Race

When two punch-in requests arrive for the same employee and day at the exact same moment, both calculate the same attendance date. Both try to insert into `attendance_logs`. MongoDB serializes the writes on the unique `(emp_code, date)` index. One write succeeds and returns 201. The other throws a `DuplicateKeyError`. The route catches that error and returns 409. We do not use check-then-insert logic, so races never slip through.

### 3. Ties

For the late leaderboard, I used MongoDB 6.0's `$setWindowFields` with `$rank` to get standard competition ranking (1, 2, 2, 4). When employees tie at the cutoff rank, the filter `rank <= limit` includes all of them. For example, if two employees tie at rank 10 with a limit of 10, the response returns 11 rows. The final output sorts by `total_late_minutes` descending and `emp_code` ascending so the list order stays deterministic.

### 4. Headcount

The department summary query starts from `employees`, not `attendance_logs`. It filters by `joined_on <= month_end`, looks up logs for that month, and unwinds with `preserveNullAndEmptyArrays: true`. Employees with no logs stay in the stream with empty records. The grouping stage counts unique employee IDs using `$addToSet` and `$size`. This counts every active employee in the headcount, even if they never logged attendance.

### 5. What I Would Change at 100x Scale

At 100x volume (around 10 million attendance logs and 50,000 employees), running multi-stage aggregations with `$lookup` and `$densify` on every analytics call will burn too much CPU and memory. I would move to background rollups. A change stream listener or message queue worker would update daily and monthly summary tables as punches happen. The analytics endpoints would then read small, pre-calculated documents with simple index lookups instead of scanning millions of raw logs.
