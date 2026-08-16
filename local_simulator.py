import time
import random
import hmac
import hashlib
import json
import httpx
import config

TARGET_WEBHOOK = "http://127.0.0.1:8000/webhook"

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

def main():
    print("⚡ Starting Local 500-Event Load Test against http://127.0.0.1:8000/webhook...")
    
    # Ensure rule exists
    httpx.post("http://127.0.0.1:8000/rules", json={"keyword": "PRICE", "dm_message": "Here is the price list!"})
    
    events = []
    # Create 50 distinct users, each posting ~10 comments = 500 events total
    users = [f"usr_sim_{i}" for i in range(1, 51)]
    
    for i in range(500):
        evt_id = f"evt_sim_{i+1:04d}"
        cmt_id = f"cmt_sim_{i+1:04d}"
        user_id = random.choice(users)
        
        # 8% chance of repeating an event_id
        if i > 10 and random.random() < 0.08:
            evt_id = f"evt_sim_{random.randint(1, i):04d}"
            
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
            
            resp = client.post(TARGET_WEBHOOK, content=raw_body, headers=headers)
            if resp.status_code == 200:
                success_count += 1
                
            # Pace over ~5-10 seconds
            if idx % 50 == 0:
                time.sleep(0.1)

    duration = time.time() - start_time
    print(f"✅ Dispatched 500 events in {duration:.2f} seconds. Webhook 200 OK responses: {success_count}/500.")
    
    print("\n📊 Monitoring background queue processing and status reconciliation...")
    for _ in range(15):
        time.sleep(2)
        stats = httpx.get("http://127.0.0.1:8000/stats").json()
        print(f"   Stats: sent={stats['sent']}, queued={stats['queued']}, duplicates_blocked={stats['duplicates_blocked']}, failed={stats['failed']}")
        if stats['queued'] == 0:
            print("\n🎉 All queued DMs fully processed and reconciled!")
            break

if __name__ == "__main__":
    main()
