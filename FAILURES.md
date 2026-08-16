# LinkPlease — Failures & System Limitations (`FAILURES.md`)

This document provides an honest, technical analysis of edge cases, race conditions, and scenarios where this implementation can lose a DM, send a duplicate, or report discrepancies in `/stats`.

---

### 1. Process Hard Termination During In-Flight Webhook Processing
- **Condition**: If the host process or server container is unexpectedly terminated (`SIGKILL` / `kill -9` or server power loss) while processing a `POST /webhook` request *after* returning `200 OK` but *before* SQLite commits the `user_rule_dispatches` and `dm_queue` transaction.
- **Impact**: The incoming comment event is lost. Because the mock server already received a `200 OK`, it will not redeliver that event.
- **Mitigation**: Using SQLite WAL mode with immediate synchronous commits mitigates standard crashes, but hard process kills during the millisecond window between HTTP response and DB commit result in lost DMs.

### 2. Network Timeout on `POST /v1/dm/send` Response Path
- **Condition**: Our background worker transmits `POST /v1/dm/send` with an `Idempotency-Key`. The mock server receives the request, queues the DM, but a network drop or TCP reset occurs before the `202 Accepted` response reaches our application.
- **Impact**: Our worker catches a network exception and re-queues the job for retry. On retry, the worker re-sends the request using the identical `Idempotency-Key`. If the remote mock server correctly handles the idempotency key, it returns the existing `dm_id`. However, if the mock server fails to persist or index the `Idempotency-Key` across internal resets, a duplicate DM may be dispatched by the mock server.

### 3. Concurrent Webhook Lock Contention Under Extreme SQLite Lock Saturation
- **Condition**: If two webhook events for the same `user_id` and matching `rule_id` arrive concurrently (e.g. within 5ms of each other), both invoke `database.try_lock_user_rule(user_id, rule_id)`.
- **Impact**: One transaction succeeds and inserts the lock into `user_rule_dispatches`; the second transaction encounters a `sqlite3.IntegrityError` (caught cleanly to increment `duplicates_blocked`). However, under extreme database lock contention (e.g., >500 concurrent connections exceeding SQLite's 30-second `busy_timeout`), a database lock timeout could occur, causing the second webhook thread to crash before recording `duplicates_blocked`, slightly deflating `duplicates_blocked` in `/stats`.

### 4. Remote API Indefinite `404` or State Purge on `GET /v1/dm/{dm_id}`
- **Condition**: A DM is successfully accepted by `POST /v1/dm/send` (`202 Accepted`), and our worker stores the `dm_id` and status `api_accepted`. Later, during status reconciliation, `GET /v1/dm/{dm_id}` returns `404 Not Found` (e.g. if the mock server purges test state or drops the record).
- **Impact**: The DM remains in `api_accepted` state in our local database. Because `/stats` computes `queued` as any DM in `queued`, `sending`, or `api_accepted`, `/stats` will report this DM as `queued` indefinitely, causing `queued` to be inflated relative to the remote server's actual state.

### 5. `comment.deleted` Event Arriving Post-Dispatch
- **Condition**: A user posts a comment matching a rule, and a `comment.deleted` webhook arrives *after* our dispatch worker has already transmitted `POST /v1/dm/send` to the mock API (status `api_accepted`).
- **Impact**: Once `POST /v1/dm/send` returns `202 Accepted`, the mock API controls delivery. Our cancellation check in `record_deleted_comment` can only cancel jobs with status `queued`. It cannot recall a DM that has already been accepted by the mock server. The DM will be delivered to the recipient despite the comment being deleted.
