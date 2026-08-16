import hmac
import hashlib
import json
import time
import httpx
import config

BASE_URL = "http://127.0.0.1:8000"

def test_stats_initial():
    print("[TEST 1] Checking initial /stats...")
    resp = httpx.get(f"{BASE_URL}/stats")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    data = resp.json()
    print("   Initial stats:", data)
    assert "sent" in data and "failed" in data and "queued" in data and "duplicates_blocked" in data
    print("   ✅ PASS: /stats endpoint structure verified!")

def test_create_rule():
    print("[TEST 2] Testing POST /rules...")
    payload = {"keyword": "PRICE", "dm_message": "Here is the price list: $99"}
    resp = httpx.post(f"{BASE_URL}/rules", json=payload)
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    data = resp.json()
    print("   Created rule response:", data)
    assert data["keyword"] == "PRICE"
    assert "rule_id" in data
    print("   ✅ PASS: POST /rules creates rule with 201 status code!")

def test_webhook_signature():
    print("[TEST 3] Testing webhook signature verification...")
    payload = {
        "event_id": "evt_test_sig_001",
        "event_type": "comment.created",
        "sent_at": "2026-08-16T12:00:00Z",
        "data": {
            "comment_id": "cmt_sig_001",
            "post_id": "post_001",
            "text": "Tell me PRICE please!",
            "created_at": "2026-08-16T12:00:00Z",
            "from": {"user_id": "usr_sig_user", "username": "siguser"}
        }
    }
    raw_body = json.dumps(payload).encode("utf-8")
    valid_sig = "sha256=" + hmac.new(config.API_KEY.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    
    # 1. Valid signature
    resp = httpx.post(f"{BASE_URL}/webhook", content=raw_body, headers={"Content-Type": "application/json", "X-PseudoGram-Signature": valid_sig})
    assert resp.status_code == 200, f"Valid sig expected 200, got {resp.status_code}: {resp.text}"
    print("   Valid signature accepted: 200 OK")

    # 2. Invalid signature
    invalid_sig = "sha256=" + "0" * 64
    resp = httpx.post(f"{BASE_URL}/webhook", content=raw_body, headers={"Content-Type": "application/json", "X-PseudoGram-Signature": invalid_sig})
    assert resp.status_code == 401, f"Invalid sig expected 401, got {resp.status_code}"
    print("   Invalid signature rejected: 401 Unauthorized")
    print("   ✅ PASS: Webhook signature verification functioning perfectly!")

def test_deduplication():
    print("[TEST 4] Testing User & Event Deduplication...")
    user_id = "usr_dedup_test_99"
    
    # First comment
    p1 = {
        "event_id": "evt_dedup_001",
        "event_type": "comment.created",
        "sent_at": "2026-08-16T12:01:00Z",
        "data": {
            "comment_id": "cmt_dedup_001",
            "post_id": "post_001",
            "text": "What is the PRICE?",
            "from": {"user_id": user_id, "username": "deduptester"}
        }
    }
    b1 = json.dumps(p1).encode("utf-8")
    sig1 = "sha256=" + hmac.new(config.API_KEY.encode("utf-8"), b1, hashlib.sha256).hexdigest()
    httpx.post(f"{BASE_URL}/webhook", content=b1, headers={"Content-Type": "application/json", "X-PseudoGram-Signature": sig1})

    # Second comment from same user matching same rule
    p2 = {
        "event_id": "evt_dedup_002",
        "event_type": "comment.created",
        "sent_at": "2026-08-16T12:02:00Z",
        "data": {
            "comment_id": "cmt_dedup_002",
            "post_id": "post_001",
            "text": "PRICE list again please!",
            "from": {"user_id": user_id, "username": "deduptester"}
        }
    }
    b2 = json.dumps(p2).encode("utf-8")
    sig2 = "sha256=" + hmac.new(config.API_KEY.encode("utf-8"), b2, hashlib.sha256).hexdigest()
    httpx.post(f"{BASE_URL}/webhook", content=b2, headers={"Content-Type": "application/json", "X-PseudoGram-Signature": sig2})

    stats = httpx.get(f"{BASE_URL}/stats").json()
    print("   Stats after duplicate user comment:", stats)
    assert stats["duplicates_blocked"] >= 1, "Expected duplicates_blocked to be >= 1"
    print("   ✅ PASS: Deduplication blocked repeated DM for same user & rule!")

def run_all_tests():
    test_stats_initial()
    test_create_rule()
    test_webhook_signature()
    test_deduplication()
    print("\n🎉 ALL AUTOMATED CONTRACT TESTS PASSED PERFECTLY!")

if __name__ == "__main__":
    run_all_tests()
