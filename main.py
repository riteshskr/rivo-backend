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
app = FastAPI(title="Rivo Taxi API - Final Fixed")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
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
        if ride_id not in self.active_connections: self.active_connections[ride_id] = []
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
        message = {"type": "new_ride_alert", "title": "New Ride!", "data": ride_data}
        for ws in list(self.driver_connections):
            try: await ws.send_text(json.dumps(message))
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
    current_latitude: Optional[float] = None
    current_longitude: Optional[float] = None
class TokenRequest(BaseModel):
    driver_id: str
    ride_id: Optional[str] = None

# HAR RIDE PAR NAYA TOKEN
def generate_stringee_token(user_id: str, ride_id: str = ""):
    if not STRINGEE_API_KEY_SID or not STRINGEE_API_KEY_SECRET:
        print("Stringee Keys missing")
        return None, None
    try:
        clean_id = user_id.replace("+", "").replace(" ", "_").replace("-", "_").strip()
        now = int(time.time())
        unique_jti = f"{STRINGEE_API_KEY_SID}-{now}-{clean_id}-{ride_id}-{uuid.uuid4().hex[:8]}"
        payload = {"jti": unique_jti, "iss": STRINGEE_API_KEY_SID, "exp": now + 3600, "userId": clean_id}
        token = jwt.encode(payload, STRINGEE_API_KEY_SECRET, algorithm="HS256")
        print(f"NEW TOKEN for {clean_id} Ride {ride_id}")
        return token, clean_id
    except Exception as e:
        print(f"Token error: {e}")
        return None, None

def send_fcm_to_drivers(ride_data: dict):
    try:
        vehicle_type = ride_data.get("vehicle_type", "Bike")
        res = supabase.table("drivers").select("fcm_token").eq("is_online", True).eq("available", True).eq("vehicle_type", vehicle_type).execute()
        tokens = [d['fcm_token'] for d in res.data if d.get('fcm_token') and not d['fcm_token'].startswith('backend_gen_')]
        if not tokens: return
        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=f"New {vehicle_type} Ride - {ride_data['fare']}", body=f"{ride_data.get('pickup_address','')} -> {ride_data.get('drop_address','')}"),
            data={"type": "new_ride_alert", "ride_id": str(ride_data['id'])},
            tokens=tokens,
            android=messaging.AndroidConfig(priority="high")
        )
        messaging.send_each_for_multicast(message)
    except Exception as e:
        print(f"FCM Error: {e}")

@app.get("/stringee/token")
def get_stringee_token(user_id: str = Query(...), ride_id: str = Query("")):
    token, clean_id = generate_stringee_token(user_id, ride_id)
    if not token: raise HTTPException(status_code=500, detail="Token fail")
    return {"token": token, "access_token": token, "userId": clean_id}

@app.post("/stringee/token/generate")
def generate_token_post(payload: TokenRequest):
    token, clean_id = generate_stringee_token(payload.driver_id, payload.ride_id or "")
    if not token: raise HTTPException(status_code=500, detail="Keys missing")
    supabase.table("drivers").update({"stringee_token": token, "token_updated_at": datetime.now().isoformat()}).eq("id", payload.driver_id).execute()
    return {"access_token": token, "token": token, "userId": clean_id}

@app.get("/vehicles")
def get_vehicles():
    res = supabase.table("vehicles").select("*").order("id").execute()
    return res.data or []

@app.post("/users/init")
def init_user(payload: UserInitRequest):
    try:
        supabase.table("users").delete().eq("id", payload.device_id).execute()
        new_user = {"id": payload.device_id, "device_id": payload.device_id, "created_at": datetime.now().isoformat()}
        res = supabase.table("users").insert(new_user).execute()
        return res.data[0]
    except:
        return {"id": payload.device_id}

@app.post("/drivers/login")
def driver_login(payload: DriverLoginRequest):
    q = supabase.table("drivers").select("*")
    if payload.driver_id: q = q.eq("id", payload.driver_id)
    elif payload.phone: q = q.eq("phone", payload.phone)
    else: raise HTTPException(status_code=400, detail="driver_id bhejo")
    res = q.execute()
    if not res.data: raise HTTPException(status_code=404, detail="Driver nahi mila")
    driver = res.data[0]
    if driver.get("password")!= payload.password:
        raise HTTPException(status_code=401, detail="Password galat")
    final_fcm = payload.fcm_token or driver.get("fcm_token") or f"backend_gen_{uuid.uuid4().hex[:8]}"
    token, clean_id = generate_stringee_token(driver["id"])
    update_data = {"is_online": True, "available": True, "fcm_token": final_fcm, "stringee_token": token, "token_updated_at": datetime.now().isoformat(), "last_seen": datetime.now().isoformat()}
    updated = supabase.table("drivers").update(update_data).eq("id", driver["id"]).execute()
    # FIX: select hata diya, alag se fetch karenge
    driver_data = supabase.table("drivers").select("*").eq("id", driver["id"]).execute().data[0]
    return {"success": True, "driver": driver_data, "stringee_token": token, "stringee_user_id": clean_id}

