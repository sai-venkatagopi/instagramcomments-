import asyncio
import time
import httpx
from datetime import datetime, timezone, timedelta
from typing import List

import config
import database

class RateLimiter:
    """
    Sliding window rate limiter enforcing max requests within rolling window (e.g. 10 reqs / 60s).
    """
    def __init__(self, max_requests: int = 10, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.timestamps: List[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = time.monotonic()
                # Purge timestamps outside rolling window
                self.timestamps = [t for t in self.timestamps if now - t < self.window_seconds]
                
                if len(self.timestamps) < self.max_requests:
                    self.timestamps.append(now)
                    return
                
                # Wait until oldest timestamp expires out of window
                oldest = self.timestamps[0]
                sleep_duration = self.window_seconds - (now - oldest) + 0.1
                if sleep_duration > 0:
                    await asyncio.sleep(sleep_duration)

rate_limiter = RateLimiter(
    max_requests=config.RATE_LIMIT_MAX_REQUESTS,
    window_seconds=config.RATE_LIMIT_WINDOW
)

async def dispatch_worker_loop():
    """
    Background worker loop processing queued DMs while strictly adhering to rate limits and retry logic.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                # Fetch pending items ready to send
                pending_dms = database.get_pending_dms(limit=5)
                if not pending_dms:
                    await asyncio.sleep(config.WORKER_POLL_INTERVAL)
                    continue

                for dm in pending_dms:
                    queue_id = dm["id"]
                    comment_id = dm["comment_id"]
                    user_id = dm["user_id"]
                    message = dm["message"]
                    attempts = dm["attempts"]

                    # Check if comment was deleted before dispatching
                    if database.is_comment_deleted(comment_id):
                        database.update_dm_status(
                            queue_id,
                            status="duplicates_blocked",
                            last_error="Comment was deleted before dispatch"
                        )
                        database.increment_metric("duplicates_blocked")
                        continue

                    # Acquire rate limiter slot before attempting API call
                    await rate_limiter.acquire()

                    # Mark as sending
                    database.update_dm_status(queue_id, status="sending")

                    headers = {
                        "X-API-Key": config.API_KEY,
                        "Content-Type": "application/json",
                        "Idempotency-Key": f"dm_idempotent_{queue_id}"
                    }
                    payload = {
                        "recipient_user_id": user_id,
                        "message": message,
                        "comment_id": comment_id
                    }

                    try:
                        resp = await client.post(
                            f"{config.MOCK_API_BASE}/v1/dm/send",
                            json=payload,
                            headers=headers
                        )
                        
                        if resp.status_code in (200, 202):
                            data = resp.json()
                            api_dm_id = data.get("dm_id")
                            database.update_dm_status(
                                queue_id,
                                status="api_accepted",
                                dm_id=api_dm_id,
                                attempts=attempts + 1,
                                last_error=None
                            )
                        elif resp.status_code == 429:
                            # Rate limited by remote API
                            retry_after = 60.0
                            raw_retry = resp.headers.get("Retry-After")
                            if raw_retry and raw_retry.isdigit():
                                retry_after = float(raw_retry)
                            
                            next_retry = (datetime.now(timezone.utc) + timedelta(seconds=retry_after)).isoformat()
                            database.update_dm_status(
                                queue_id,
                                status="queued",
                                attempts=attempts + 1,
                                last_error=f"API 429 Rate limited. Retry after {retry_after}s",
                                next_retry_at=next_retry
                            )
                            # Additional sleep to respect server Retry-After
                            await asyncio.sleep(min(retry_after, 5.0))
                        elif resp.status_code >= 500:
                            # Remote internal server error (random ~20% failure)
                            new_attempts = attempts + 1
                            if new_attempts < config.MAX_RETRIES:
                                backoff_sec = 2 ** new_attempts
                                next_retry = (datetime.now(timezone.utc) + timedelta(seconds=backoff_sec)).isoformat()
                                database.update_dm_status(
                                    queue_id,
                                    status="queued",
                                    attempts=new_attempts,
                                    last_error=f"API {resp.status_code} internal error",
                                    next_retry_at=next_retry
                                )
                            else:
                                database.update_dm_status(
                                    queue_id,
                                    status="failed",
                                    attempts=new_attempts,
                                    last_error=f"API {resp.status_code} internal error (max retries reached)"
                                )
                        else:
                            # 400 Malformed or unexpected failure
                            database.update_dm_status(
                                queue_id,
                                status="failed",
                                attempts=attempts + 1,
                                last_error=f"API error {resp.status_code}: {resp.text}"
                            )

                    except Exception as e:
                        new_attempts = attempts + 1
                        if new_attempts < config.MAX_RETRIES:
                            backoff_sec = 2 ** new_attempts
                            next_retry = (datetime.now(timezone.utc) + timedelta(seconds=backoff_sec)).isoformat()
                            database.update_dm_status(
                                queue_id,
                                status="queued",
                                attempts=new_attempts,
                                last_error=f"Network exception: {str(e)}",
                                next_retry_at=next_retry
                            )
                        else:
                            database.update_dm_status(
                                queue_id,
                                status="failed",
                                attempts=new_attempts,
                                last_error=f"Network exception: {str(e)} (max retries reached)"
                            )

            except Exception as outer_err:
                print(f"[Worker Error] Unhandled exception in dispatch worker: {outer_err}")
                await asyncio.sleep(1.0)


async def reconcile_worker_loop():
    """
    Background status reconciler loop checking delivery status of api_accepted DMs.
    Note: GET /v1/dm/{dm_id} does NOT count against API rate limit.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                accepted_dms = database.get_api_accepted_dms(limit=10)
                if not accepted_dms:
                    await asyncio.sleep(config.RECONCILE_POLL_INTERVAL)
                    continue

                for dm in accepted_dms:
                    queue_id = dm["id"]
                    dm_id = dm["dm_id"]
                    attempts = dm["attempts"]

                    headers = {"X-API-Key": config.API_KEY}
                    try:
                        resp = await client.get(
                            f"{config.MOCK_API_BASE}/v1/dm/{dm_id}",
                            headers=headers
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            status = data.get("status")
                            if status == "delivered":
                                database.update_dm_status(queue_id, status="delivered")
                            elif status == "failed":
                                # ~15% accepted DMs end up failed on remote side. Retry if attempts left!
                                if attempts < config.MAX_RETRIES:
                                    database.update_dm_status(
                                        queue_id,
                                        status="queued",
                                        last_error="API delivery status reported failed; re-queued for retry"
                                    )
                                else:
                                    database.update_dm_status(
                                        queue_id,
                                        status="failed",
                                        last_error="API delivery status reported failed (max retries reached)"
                                    )
                            # If status is still 'queued' on mock server, keep waiting in 'api_accepted'
                        elif resp.status_code == 404:
                            # Not found yet, keep waiting
                            pass
                    except Exception as err:
                        print(f"[Reconciler Warning] Error checking DM {dm_id}: {err}")

                await asyncio.sleep(config.RECONCILE_POLL_INTERVAL)

            except Exception as outer_err:
                print(f"[Reconciler Error] Unhandled exception in reconciler worker: {outer_err}")
                await asyncio.sleep(2.0)


async def process_queue_single_pass(limit: int = 10) -> dict:
    """
    Single pass helper to process pending DMs and reconcile statuses for serverless/cron calls.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        # 1. Process pending DMs
        pending_dms = database.get_pending_dms(limit=limit)
        processed = 0
        for dm in pending_dms:
            queue_id = dm["id"]
            comment_id = dm["comment_id"]
            user_id = dm["user_id"]
            message = dm["message"]
            attempts = dm["attempts"]

            if database.is_comment_deleted(comment_id):
                database.update_dm_status(
                    queue_id,
                    status="duplicates_blocked",
                    last_error="Comment deleted before dispatch"
                )
                database.increment_metric("duplicates_blocked")
                continue

            await rate_limiter.acquire()
            database.update_dm_status(queue_id, status="sending")

            headers = {
                "X-API-Key": config.API_KEY,
                "Content-Type": "application/json",
                "Idempotency-Key": f"dm_idempotent_{queue_id}"
            }
            payload = {
                "recipient_user_id": user_id,
                "message": message,
                "comment_id": comment_id
            }

            try:
                resp = await client.post(
                    f"{config.MOCK_API_BASE}/v1/dm/send",
                    json=payload,
                    headers=headers
                )
                if resp.status_code in (200, 202):
                    data = resp.json()
                    database.update_dm_status(
                        queue_id,
                        status="api_accepted",
                        dm_id=data.get("dm_id"),
                        attempts=attempts + 1
                    )
                    processed += 1
                elif resp.status_code == 429:
                    database.update_dm_status(
                        queue_id,
                        status="queued",
                        attempts=attempts + 1,
                        last_error="API 429 Rate limited"
                    )
                else:
                    database.update_dm_status(
                        queue_id,
                        status="failed",
                        attempts=attempts + 1,
                        last_error=f"API {resp.status_code}"
                    )
            except Exception as e:
                database.update_dm_status(
                    queue_id,
                    status="queued",
                    attempts=attempts + 1,
                    last_error=f"Exception {str(e)}"
                )

        # 2. Reconcile accepted DMs
        accepted_dms = database.get_api_accepted_dms(limit=limit)
        reconciled = 0
        for dm in accepted_dms:
            queue_id = dm["id"]
            dm_id = dm["dm_id"]
            headers = {"X-API-Key": config.API_KEY}
            try:
                resp = await client.get(
                    f"{config.MOCK_API_BASE}/v1/dm/{dm_id}",
                    headers=headers
                )
                if resp.status_code == 200:
                    st = resp.json().get("status")
                    if st == "delivered":
                        database.update_dm_status(queue_id, status="delivered")
                        reconciled += 1
                    elif st == "failed":
                        database.update_dm_status(queue_id, status="failed", last_error="API status failed")
                        reconciled += 1
            except Exception:
                pass

        return {"processed": processed, "reconciled": reconciled}

