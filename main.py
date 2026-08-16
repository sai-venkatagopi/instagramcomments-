import hmac
import hashlib
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
import httpx

from typing import Optional
import config
import database
from worker import dispatch_worker_loop, reconcile_worker_loop, process_queue_single_pass
from local_simulator import run_simulation

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize Database
    database.init_db()
    
    # Start background async workers
    dispatch_task = asyncio.create_task(dispatch_worker_loop())
    reconcile_task = asyncio.create_task(reconcile_worker_loop())
    
    yield
    
    # Shutdown: cancel worker tasks
    dispatch_task.cancel()
    reconcile_task.cancel()

app = FastAPI(title="LinkPlease Instagram Automation", lifespan=lifespan)

# --- Pydantic Models ---
class RuleCreateRequest(BaseModel):
    keyword: str
    dm_message: str

class SimulateRequest(BaseModel):
    webhook_url: Optional[str] = None
    count: int = 10
    duration_seconds: int = 10


# --- NON-NEGOTIABLE API CONTRACT ENDPOINTS ---

@app.post("/webhook")
async def handle_webhook(request: Request):
    """
    POST /webhook
    Receives comment events from PseudoGram API.
    Must return 200 within 5 seconds.
    Signature verification: X-PseudoGram-Signature: sha256=<hex>
    """
    raw_body = await request.body()
    
    # Signature Verification (Part B)
    sig_header = request.headers.get("X-PseudoGram-Signature")
    if sig_header:
        expected_prefix = "sha256="
        provided_sig = sig_header[len(expected_prefix):] if sig_header.startswith(expected_prefix) else sig_header
        computed_sig = hmac.new(
            config.API_KEY.encode("utf-8"),
            raw_body,
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(computed_sig, provided_sig):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_id = payload.get("event_id")
    event_type = payload.get("event_type")
    data = payload.get("data", {})

    if not event_id:
        raise HTTPException(status_code=400, detail="Missing event_id")

    # 1. Event Deduplication check
    is_new_event = database.process_event_id(event_id)
    if not is_new_event:
        # Event is a redelivery (~8% re-delivered). Return 200 instantly without re-processing.
        return JSONResponse(content={"status": "ok", "detail": "duplicate event ignored"}, status_code=200)

    # 2. Handle event types
    if event_type == "comment.deleted":
        comment_id = data.get("comment_id")
        if comment_id:
            database.record_deleted_comment(comment_id)
        return JSONResponse(content={"status": "ok"}, status_code=200)

    elif event_type == "comment.created":
        comment_id = data.get("comment_id")
        text = data.get("text", "")
        from_user = data.get("from", {})
        user_id = from_user.get("user_id")
        username = from_user.get("username", "")

        if not comment_id or not user_id:
            return JSONResponse(content={"status": "ok", "detail": "missing user or comment id"}, status_code=200)

        # Check if comment was already deleted
        if database.is_comment_deleted(comment_id):
            database.increment_metric("duplicates_blocked")
            return JSONResponse(content={"status": "ok", "detail": "comment already deleted"}, status_code=200)

        # Find matching rules
        matching_rules = database.find_matching_rules(text)
        for rule in matching_rules:
            rule_id = rule["rule_id"]
            dm_message = rule["dm_message"]

            # Atomic lock check: 1 DM per user per rule
            acquired = database.try_lock_user_rule(user_id, rule_id)
            if acquired:
                database.enqueue_dm(
                    rule_id=rule_id,
                    user_id=user_id,
                    username=username,
                    comment_id=comment_id,
                    message=dm_message
                )
            else:
                # User already received a DM for this rule! Block duplicate.
                database.increment_metric("duplicates_blocked")

        return JSONResponse(content={"status": "ok"}, status_code=200)

    return JSONResponse(content={"status": "ok"}, status_code=200)


@app.post("/rules", status_code=201)
async def create_rule(req: RuleCreateRequest):
    """
    POST /rules
    Creates a new keyword -> dm_message automation rule.
    """
    if not req.keyword or not req.dm_message:
        raise HTTPException(status_code=400, detail="keyword and dm_message are required")
    
    rule = database.add_rule(req.keyword, req.dm_message)
    return {
        "rule_id": rule["rule_id"],
        "keyword": rule["keyword"],
        "dm_message": rule["dm_message"]
    }


@app.get("/stats")
async def get_stats():
    """
    GET /stats
    Reports live numbers under load.
    """
    return database.get_stats()


# --- AUXILIARY MANAGEMENT ENDPOINTS ---

@app.get("/rules")
async def list_rules():
    return database.get_rules()

@app.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str):
    success = database.delete_rule(rule_id)
    if not success:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"status": "success", "rule_id": rule_id}

@app.get("/api/logs")
async def get_logs(limit: int = 50):
    return database.get_recent_logs(limit=limit)

@app.post("/api/simulate")
async def trigger_simulation(req: Optional[SimulateRequest] = None):
    """
    Triggers simulation test run.
    If webhook_url points to mock API remote host, calls mock API server.
    Otherwise runs local batch simulation directly.
    """
    count = req.count if req and req.count else 10
    webhook_url = req.webhook_url if req else None

    if webhook_url and "pseudogram-api" in webhook_url:
        async with httpx.AsyncClient() as client:
            headers = {
                "X-API-Key": config.API_KEY,
                "Content-Type": "application/json"
            }
            payload = {
                "webhook_url": webhook_url,
                "count": count,
                "duration_seconds": req.duration_seconds if req else 10
            }
            resp = await client.post(
                f"{config.MOCK_API_BASE}/v1/simulate/start",
                json=payload,
                headers=headers
            )
            if resp.status_code in (200, 202):
                return resp.json()
            else:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
    else:
        target = webhook_url or "http://127.0.0.1:8000/webhook"
        res = run_simulation(count=count, target_url=target)
        return {
            "status": "success",
            "message": f"Dispatched {res.get('dispatched', count)} simulated Instagram DMs",
            "data": res
        }

@app.get("/api/cron/process-queue")
async def vercel_cron_process_queue():
    """
    Triggered by Vercel Cron Jobs every minute to process pending DMs in serverless environments.
    """
    cron_res = await process_queue_single_pass(limit=10)
    return {
        "status": "success",
        "message": "Processed pending DM queue via Vercel Cron",
        "cron_result": cron_res
    }

@app.get("/api/simulate/{run_id}/truth")
async def get_simulation_truth(run_id: str):
    async with httpx.AsyncClient() as client:
        headers = {"X-API-Key": config.API_KEY}
        resp = await client.get(
            f"{config.MOCK_API_BASE}/v1/simulate/{run_id}/truth",
            headers=headers
        )
        if resp.status_code == 200:
            return resp.json()
        else:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)


# Serve Static UI
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")
