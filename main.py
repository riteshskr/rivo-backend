import os
import uuid
import secrets
import logging
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
import jwt
import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rivo-api-final")

app = FastAPI(title="Rivo Taxi - Final Fixed with Driver Details", version="5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ENV ---
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

STRINGEE_API_KEY_SID = os.getenv("STRINGEE_API_KEY_SID", "")
STRINGEE_API_KEY_SECRET = os.getenv("STRINGEE_API_KEY_SECRET", "")
STRINGEE_EXPIRE = int(os.getenv("STRINGEE_EXPIRE_SECONDS", "86400"))

print(f"INIT CHECK: SUPABASE_URL={SUPABASE_URL} | KEY_EXISTS={bool(SUPABASE_KEY)}")

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Supabase Client Created Successfully")
    except Exception as e:
        print(f"Supabase Client Failed: {e}")
        supabase = None

def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def generate_otp():
    return str(secrets.randbelow(900000) + 100000)

def create_stringee_token(user_id: str):
    if not STRINGEE_API_KEY_SID or not STRINGEE_API_KEY_SECRET:
        return f"temp_token_{user_id}_{int(time.time())}"
    now = int(time.time())
    payload = {
        "jti": f"{STRINGEE_API_KEY_SID}-{now}-{user_id}-{uuid.uuid4().hex[:6]}",
        "iss": STRINGEE_API_KEY_SID,
        "exp": now + STRINGEE_EXPIRE,
        "userId": user_id
    }
    return jwt.encode(payload, STRINGEE_API_KEY_SECRET, algorithm="HS256")

class RideCreate(BaseModel):
    pickup_lat: float
    pickup_lng: float
    drop_lat: float
    drop_lng: float
    pickup_address: str
    drop_address: str
    vehicle_type: str
    distance: float
    fare: float
    city: Optional[str] = "Sikar"
    scheduled_time: Optional[str] = None
    trip_type: Optional[str] = "ride"

class InitReq(BaseModel):
    device_id: str

class DriverTokenReq(BaseModel):
    ride_id: str
    driver_id: str
    force_new: Optional[bool] = True

@app.get("/")
def root():
    return {"message": "Rivo API v5 - Driver Details Fixed"}

@app.post("/users/init")
async def init_user(req: InitReq):
    if not supabase:
        return {"id": str(uuid.uuid4()), "device_id": req.device_id}
    try:
        res = supabase.table("users").select("*").eq("device_id", req.device_id).execute()
        if res.data:
            return res.data[0]
        new_id = str(uuid.uuid4())
        new_user = {"id": new_id, "device_id": req.device_id}
        res2 = supabase.table("users").insert(new_user).execute()
        return res2.data[0] if res2.data else new_user
    except Exception as e:
        print(f"init_user error: {e}")
        return {"id": str(uuid.uuid4()), "device_id": req.device_id}

# --- MAIN FIX: DIRECT DB SE ---
@app.get("/vehicles")
async def get_vehicles(city: Optional[str] = Query(None)):
    try:
        if not supabase:
            raise HTTPException(status_code=500, detail="Supabase client not initialized - check ENV")

        query = supabase.table("vehicles").select("*").order("id")
        if city:
            query = query.ilike("city", f"%{city}%")

        result = query.execute()
        logger.info(f"VEHICLES FETCHED: {len(result.data)} rows")
        return result.data

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Supabase Error: {str(e)}")

@app.post("/rides")
async def create_ride(payload: RideCreate, user_id: str = Query(...)):
    otp = generate_otp()
    user_token = create_stringee_token(user_id)

    if supabase:
        try:
            check = supabase.table("users").select("id").eq("id", user_id).execute()
            if not check.data:
                supabase.table("users").insert({"id": user_id, "device_id": user_id}).execute()
        except:
            pass

    ride_data = {
        "user_id": user_id,
        "pickup_lat": payload.pickup_lat,
        "pickup_lng": payload.pickup_lng,
        "drop_lat": payload.drop_lat,
        "drop_lng": payload.drop_lng,
        "pickup_address": payload.pickup_address,
        "drop_address": payload.drop_address,
        "vehicle_type": str(payload.vehicle_type).lower(),
        "distance": payload.distance,
        "fare": payload.fare,
        "city": payload.city,
        "otp": otp,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stringee_token": user_token,
        "driver_id": None,
        "driver_name": None,
        "driver_phone": None,
        "vehicle_number": None
    }
    if payload.scheduled_time:
        ride_data["scheduled_time"] = payload.scheduled_time

    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    try:
        res = supabase.table("rides").insert(ride_data).execute()
        return res.data[0]
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/rides/{ride_id}")
async def get_ride_by_id(ride_id: str):
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    res = supabase.table("rides").select("*").eq("id", ride_id).execute()
    if res.data:
        return res.data[0]
    raise HTTPException(status_code=404, detail="Ride not found")

@app.get("/rides/ongoing/{user_id}")
async def get_ongoing_ride(user_id: str):
    if not supabase:
        return None
    res = supabase.table("rides").select("*").eq("user_id", user_id).in_("status", ["pending", "accepted", "started"]).order("created_at", desc=True).limit(1).execute()
    if res.data:
        return res.data[0]
    return None

@app.put("/rides/{ride_id}/cancel")
async def cancel_ride(ride_id: str, user_id: str = Query(...)):
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    res = supabase.table("rides").update({"status": "cancelled"}).eq("id", ride_id).eq("user_id", user_id).execute()
    return {"message": "cancelled"}

@app.post("/generate-driver-token")
async def generate_driver_token(req: DriverTokenReq):
    token = create_stringee_token(req.driver_id)
    if supabase:
        try:
            supabase.table("drivers").update({"stringee_token": token}).eq("id", req.driver_id).execute()
        except Exception as e:
            logger.warning(f"Driver token save failed: {e}")
    return {"access_token": token, "driver_id": req.driver_id}