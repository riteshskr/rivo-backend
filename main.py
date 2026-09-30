import os
import json
import time
import jwt
import uuid
from datetime import datetime
from typing import Optional, Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, messaging

load_dotenv()
app = FastAPI(title="Rivo Taxi API - Token Only On Accept")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

STRINGEE_API_KEY_SID = os.getenv("STRINGEE_API_KEY_SID")
STRINGEE_API_KEY_SECRET = os.getenv("STRINGEE_API_KEY_SECRET")

try:
    firebase_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if firebase_json:
        cred_dict = json.loads(firebase_json)
        cred = credentials.Certificate(cred_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        print("Firebase Ready")
except Exception as e:
    print(f"Firebase error: {e}")

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, list[WebSocket]] = {}
        self.driver_connections: list[WebSocket] = []
    async def connect(self, ride_id: int, websocket: WebSocket):
        await websocket.accept()
        if ride_id not in self.active_connections:
            self.active_connections[ride_id] = []
        self.active_connections[ride_id].append(websocket)
    def disconnect(self, ride_id: int, websocket: WebSocket):
        if ride_id in self.active_connections and websocket in self.active_connections[ride_id]:
            self.active_connections[ride_id].remove(websocket)
    async def broadcast(self, ride_id: int, message: dict):
        if ride_id in self.active_connections:
            for c in list(self.active_connections[ride_id]):
                try: await c.send_text(json.dumps(message))
                except: pass
    async def connect_driver(self, websocket: WebSocket):
        await websocket.accept()
        self.driver_connections.append(websocket)
    def disconnect_driver(self, websocket: WebSocket):
        if websocket in self.driver_connections:
            self.driver_connections.remove(websocket)
    async def broadcast_new_ride(self, ride_data: dict):
        if not self.driver_connections: return
        for ws in list(self.driver_connections):
            try: await ws.send_text(json.dumps({"type": "new_ride_alert", "data": ride_data}))
            except: self.driver_connections.remove(ws)

manager = ConnectionManager()

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
class DriverLoginRequest(BaseModel):
    driver_id: Optional[str] = None
    phone: Optional[str] = None
    password: str
    fcm_token: Optional[str] = None
class TokenRequest(BaseModel):
    driver_id: str
    ride_id: Optional[str] = None

def generate_stringee_token(user_id: str, ride_id: str = ""):
    if not STRINGEE_API_KEY_SID or not STRINGEE_API_KEY_SECRET:
        return None, None
    try:
        clean_id = user_id.replace("+", "").replace(" ", "_").replace("-", "_").strip()
        now = int(time.time())
        unique_jti = f"{STRINGEE_API_KEY_SID}-{now}-{clean_id}-{ride_id}-{uuid.uuid4().hex[:6]}"
        payload = {"jti": unique_jti, "iss": STRINGEE_API_KEY_SID, "exp": now + 3600, "userId": clean_id}
        token = jwt.encode(payload, STRINGEE_API_KEY_SECRET, algorithm="HS256")
        print(f"NEW TOKEN for {clean_id} Ride {ride_id}")
        return token, clean_id
    except Exception as e:
        print(f"Token Error {e}")
        return None, None

# --- 1. LOGIN PAR TOKEN NAHI BANEGA ---
@app.post("/drivers/login")
def driver_login(payload: DriverLoginRequest):
    q = supabase.table("drivers").select("*")
    if payload.driver_id: q = q.eq("id", payload.driver_id)
    elif payload.phone: q = q.eq("phone", payload.phone)
    else: raise HTTPException(status_code=400, detail="driver_id bhejo")

    res = q.execute()
    if not res.data: raise HTTPException(status_code=404, detail="Driver not found")
    driver = res.data[0]
    if driver.get("password")!= payload.password:
        raise HTTPException(status_code=401, detail="Password galat")

    fcm = payload.fcm_token or driver.get("fcm_token") or f"backend_gen_{uuid.uuid4().hex[:8]}"

    # YAHAN TOKEN GENERATE NAHI KAR RAHE - SIRF ONLINE STATUS
    supabase.table("drivers").update({
        "is_online": True,
        "available": True,
        "fcm_token": fcm,
        "last_seen": datetime.now().isoformat()
    }).eq("id", driver["id"]).execute()

    driver_data = supabase.table("drivers").select("*").eq("id", driver["id"]).execute().data[0]
    return {"success": True, "driver": driver_data} # token nahi bhej rahe

