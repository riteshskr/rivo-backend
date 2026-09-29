from fastapi import FastAPI, Query, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
import uuid, secrets, os, logging, time
from datetime import datetime, timezone
from dotenv import load_dotenv
import jwt, httpx
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rivo-api-final")
app = FastAPI(title="Rivo Taxi - Final Fixed", version="4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
SUPABASE_URL = os.getenv("SUPABASE_URL","").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY","")
STRINGEE_API_KEY_SID = os.getenv("STRINGEE_API_KEY_SID","")
STRINGEE_API_KEY_SECRET = os.getenv("STRINGEE_API_KEY_SECRET","")
STRINGEE_EXPIRE = int(os.getenv("STRINGEE_EXPIRE_SECONDS","86400"))
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY","")

def supabase_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}
def generate_otp(): return str(secrets.randbelow(900000)+100000)
def create_stringee_token(user_id: str):
    if not STRINGEE_API_KEY_SID or not STRINGEE_API_KEY_SECRET:
        return f"temp_token_{user_id}_{int(time.time())}"
    now=int(time.time())
    payload={"jti": f"{STRINGEE_API_KEY_SID}-{now}-{user_id}-{uuid.uuid4().hex[:6]}", "iss": STRINGEE_API_KEY_SID, "exp": now+STRINGEE_EXPIRE, "userId": user_id}
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

@app.get("/")
def root(): return {"message":"Rivo API Fixed - Enum Removed"}

# ===== NEW ENDPOINT 1 - YEHI MISSING THA =====
@app.post("/users/init")
async def init_user(req: InitReq):
    async with httpx.AsyncClient() as client:
        # Check existing by device_id
        r = await client.get(f"{SUPABASE_URL}/rest/v1/users", headers=supabase_headers(), params={"device_id": f"eq.{req.device_id}", "select":"*"}, timeout=10)
        if r.status_code == 200 and r.json():
            return r.json()[0]
        # Create new
        new_id = str(uuid.uuid4())
        new_user = {"id": new_id, "device_id": req.device_id}
        r2 = await client.post(f"{SUPABASE_URL}/rest/v1/users", json=new_user, headers=supabase_headers(), timeout=10)
        if r2.status_code in [200,201]:
            return r2.json()[0] if isinstance(r2.json(), list) else new_user
        # Agar users table me device_id column nahi hai to sirf id se banao
        new_user2 = {"id": new_id}
        r3 = await client.post(f"{SUPABASE_URL}/rest/v1/users", json=new_user2, headers=supabase_headers(), timeout=10)
        if r3.status_code in [200,201]:
            return r3.json()[0] if isinstance(r3.json(), list) else new_user2
        return {"id": new_id, "device_id": req.device_id}

@app.get("/vehicles")
async def get_vehicles(city: Optional[str] = Query(None)):
    url=f"{SUPABASE_URL}/rest/v1/vehicles"
    params={"select":"*", "order":"id.asc"}
    if city: params["city"]=f"ilike.{city}"
    async with httpx.AsyncClient() as client:
        r=await client.get(url, headers=supabase_headers(), params=params, timeout=10)
        if r.status_code==200: return r.json()
        raise HTTPException(status_code=r.status_code, detail=r.text)

@app.post("/rides")
async def create_ride(payload: RideCreate, user_id: str = Query(...)):
    otp=generate_otp()
    user_token=create_stringee_token(user_id)

    # ===== FIX 23503 - USER NAHI HAI TO BANA DO =====
    async with httpx.AsyncClient() as client:
        check = await client.get(f"{SUPABASE_URL}/rest/v1/users", headers=supabase_headers(), params={"id": f"eq.{user_id}", "select":"id"}, timeout=10)
        if not (check.status_code == 200 and check.json()):
            try:
                await client.post(f"{SUPABASE_URL}/rest/v1/users", json={"id": user_id, "device_id": user_id}, headers=supabase_headers(), timeout=10)
            except:
                pass

    ride_data={
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
        "stringee_token": user_token
    }
    if payload.scheduled_time:
        ride_data["scheduled_time"]=payload.scheduled_time

    logger.info(f"Booking Payload: {ride_data}")
    async with httpx.AsyncClient() as client:
        r=await client.post(f"{SUPABASE_URL}/rest/v1/rides", json=ride_data, headers=supabase_headers(), timeout=15)
        if r.status_code in [200,201]:
            logger.info("Booking Success")
            return r.json()[0]
        logger.error(f"Booking Fail Supabase: {r.text}")
        raise HTTPException(status_code=r.status_code, detail=r.text)

# ===== NEW ENDPOINT 2 - Flutter isko call karta hai =====
@app.get("/rides/{ride_id}")
async def get_ride_by_id(ride_id: int):
    url=f"{SUPABASE_URL}/rest/v1/rides"
    params={"id": f"eq.{ride_id}", "select":"*"}
    async with httpx.AsyncClient() as client:
        r=await client.get(url, headers=supabase_headers(), params=params, timeout=10)
        if r.status_code==200 and r.json():
            return r.json()[0]
        raise HTTPException(status_code=404, detail="Ride not found")

@app.get("/rides/ongoing/{user_id}")
async def get_ongoing_ride(user_id: str):
    url=f"{SUPABASE_URL}/rest/v1/rides"
    params={"user_id": f"eq.{user_id}", "status": "in.(pending,accepted,started)", "order":"created_at.desc", "limit":"1"}
    async with httpx.AsyncClient() as client:
        r=await client.get(url, headers=supabase_headers(), params=params, timeout=10)
        if r.status_code==200 and r.json(): return r.json()[0]
    return None

@app.put("/rides/{ride_id}/cancel")
async def cancel_ride(ride_id: int, user_id: str = Query(...)):
    async with httpx.AsyncClient() as client:
        r=await client.patch(f"{SUPABASE_URL}/rest/v1/rides", headers=supabase_headers(), params={"id": f"eq.{ride_id}", "user_id": f"eq.{user_id}"}, json={"status":"cancelled"}, timeout=10)
        if r.status_code not in [200,201,204]: raise HTTPException(status_code=400, detail=r.text)
        return {"message":"cancelled"}