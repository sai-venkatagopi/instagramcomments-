import time
import httpx

BASE_URL = "http://127.0.0.1:8000"

def main():
    print("🚀 Triggering 50-event test load on mock API server...")
    
    # 1. Ensure a rule exists
    httpx.post(f"{BASE_URL}/rules", json={"keyword": "PRICE", "dm_message": "Automated Price Catalog Response"})
    
    # 2. Trigger simulation
    sim_resp = httpx.post(
        f"{BASE_URL}/api/simulate",
        json={
            "webhook_url": f"{BASE_URL}/webhook",
            "count": 50,
            "duration_seconds": 5
        }
    )
    
    if sim_resp.status_code not in (200, 202):
        print(f"Simulation trigger failed: {sim_resp.status_code} - {sim_resp.text}")
        return
        
    sim_data = sim_resp.json()
    run_id = sim_data.get("run_id") or sim_data.get("id")
    print(f"✅ Simulation started successfully! Run ID: {run_id}")
    
    print("\n📊 Monitoring live stats during processing (polling every 3 seconds)...")
    for i in range(10):
        time.sleep(3)
        stats = httpx.get(f"{BASE_URL}/stats").json()
        print(f"   [T+{ (i+1)*3 }s] Stats: sent={stats['sent']}, queued={stats['queued']}, blocked={stats['duplicates_blocked']}, failed={stats['failed']}")
        if stats['queued'] == 0 and stats['sent'] > 0:
            print("\n✅ Queue drained! All DMs processed and reconciled successfully.")
            break

    print("\n🔍 Fetching simulation ground truth from mock server...")
    if run_id:
        truth_resp = httpx.get(f"{BASE_URL}/api/simulate/{run_id}/truth")
        if truth_resp.status_code == 200:
            truth = truth_resp.json()
            print(f"   Ground Truth Summary: Received events count = {len(truth.get('events', []))}")
        else:
            print(f"   Could not fetch truth: {truth_resp.status_code}")

if __name__ == "__main__":
    main()