@app.put("/rides/{ride_id}/accept")
def accept_ride(ride_id: int, driver_id: str = Query(...)):
    try:
        driver_res = supabase.table("drivers").select("id, name, phone, vehicle_number").eq("id", driver_id).execute()
        if not driver_res.data: raise HTTPException(status_code=404, detail="Driver not found")
        driver = driver_res.data[0]

        ride_update = {
            "driver_id": driver["id"],
            "status": "accepted",
            "driver_name": driver.get("name"),
            "driver_phone": driver.get("phone"),
            "accepted_at": datetime.now().isoformat()
        }

        # FIX:.select() hata diya
        update_res = supabase.table("rides").update(ride_update).eq("id", ride_id).eq("status", "pending").execute()

        if not update_res.data or len(update_res.data) == 0:
            # Agar update.data empty hai to check karo kya ride already accept hai
            check = supabase.table("rides").select("*").eq("id", ride_id).execute()
            if check.data and check.data[0].get("status")!= "pending":
                raise HTTPException(status_code=400, detail="Ride already taken")
            # Supabase purane version me update ke baad data nahi deta, isliye alag fetch
            pass

        updated_ride_res = supabase.table("rides").select("*").eq("id", ride_id).execute()
        if not updated_ride_res.data: raise HTTPException(status_code=404, detail="Ride not found")
        updated_ride = updated_ride_res.data[0]

        new_token, clean_id = generate_stringee_token(driver_id, str(ride_id))
        if new_token:
            supabase.table("drivers").update({"stringee_token": new_token, "token_updated_at": datetime.now().isoformat()}).eq("id", driver_id).execute()

        return {"success": True, "ride": updated_ride, "stringee_token": new_token, "stringee_user_id": clean_id}
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Accept error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/drivers/logout")
def driver_logout(driver_id: str = Query(...)):
    supabase.table("drivers").update({"is_online": False, "available": False}).eq("id", driver_id).execute()
    return {"success": True}

@app.post("/rides")
async def create_ride(payload: RideCreateRequest, user_id: str = Query(...)):
    check = supabase.table("users").select("id").eq("id", user_id).execute()
    if not check.data:
        supabase.table("users").insert({"id": user_id, "device_id": user_id}).execute()
    token, clean_id = generate_stringee_token(user_id)
    ride_data = {
        "user_id": user_id, "pickup_lat": payload.pickup_lat, "pickup_lng": payload.pickup_lng,
        "drop_lat": payload.drop_lat, "drop_lng": payload.drop_lng,
        "pickup_address": payload.pickup_address, "drop_address": payload.drop_address,
        "vehicle_type": payload.vehicle_type, "distance": payload.distance, "fare": payload.fare,
        "status": "pending", "otp": payload.otp, "trip_type": payload.trip_type, "city": "Sikar",
        "created_at": datetime.now().isoformat(), "stringee_token": token, "stringee_user_id": clean_id
    }
    if payload.scheduled_time: ride_data["scheduled_time"] = payload.scheduled_time
    res = supabase.table("rides").insert(ride_data).execute()
    new_ride = res.data[0]
    await manager.broadcast_new_ride(new_ride)
    send_fcm_to_drivers(new_ride)
    return new_ride

@app.get("/rides/{ride_id}")
def get_ride(ride_id: int):
    res = supabase.table("rides").select("*").eq("id", ride_id).execute()
    if not res.data: return {"error": "Ride not found"}
    return res.data[0]

@app.get("/rides/ongoing/{user_id}")
def get_ongoing_ride(user_id: str):
    res = supabase.table("rides").select("*").eq("user_id", user_id).in_("status", ["pending", "accepted", "started"]).order("created_at", desc=True).limit(1).execute()
    return res.data[0] if res.data else None

@app.get("/rides/pending/list")
def get_pending_rides(vehicle_type: Optional[str] = None):
    q = supabase.table("rides").select("*").eq("status", "pending").order("created_at", desc=True)
    if vehicle_type: q = q.eq("vehicle_type", vehicle_type)
    res = q.execute()
    return res.data or []

@app.put("/rides/{ride_id}/cancel")
def cancel_ride(ride_id: int, user_id: str = Query(...)):
    supabase.table("rides").update({"status": "cancelled"}).eq("id", ride_id).execute()
    supabase.table("users").delete().eq("id", user_id).execute()
    return {"success": True}

@app.put("/rides/{ride_id}/complete")
def complete_ride(ride_id: int):
    ride_res = supabase.table("rides").select("user_id").eq("id", ride_id).execute()
    if ride_res.data:
        supabase.table("rides").update({"status": "completed", "completed_at": datetime.now().isoformat()}).eq("id", ride_id).execute()
        supabase.table("users").delete().eq("id", ride_res.data[0]['user_id']).execute()
    return {"success": True}

@app.websocket("/ws/ride/{ride_id}")
async def ride_ws(websocket: WebSocket, ride_id: int):
    await manager.connect(ride_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try: await manager.broadcast(ride_id, json.loads(data))
            except: await manager.broadcast(ride_id, {"type": "message", "data": data})
    except WebSocketDisconnect:
        manager.disconnect(ride_id, websocket)

@app.websocket("/ws/drivers")
async def driver_ws(websocket: WebSocket, city: str = Query("Sikar")):
    await manager.connect_driver(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "connected", "city": city}))
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_driver(websocket)

@app.get("/")
def root():
    return {"status": "Rivo API Final Fixed - No Select Error", "online": len(manager.driver_connections)}

