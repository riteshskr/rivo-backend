import asyncio, httpx
BASE = "https://rivo-api-ezoo.onrender.com"
async def one(client, i):
    try:
        r = await client.post(
            f"{BASE}/rides?user_id=load2_{i}",
            json={"pickup_lat":26.9124,"pickup_lng":75.7873,"drop_lat":26.85,"drop_lng":75.8,"distance":5.0,"fare":60.0,"otp":"1234","vehicle_type":"Mini","trip_type":"ride"},
            timeout=20)
        return f"Ride {i}: {r.status_code} - {r.text[:200]}"
    except Exception as e:
        return f"Ride {i}: FAIL {e}"
async def main():
    async with httpx.AsyncClient() as client:
        res = await asyncio.gather(*[one(client, i) for i in range(10)])
        for line in res:
            print(line)
import asyncio
asyncio.run(main())