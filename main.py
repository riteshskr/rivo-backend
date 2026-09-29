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

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rivo-api-final")

app = FastAPI(title="Rivo Taxi - REST Direct", version="7.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
STRINGEE_API_KEY_SID = os.getenv("STRINGEE_API_KEY_SID", "")
STRINGEE_API_KEY_SECRET = os.getenv("STRINGEE_API_KEY_SECRET", "")
STRINGEE_EXPIRE = int(os.getenv("STRINGEE_EXPIRE_SECONDS", "86400"))

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
    payload = {"jti": f"{STRINGEE_API_KEY_SID}-{now}-{user_id}-{uuid.uuid4().hex[:6]}", "iss": STRINGEE_API_KEY_SID, "exp": now + STRINGEE_EXPIRE, "userId": user_id}
    return jwt.encode(payload, STRINGEE_API_KEY_SECRET, algorithm="HS256")

class RideCreate(BaseModel):
    pickup_lat: float; pickup_lng: float; drop_lat: float; drop_lng: float
    pickup_address: str; drop_address: str; vehicle_type: str
    distance: float; fare: float; city: Optional[str] = "Sikar"
    scheduled_time: Optional[str] = None; trip_type: Optional[str] = "ride"

class InitReq(BaseModel):
    device_id: str

class DriverTokenReq(BaseModel):
    ride_id: str; driver_id: str; force_new: Optional[bool] = True

@app.get("/")
def root():
    return {"message": "Rivo API v7 - REST Direct - No supabase lib"}

@app.post("/users/init")
async def init_user(req: InitReq):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/users", headers=supabase_headers(), params={"device_id": f"eq.{req.device_id}", "select": "*"}, timeout=10)
        if r.status_code == 200 and r.json():
            return r.json()[0]
        new_id = str(uuid.uuid4())
        new_user = {"id": new_id, "device_id": req.device_id}
        r2 = await client.post(f"{SUPABASE_URL}/rest/v1/users", json=new_user, headers=supabase_headers(), timeout=10)
        if r2.status_code in [200, 201]:
            return r2.json()[0] if isinstance(r2.json(), list) else new_user
        return new_user

@app.get("/vehicles")
async def get_vehicles(city: Optional[str] = Query(None)):
    # Direct REST - no supabase library
    url = f"{SUPABASE_URL}/rest/v1/vehicles"
    params = {"select": "*", "order": "id.asc"}
    if city:
        params["city"] = f"ilike.%{city}%"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=supabase_headers(), params=params, timeout=15)
        if r.status_code == 200:
            return r.json() # यही आपका असली Bike वाला data होगा
        raise HTTPException(status_code=r.status_code, detail=r.text)

@app.post("/rides")
async def create_ride(payload: RideCreate, user_id: str = Query(...)):
    otp = generate_otp()
    user_token = create_stringee_token(user_id)
    ride_data = {
        "user_id": user_id, "pickup_lat": payload.pickup_lat, "pickup_lng": payload.pickup_lng,
        "drop_lat": payload.drop_lat, "drop_lng": payload.drop_lng,
        "pickup_address": payload.pickup_address, "drop_address": payload.drop_address,
        "vehicle_type": str(payload.vehicle_type).lower(), "distance": payload.distance,
        "fare": payload.fare, "city": payload.city, "otp": otp, "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(), "stringee_token": user_token
    }
    if payload.scheduled_time:
        ride_data["scheduled_time"] = payload.scheduled_time
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{SUPABASE_URL}/rest/v1/rides", json=ride_data, headers=supabase_headers(), timeout=15)
        if r.status_code in [200, 201]:
            return r.json()[0]
        raise HTTPException(status_code=r.status_code, detail=r.text)

@app.get("/rides/{ride_id}")
async def get_ride_by_id(ride_id: str):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/rides", headers=supabase_headers(), params={"id": f"eq.{ride_id}", "select": "*"}, timeout=10)
        if r.status_code == 200 and r.json():
            return r.json()[0]
        raise HTTPException(status_code=404, detail="Ride not found")

@app.get("/rides/ongoing/{user_id}")
async def get_ongoing_ride(user_id: str):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/rides", headers=supabase_headers(), params={"user_id": f"eq.{user_id}", "status": "in.(pending,accepted,started)", "order": "created_at.desc", "limit": "1", "select": "*"}, timeout=10)
        if r.status_code == 200 and r.json():
            return r.json()[0]
    return None

@app.put("/rides/{ride_id}/cancel")
async def cancel_ride(ride_id: str, user_id: str = Query(...)):
    async with httpx.AsyncClient() as client:
        r = await client.patch(f"{SUPABASE_URL}/rest/v1/rides", headers=supabase_headers(), params={"id": f"eq.{ride_id}", "user_id": f"eq.{user_id}"}, json={"status": "cancelled"}, timeout=10)
        if r.status_code not in [200, 201, 204]:
            raise HTTPException(status_code=400, detail=r.text)
        return {"message": "cancelled"}

@app.post("/generate-driver-token")
async def generate_driver_token(req: DriverTokenReq):
    token = create_stringee_token(req.driver_id)
    try:
        async with httpx.AsyncClient() as client:
            await client.patch(f"{SUPABASE_URL}/rest/v1/drivers", headers=supabase_headers(), params={"id": f"eq.{req.driver_id}"}, json={"stringee_token": token}, timeout=10)
    except:
        pass
    return {"access_token": token, "driver_id": req.driver_id}