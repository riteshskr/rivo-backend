import os
import json
from datetime import datetime
from typing import Optional, Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Rivo Taxi API - Temp User System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Supabase Client ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
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
            if websocket in self.active_connections[ride_id]:
                self.active_connections[ride_id].remove(websocket)

    async def broadcast(self, ride_id: int, message: dict):
        if ride_id in self.active_connections:
            for connection in list(self.active_connections[ride_id]):
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

# --- 1. Vehicles API ---
@app.get("/vehicles")
def get_vehicles():
    try:
        res = supabase.table("vehicles").select("*").execute()
        if res.data:
            return res.data
    except Exception as e:
        print(f"Vehicles error: {e}")

    return [
        {"id": 1, "name": "Mini", "fare_per_km": 12, "night_fare_per_km": 15, "min_fare": 50, "parcel_per_km": 15, "parcel_night_per_km": 18, "parcel_min_fare": 60, "icon_path": "assets/icons/car.png"},
        {"id": 2, "name": "Sedan", "fare_per_km": 15, "night_fare_per_km": 18, "min_fare": 80, "parcel_per_km": 18, "parcel_night_per_km": 20, "parcel_min_fare": 80, "icon_path": "assets/icons/sedan.png"},
        {"id": 3, "name": "Auto", "fare_per_km": 10, "night_fare_per_km": 12, "min_fare": 40, "parcel_per_km": 12, "parcel_night_per_km": 15, "parcel_min_fare": 50, "icon_path": "assets/icons/auto.png"},
    ]

# --- 2. User Init - Har baar Temp User ---
@app.post("/users/init")
def init_user(payload: UserInitRequest):
    device_id = payload.device_id
    print(f"Init temp user: {device_id}")
    try:
        # Purana user hai to delete karke naya banao - taaki temp rahe
        supabase.table("users").delete().eq("id", device_id).execute()

        new_user = {
            "id": device_id,
            "device_id": device_id,
            "created_at": datetime.now().isoformat(),
            "last_active": datetime.now().isoformat()
        }
        res = supabase.table("users").insert(new_user).execute()
        return res.data[0]
    except Exception as e:
        print(f"User init error: {e}")
        # Agar error bhi aaye to bhi id return kar do taaki ride rukey nahi
        return {"id": device_id, "device_id": device_id}

# --- 3. Create Ride ---
@app.post("/rides")
def create_ride(payload: RideCreateRequest, user_id: str = Query(...)):
    print(f"Creating ride for temp user: {user_id}")
    try:
        # User exist karta hai ya nahi check karo, nahi to banao
        check = supabase.table("users").select("id").eq("id", user_id).execute()
        if not check.data:
            supabase.table("users").insert({"id": user_id, "device_id": user_id}).execute()

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

# --- 6. Cancel Ride - Ride Keep, User Delete ---
@app.put("/rides/{ride_id}/cancel")
def cancel_ride(ride_id: int, user_id: str = Query(...)):
    try:
        # Ride ko sirf cancelled mark karo, delete mat karo
        supabase.table("rides").update({"status": "cancelled"}).eq("id", ride_id).execute()
        # Sirf user delete karo
        supabase.table("users").delete().eq("id", user_id).execute()
        print(f"Cancelled: Ride kept {ride_id}, User deleted {user_id}")
        return {"success": True}
    except Exception as e:
        print(f"Cancel error: {e}")
        return {"success": False, "error": str(e)}

# --- 7. Complete Ride - Ride Keep, User Delete ---
@app.put("/rides/{ride_id}/complete")
def complete_ride(ride_id: int):
    try:
        ride_res = supabase.table("rides").select("user_id").eq("id", ride_id).execute()
        if ride_res.data:
            user_id = ride_res.data[0]['user_id']
            # Ride ko completed mark karo
            supabase.table("rides").update({"status": "completed"}).eq("id", ride_id).execute()
            # User delete karo taaki users table halki rahe
            supabase.table("users").delete().eq("id", user_id).execute()
            print(f"Completed: Ride kept {ride_id}, User deleted {user_id}")
        return {"success": True}
    except Exception as e:
        print(f"Complete error: {e}")
        return {"success": False, "error": str(e)}

# --- 8. WebSocket ---
@app.websocket("/ws/ride/{ride_id}")
async def websocket_endpoint(websocket: WebSocket, ride_id: int):
    await manager.connect(ride_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                await manager.broadcast(ride_id, msg)
            except:
                await manager.broadcast(ride_id, {"type": "message", "data": data})
    except WebSocketDisconnect:
        manager.disconnect(ride_id, websocket)

@app.get("/")
def root():
    return {"status": "Rivo API Running - Temp User Mode - Rides Kept, Users Deleted"}

