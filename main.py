import os
import json
import asyncio
from datetime import datetime
from typing import Optional, Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Supabase Client - SERVICE ROLE KEY USE KARO ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") # yahi service_role key hai
# Agar aapke paas SUPABASE_SERVICE_ROLE_KEY naam se hai to vo use karo
if not SUPABASE_KEY:
    SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- WebSocket Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, list[WebSocket]] = {}

    async def connect(self, ride_id: int, websocket: WebSocket):
        await websocket.accept()
        if ride_id not in self.active_connections:
            self.active_connections[ride_id] = []
        self.active_connections[ride_id].append(websocket)

    def disconnect(self, ride_id: int, websocket: WebSocket):
        if ride_id in self.active_connections:
            self.active_connections[ride_id].remove(websocket)

    async def broadcast(self, ride_id: int, message: dict):
        if ride_id in self.active_connections:
            for connection in self.active_connections[ride_id]:
                try:
                    await connection.send_text(json.dumps(message))
                except:
                    pass

manager = ConnectionManager()

# --- Models ---
class UserInitRequest(BaseModel):
    device_id: str

class RideCreateRequest(BaseModel):
    pickup_lat: float
    pickup_lng: float
    drop_lat: float
    drop_lng: float
    pickup_address: Optional[str] = None
    drop_address: Optional[str] = None
    vehicle_type: str = "Mini"
    distance: float
    fare: float
    scheduled_time: Optional[str] = None
    trip_type: str = "ride"
    otp: str

# --- 1. Vehicles ---
@app.get("/vehicles")
def get_vehicles():
    try:
        res = supabase.table("vehicles").select("*").execute()
        return res.data
    except Exception as e:
        # Agar vehicles table nahi hai to default return karo
        return [
            {"id": 1, "name": "Mini", "fare_per_km": 12, "night_fare_per_km": 15, "min_fare": 50, "parcel_per_km": 15, "parcel_night_per_km": 18, "parcel_min_fare": 60, "icon_path": "assets/icons/car.png"},
            {"id": 2, "name": "Sedan", "fare_per_km": 15, "night_fare_per_km": 18, "min_fare": 80, "parcel_per_km": 18, "parcel_night_per_km": 20, "parcel_min_fare": 80, "icon_path": "assets/icons/sedan.png"},
            {"id": 3, "name": "Auto", "fare_per_km": 10, "night_fare_per_km": 12, "min_fare": 40, "parcel_per_km": 12, "parcel_night_per_km": 15, "parcel_min_fare": 50, "icon_path": "assets/icons/auto.png"},
        ]

# --- 2. User Init - Yahi aapka main fix hai ---
@app.post("/users/init")
def init_user(payload: UserInitRequest):
    device_id = payload.device_id
    print(f"Init request for device_id: {device_id}")

    try:
        # Pehle check karo user hai kya
        existing = supabase.table("users").select("*").eq("id", device_id).execute()
        if existing.data and len(existing.data) > 0:
            # last_active update karo
            supabase.table("users").update({"last_active": datetime.now().isoformat()}).eq("id", device_id).execute()
            return existing.data[0]

        # Nahi hai to naya banao
        new_user_data = {
            "id": device_id,
            "device_id": device_id,
            "created_at": datetime.now().isoformat(),
            "last_active": datetime.now().isoformat()
        }
        res = supabase.table("users").insert(new_user_data).execute()
        print(f"User created: {res.data}")
        return res.data[0]
    except Exception as e:
        print(f"User init error: {e}")
        raise e

# --- 3. Create Ride ---
@app.post("/rides")
def create_ride(payload: RideCreateRequest, user_id: str = Query(...)):
    print(f"Creating ride for user_id: {user_id}")
    try:
        # 1. Pehle pakka karo user exist karta hai
        user_check = supabase.table("users").select("id").eq("id", user_id).execute()
        if not user_check.data:
            # Agar user nahi hai to bana do
            supabase.table("users").insert({"id": user_id, "device_id": user_id}).execute()
            print(f"Auto-created missing user: {user_id}")

        # 2. Ride data banao
        ride_data = {
            "user_id": user_id,
            "pickup_lat": payload.pickup_lat,
            "pickup_lng": payload.pickup_lng,
            "drop_lat": payload.drop_lat,
            "drop_lng": payload.drop_lng,
            "pickup_address": payload.pickup_address,
            "drop_address": payload.drop_address,
            "vehicle_type": payload.vehicle_type,
            "distance": payload.distance,
            "fare": payload.fare,
            "status": "pending",
            "otp": payload.otp,
            "trip_type": payload.trip_type,
            "city": "Sikar",
            "created_at": datetime.now().isoformat()
        }
        if payload.scheduled_time:
            ride_data["scheduled_time"] = payload.scheduled_time

        res = supabase.table("rides").insert(ride_data).execute()
        print(f"Ride created: {res.data[0]['id']}")
        return res.data[0]
    except Exception as e:
        print(f"Ride create error: {e}")
        raise e

# --- 4. Get Single Ride ---
@app.get("/rides/{ride_id}")
def get_ride(ride_id: int):
    res = supabase.table("rides").select("*").eq("id", ride_id).execute()
    if not res.data:
        return {"error": "Ride not found"}
    return res.data[0]

# --- 5. Get Ongoing Ride ---
@app.get("/rides/ongoing/{user_id}")
def get_ongoing_ride(user_id: str):
    res = supabase.table("rides").select("*").eq("user_id", user_id).in_("status", ["pending", "accepted", "on_the_way"]).order("created_at", desc=True).limit(1).execute()
    if not res.data:
        return None
    return res.data[0]

# --- 6. Cancel Ride ---
@app.put("/rides/{ride_id}/cancel")
def cancel_ride(ride_id: int, user_id: str = Query(...)):
    res = supabase.table("rides").update({"status": "cancelled"}).eq("id", ride_id).execute()
    return {"success": True, "data": res.data}

# --- 7. WebSocket for Live Tracking ---
@app.websocket("/ws/ride/{ride_id}")
async def websocket_endpoint(websocket: WebSocket, ride_id: int):
    await manager.connect(ride_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Driver se location aayegi to sabko broadcast karo
            try:
                msg = json.loads(data)
                await manager.broadcast(ride_id, msg)
            except:
                await manager.broadcast(ride_id, {"type": "driver_location", "raw": data})
    except WebSocketDisconnect:
        manager.disconnect(ride_id, websocket)