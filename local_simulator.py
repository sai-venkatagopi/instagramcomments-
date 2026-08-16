import time
import random
import hmac
import hashlib
import json
import httpx
import config

def generate_event(event_id, user_id, comment_id, is_match=True, is_duplicate=False):
    keywords = ["PRICE", "price", "how much is this PRICE?", "info please", "random comment"]
    text = random.choice(keywords) if is_match else "Nice photo!"
    
    return {
        "event_id": event_id,
        "event_type": "comment.created",
        "sent_at": "2026-08-16T12:00:00.000Z",
        "data": {
            "comment_id": comment_id,
            "post_id": "post_sim_100",
            "text": text,
            "created_at": "2026-08-16T12:00:00.000Z",
            "from": {
                "user_id": user_id,
                "username": f"user_{user_id}"
            }
        }
    }

def run_simulation(count: int = 10, target_url: str = "http://127.0.0.1:8000/webhook") -> dict:
    """
    Runs a simulation batch of `count` events against `target_url`.
    """
    try:
        base_origin = target_url.rsplit("/webhook", 1)[0] if "/webhook" in target_url else "http://127.0.0.1:8000"
        httpx.post(
            f"{base_origin}/rules",
            json={"keyword": "PRICE", "dm_message": "Here is the price list!"},
            timeout=2.0
        )
    except Exception:
        pass

    events = []
    num_users = max(1, count // 5)
    users = [f"usr_sim_{i}" for i in range(1, num_users + 1)]

    for i in range(count):
        evt_id = f"evt_sim_{random.randint(1, 99999):05d}"
        cmt_id = f"cmt_sim_{random.randint(1, 99999):05d}"
        user_id = random.choice(users)
        
        # 8% chance of duplicate event_id
        if i > 5 and random.random() < 0.08:
            evt_id = events[random.randint(0, i - 1)]["event_id"]
            
        evt = generate_event(evt_id, user_id, cmt_id, is_match=True)
        events.append(evt)

    start_time = time.time()
    success_count = 0
    
    with httpx.Client(timeout=5.0) as client:
        for idx, evt in enumerate(events):
            raw_body = json.dumps(evt).encode("utf-8")
            sig = "sha256=" + hmac.new(config.API_KEY.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
            headers = {
                "Content-Type": "application/json",
                "X-PseudoGram-Signature": sig
            }
            try:
                resp = client.post(target_url, content=raw_body, headers=headers)
                if resp.status_code == 200:
                    success_count += 1
            except Exception as err:
                print(f"Error posting event to {target_url}: {err}")

    duration = time.time() - start_time
    return {
        "status": "success",
        "count": count,
        "dispatched": success_count,
        "duration_seconds": round(duration, 2),
        "message": f"Dispatched {success_count}/{count} simulated Instagram DMs"
    }

def main():
    print("⚡ Starting Local 500-Event Load Test...")
    result = run_simulation(count=500, target_url="http://127.0.0.1:8000/webhook")
    print(f"✅ Result: {result}")

if __name__ == "__main__":
    main()
