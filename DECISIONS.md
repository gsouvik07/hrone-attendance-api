# DECISIONS.md

1. **Indexes.**
   - `employees`: `{emp_code: 1}` (unique, enforces customer code uniqueness and fast lookups); `{department: 1, joined_on: 1}` and `{joined_on: 1}` (accelerate department listings and monthly summary headcount matching).
   - `attendance_logs`: `{emp_code: 1, date: 1}` (unique composite natural key; prevents duplicate punch-ins); `{date: -1, emp_code: 1}` (supports `/attendance` sorted pagination); `{date: 1, late_minutes: 1}` (powers late leaderboard filtering); `{emp_code: 1, punch_in: -1}` (rapidly locates candidate punch-ins during punch-out).
   - *Considered and rejected*: A large compound index covering `{emp_code: 1, date: -1, status: 1, work_hours: 1}`. It would inflate memory usage and write overhead with negligible performance improvement over `{emp_code: 1, date: 1}`.

2. **Punch-in race.**
   Both concurrent requests validate employee existence and compute the attendance date. Both then attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` atomically serializes the writes. Exactly one document is inserted and returns HTTP 201; the concurrent operation fails with `DuplicateKeyError`, which our handler intercepts and returns as HTTP 409 Conflict.

3. **Ties.**
   The pipeline ranks employees using `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4) sorted strictly by `total_late_minutes` descending. The `$match: {rank: {$lte: limit}}` is applied after ranking. Both tied employees receive the identical rank; because that rank is `<= limit`, both rows are included, ordered by `emp_code` ascending. Consequently, the result may return more than `limit` rows per the specification.

4. **Headcount.**
   The aggregation begins on the `employees` collection (filtering by `joined_on <= month_end`) instead of `attendance_logs`. It joins records via `$lookup`. Employees with zero logs simply receive an empty array, but still contribute `1` to the department's `$sum: 1` headcount.

5. **One thing you would change if this had to serve 100x the data.**
   At 100x scale (~10 million records), on-the-fly aggregations across collections become resource-intensive. I would implement incremental pre-aggregated rollups (e.g. daily/monthly summary collections updated via MongoDB change streams or async workers on punch/regularization events), enabling analytics endpoints to read precomputed metrics with direct index lookups.