@app.get("/stringee/token")
def get_token(user_id: str = Query(...), ride_id: str = Query("")):
    token, clean_id = generate_stringee_token(user_id, ride_id)
    if not token: raise HTTPException(status_code=500, detail="Keys missing")
    return {"token": token, "access_token": token, "userId": clean_id}

# --- 2. ACCEPT PAR HI TOKEN BANEGA AUR RIDE TABLE ME SAVE HOGA ---
@app.put("/rides/{ride_id}/accept")
def accept_ride(ride_id: int, driver_id: str = Query(...)):
    try:
        d_res = supabase.table("drivers").select("id, name, phone").eq("id", driver_id).execute()
        if not d_res.data: raise HTTPException(status_code=404, detail="Driver not found")
        driver = d_res.data[0]

        # HAR RIDE PAR NAYA TOKEN - SIRF YAHAN
        new_token, clean_driver_id = generate_stringee_token(driver_id, str(ride_id))
        if not new_token:
            raise HTTPException(status_code=500, detail="STRINGEE keys missing in env")

        # Ride table me hi save karo
        ride_update = {
            "driver_id": driver["id"],
            "status": "accepted",
            "driver_name": driver.get("name"),
            "driver_phone": driver.get("phone"),
            "driver_stringee_token": new_token,
            "driver_stringee_user_id": clean_driver_id,
            "driver_driver_tocken": new_token, # aapke purane column ke liye
            "accepted_at": datetime.now().isoformat()
        }

        supabase.table("rides").update(ride_update).eq("id", ride_id).eq("status", "pending").execute()

        # Driver table me bhi token update kar do taki call ke liye mile (optional)
        try:
            supabase.table("drivers").update({
                "stringee_token": new_token,
                "stringee_user_id": clean_driver_id
            }).eq("id", driver_id).execute()
        except: pass

        ride_res = supabase.table("rides").select("*").eq("id", ride_id).execute()
        if not ride_res.data: raise HTTPException(status_code=404, detail="Ride not found after accept")

        print(f"Ride {ride_id} Accepted - Token saved in RIDE table only")

        return {
            "success": True,
            "ride": ride_res.data[0],
            "stringee_token": new_token,
            "stringee_user_id": clean_driver_id
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# --- Baaki APIs same ---
@app.post("/users/init")
def init_user(payload: UserInitRequest):
    try:
        supabase.table("users").delete().eq("id", payload.device_id).execute()
        res = supabase.table("users").insert({"id": payload.device_id, "device_id": payload.device_id}).execute()
        return res.data[0]
    except:
        return {"id": payload.device_id}

@app.post("/rides")
async def create_ride(payload: RideCreateRequest, user_id: str = Query(...)):
    token, clean_id = generate_stringee_token(user_id)
    ride_data = {
        "user_id": user_id,
        "pickup_lat": payload.pickup_lat, "pickup_lng": payload.pickup_lng,
        "drop_lat": payload.drop_lat, "drop_lng": payload.drop_lng,
        "pickup_address": payload.pickup_address, "drop_address": payload.drop_address,
        "vehicle_type": payload.vehicle_type, "distance": payload.distance, "fare": payload.fare,
        "status": "pending", "otp": payload.otp, "city": "Sikar",
        "created_at": datetime.now().isoformat(),
        "stringee_token": token, "stringee_user_id": clean_id
    }
    if payload.scheduled_time: ride_data["scheduled_time"] = payload.scheduled_time
    res = supabase.table("rides").insert(ride_data).execute()
    new_ride = res.data[0]
    await manager.broadcast_new_ride(new_ride)
    return new_ride

@app.get("/rides/{ride_id}")
def get_ride(ride_id: int):
    res = supabase.table("rides").select("*").eq("id", ride_id).execute()
    return res.data[0] if res.data else {"error": "not found"}

@app.get("/rides/pending/list")
def pending_list(vehicle_type: Optional[str] = None):
    q = supabase.table("rides").select("*").eq("status", "pending").order("created_at", desc=True)
    if vehicle_type: q = q.eq("vehicle_type", vehicle_type)
    return q.execute().data or []

@app.put("/rides/{ride_id}/complete")
def complete_ride(ride_id: int):
    r = supabase.table("rides").select("user_id").eq("id", ride_id).execute()
    if r.data:
        supabase.table("rides").update({"status": "completed", "completed_at": datetime.now().isoformat()}).eq("id", ride_id).execute()
        supabase.table("users").delete().eq("id", r.data[0]['user_id']).execute()
    return {"success": True}

@app.get("/")
def root():
    return {"status": "Token Only On Accept - Working"}

