# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.**
We created unique indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` to guarantee uniqueness and atomic race-safety. For fast querying and aggregations, we indexed `attendance_logs(date DESC, emp_code ASC)` (supports list sorting and range filtering), `attendance_logs(date, late_minutes)` (backs late leaderboard aggregation), `attendance_logs(emp_code, punch_in DESC)` (accelerates punch-out lookups), and `employees(department, joined_on)` (backs department aggregations and headcount filters). We considered a broad covering index across all log fields but rejected it to avoid write amplification on frequent punch events.

2. **Punch-in race.**
Both simultaneous requests validate input and compute the same attendance date. Both issue `insert_one` to MongoDB concurrently. The database evaluates the unique index on `(emp_code, date)` atomically: exactly one succeeds and returns 201 Created, while the other raises a `DuplicateKeyError` (code 11000). Our application catches this error and returns HTTP 409 Conflict without corrupting state.

3. **Ties.**
The pipeline uses `$rank` in `$setWindowFields` for standard competition ranking (1, 2, 2, 4), and then filters with `{ rank: { $lte: limit } }`. If two employees tie at the cutoff rank, both satisfy `rank <= limit`. Thus, both are returned, resulting in more than `limit` items, ordered by total late minutes descending, then `emp_code` ascending.

4. **Headcount.**
The department summary aggregation pipeline starts on the `employees` collection filtered by `joined_on <= period_end` (each employee contributes 1 to headcount), then performs a `$lookup` on `attendance_logs`. Employees with zero logs produce an empty array; their absence of logs does not omit them from the department grouping or headcount.

5. **One thing you would change** if this had to serve 100x the data.
At 100x volume (~10 million records), real-time cross-collection aggregations would saturate CPU and I/O. I would introduce incremental pre-aggregation using MongoDB Change Streams: punches and regularizations would asynchronously update daily and monthly rollup summary collections, allowing analytics endpoints to perform O(1) indexed lookups.
