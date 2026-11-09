import asyncio, httpx, time
BASE = "https://rivo-api-ezoo.onrender.com"
async def create_ride(client, i):
    try:
        r = await client.post(
            f"{BASE}/rides?user_id=load_user_{i}",
            json={
                "pickup_lat": 26.9124, "pickup_lng": 75.7873,
                "drop_lat": 26.8500, "drop_lng": 75.8000,
                "distance": 5.0, "fare": 60.0,
                "otp": "1234", "vehicle_type": "Mini", "trip_type": "ride"
            }, timeout=20)
        return 1 if r.status_code == 200 else 0
    except:
        return 0
async def pending_list(client):
    try:
        r = await client.get(f"{BASE}/rides/pending/list?driver_id=car1", timeout=20)
        return 1 if r.status_code == 200 else 0
    except:
        return 0
async def main():
    async with httpx.AsyncClient() as client:
        print("--- TEST 1: 20 Ride Create ---")
        start = time.time()
        res = await asyncio.gather(*[create_ride(client, i) for i in range(20)])
        print(f"OK: {sum(res)}/20 | Time: {time.time()-start:.1f} sec")
        print("\n--- TEST 2: 20 Driver Pending List ---")
        start = time.time()
        res2 = await asyncio.gather(*[pending_list(client) for _ in range(20)])
        print(f"OK: {sum(res2)}/20 | Time: {time.time()-start:.1f} sec")
asyncio.run(main())