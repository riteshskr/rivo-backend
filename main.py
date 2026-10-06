import os
import asyncio
from datetime import datetime, timezone
from math import radians, cos, sin, asin, sqrt
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TIMEOUT_MINUTES = 10

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = radians(lat2-lat1)
    dlon = radians(lon2-lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return 2 * R * asin(sqrt(a))

async def archive_and_delete(ride):
    try:
        archive_data = {
            "original_ride_id": ride['id'],
            "user_id": ride.get('user_id'),
            "status": "timeout",
            "fare": ride.get('fare'),
            "pickup_address": ride.get('pickup_address'),
            "drop_address": ride.get('drop_address'),
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "vehicle_type": ride.get('vehicle_type'),
            "trip_type": ride.get('trip_type'),
        }
        supabase.table("rides_archive").insert(archive_data).execute()
        supabase.table("rides").delete().eq("id", ride['id']).execute()
        print(f"✅ Timeout done {ride['id']}")
    except Exception as e:
        print(f"Archive fail {ride['id']}: {e}")
        try:
            supabase.table("rides").update({"status":"timeout"}).eq("id", ride['id']).execute()
        except Exception as e2:
            print(f"Timeout fail {ride['id']}: {e2}")

async def timeout_checker():
    print(f"⏰ Auto Timeout Checker Started - {TIMEOUT_MINUTES} min")
    while True:
        try:
            res = supabase.table("rides").select("*").eq("status","pending").execute()
            pending = res.data or []
            now = datetime.now(timezone.utc)
            for ride in pending:
                created = ride.get('created_at')
                if not created: continue
                try:
                    c_time = datetime.fromisoformat(created.replace('Z','+00:00'))
                except: continue
                age_min = (now - c_time).total_seconds()/60
                if age_min >= TIMEOUT_MINUTES:
                    print(f"⏰ TIMING OUT {ride['id']} - {age_min:.1f}min")
                    await archive_and_delete(ride)
        except Exception as e:
            print(f"Checker error: {e}")
        await asyncio.sleep(30)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(timeout_checker())

@app.get("/")
def home():
    return {"status":"Rivo API Running - Timeout Fixed + Range Feature"}

# ⭐⭐⭐ YOUR REQUIRED FEATURE - /rides/pending/list
@app.get("/rides/pending/list")
async def pending_list(driver_id: str, vehicle_type: str = "", driver_lat: float = 0, driver_lng: float = 0):
    q = supabase.table("rides").select("*").eq("status","pending")
    if vehicle_type:
        q = q.eq("vehicle_type", vehicle_type)
    res = q.execute()
    all_rides = res.data or []

    if driver_lat == 0 and driver_lng == 0:
        return all_rides

    filtered = []
    for r in all_rides:
        try:
            if not r.get('pickup_lat') or not r.get('pickup_lng'):
                continue
            d = haversine(driver_lat, driver_lng, float(r['pickup_lat']), float(r['pickup_lng']))
            r['distance_from_driver'] = round(d, 2)
            r['driver_range'] = 20
            filtered.append(r)
        except:
            continue
    filtered.sort(key=lambda x: x.get('distance_from_driver', 999))
    return filtered

@app.get("/rides/{ride_id}")
def get_ride(ride_id: int):
    res = supabase.table("rides").select("*").eq("id", ride_id).execute()
    if not res.data: raise HTTPException(404, "Ride not found")
    return res.data[0]

@app.get("/drivers/{driver_id}/active-ride")
def active_ride(driver_id: str):
    res = supabase.table("rides").select("*").eq("driver_id", driver_id).in_("status", ["accepted","started"]).execute()
    if res.data and len(res.data)>0:
        return {"active": True, "ride": res.data[0]}
    return {"active": False}

@app.put("/rides/{ride_id}/accept")
def accept_ride(ride_id: int, driver_id: str):
    res = supabase.table("rides").select("*").eq("id", ride_id).eq("status","pending").execute()
    if not res.data: raise HTTPException(400, "Already Taken")
    ride = res.data[0]
    updated = supabase.table("rides").update({"status":"accepted","driver_id":driver_id,"accepted_at":datetime.now(timezone.utc).isoformat()}).eq("id", ride_id).execute()
    return {"ride": updated.data[0] if updated.data else ride}

@app.put("/drivers/{driver_id}/location")
def update_driver_loc(driver_id: str, data: dict):
    lat = data.get("latitude"); lng = data.get("longitude")
    supabase.table("drivers").update({"lat":lat,"lng":lng,"latitude":lat,"longitude":lng}).eq("id", driver_id).execute()
    return {"ok": True}

@app.put("/rides/{ride_id}/complete")
def complete_ride(ride_id: int):
    supabase.table("rides").update({"status":"completed","completed_at":datetime.now(timezone.utc).isoformat()}).eq("id", ride_id).execute()
    return {"ok": True}

@app.post("/rides/{ride_id}/verify-otp")
def verify_otp(ride_id: int, data: dict):
    otp = data.get("otp")
    res = supabase.table("rides").select("*").eq("id", ride_id).execute()
    if not res.data: raise HTTPException(404, "Ride not found")
    if str(res.data[0].get("otp")) == str(otp):
        supabase.table("rides").update({"status":"started","started_at":datetime.now(timezone.utc).isoformat()}).eq("id", ride_id).execute()
        return {"ok": True}
    raise HTTPException(400, "Wrong OTP")

