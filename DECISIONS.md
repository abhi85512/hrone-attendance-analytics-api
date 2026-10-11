# DECISIONS.md

1. **Indexes.**
   I created a unique index on `employees.emp_code` and a compound unique index on `attendance_logs.(emp_code, date)` to enforce uniqueness and concurrency guarantees. For query performance, I created `(date: -1, emp_code: 1)` on attendance logs for sorted pagination, `(date, emp_code)` for date-range analytics, and `(department, emp_code)` on employees. I considered and rejected a TTL index on attendance logs because labor audit trails and historical records must be retained indefinitely.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `insert_one`. MongoDB's unique compound index on `(emp_code, date)` ensures that only one write succeeds atomically, returning HTTP 201. The concurrent request encounters a `DuplicateKeyError`, which our code intercepts and cleanly converts into an HTTP 409 Conflict response. No duplicate records can exist.

3. **Ties.**
   The late leaderboard uses `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4). When employees tie at the cutoff rank, both tied candidates are returned. Truncating arbitrarily would be unfair and non-deterministic.

4. **Headcount.**
   The department summary pipeline initiates from the `employees` collection (filtered by hire date `joined_on <= date_to`) and performs a `$lookup` against `attendance_logs`. Employees with zero attendance logs are fully retained in the grouping stage, ensuring exact headcount counts.

5. **100x data.**
   At 100x volume, I would shard `attendance_logs` on `emp_code` and use MongoDB Change Streams to incrementally maintain pre-aggregated monthly and department summaries in dedicated collections, eliminating expensive on-demand aggregations.
