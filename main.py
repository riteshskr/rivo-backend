import os
import json
import time
import jwt
import uuid
import asyncio
from datetime import datetime
from typing import Optional, Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv

# FCM ke liye
import firebase_admin
from firebase_admin import credentials, messaging

load_dotenv()
app = FastAPI(title="Rivo Taxi API - Final Full Code")

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

# --- Firebase Init ---
try:
    firebase_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if firebase_json:
        cred_dict = json.loads(firebase_json)
        cred = credentials.Certificate(cred_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        print("Firebase Admin Initialized - FCM Ready")
    else:
        print("FIREBASE_CREDENTIALS_JSON missing - FCM nahi chalega")
except Exception as e:
    print(f"Firebase init error: {e}")

# --- WebSocket Manager ---
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
        print(f"Driver WS Connected. Total: {len(self.driver_connections)}")

    def disconnect_driver(self, websocket: WebSocket):
        if websocket in self.driver_connections:
            self.driver_connections.remove(websocket)

    async def broadcast_new_ride(self, ride_data: dict):
        if not self.driver_connections: return
        message = {"type": "new_ride_alert", "title": "🔔 New Ride!", "data": ride_data}
        for ws in list(self.driver_connections):
            try: await ws.send_text(json.dumps(message))
            except: self.driver_connections.remove(ws)

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

class DriverLoginRequest(BaseModel):
    driver_id: Optional[str] = None
    phone: Optional[str] = None
    password: str
    fcm_token: Optional[str] = None
    current_latitude: Optional[float] = None
    current_longitude: Optional[float] = None

class DriverRegisterRequest(BaseModel):
    driver_id: str
    fcm_token: str
    city: str = "Sikar"
    name: Optional[str] = None
    vehicle_type: str = "Bike"

# --- Functions ---
def generate_stringee_token(user_id: str):
    if not STRINGEE_API_KEY_SID or not STRINGEE_API_KEY_SECRET:
        print("Stringee Keys missing")
        return None, None
    try:
        clean_id = user_id.replace("+", "").replace(" ", "_").replace("-", "_").strip()
        now = int(time.time())
        payload = {
            "jti": f"{STRINGEE_API_KEY_SID}-{now}-{clean_id}",
            "iss": STRINGEE_API_KEY_SID,
            "exp": now + 86400,
            "userId": clean_id
        }
        token = jwt.encode(payload, STRINGEE_API_KEY_SECRET, algorithm="HS256")
        return token, clean_id
    except Exception as e:
        print(f"Token gen error: {e}")
        return None, None

def send_fcm_to_drivers(ride_data: dict):
    try:
        vehicle_type = ride_data.get("vehicle_type", "Bike")
        # Aapki drivers table ke hisab se - available + is_online dono true
        res = supabase.table("drivers").select("fcm_token, id").eq("is_online", True).eq("available", True).eq("vehicle_type", vehicle_type).execute()
        tokens = [d['fcm_token'] for d in res.data if d.get('fcm_token') and not d['fcm_token'].startswith('backend_gen_')]

        if not tokens:
            print(f"No real FCM tokens for {vehicle_type}, Ride {ride_data['id']}")
            return

        print(f"Sending FCM to {len(tokens)} drivers for ride {ride_data['id']}")

        message = messaging.MulticastMessage(
            notification=messaging.Notification(
                title=f"🔔 New {vehicle_type} Ride - ₹{ride_data['fare']}",
                body=f"{ride_data.get('pickup_address','New Pickup')} -> {ride_data.get('drop_address','Drop')}"
            ),
            data={
                "type": "new_ride_alert",
                "ride_id": str(ride_data['id']),
                "fare": str(ride_data['fare']),
                "pickup_address": str(ride_data.get('pickup_address','')),
                "drop_address": str(ride_data.get('drop_address','')),
                "click_action": "FLUTTER_NOTIFICATION_CLICK"
            },
            tokens=tokens,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    sound="ride_alert",
                    channel_id="ride_alert_channel",
                    priority="max",
                    visibility="public"
                )
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(aps=messaging.Aps(sound="ride_alert.wav", badge=1))
            )
        )
        response = messaging.send_each_for_multicast(message)
        print(f"FCM Sent: Success {response.success_count}, Fail {response.failure_count}")

    except Exception as e:
        print(f"FCM Error: {e}")

# --- APIs ---

@app.get("/stringee/token")
def get_stringee_token(user_id: str = Query(...)):
    token, clean_id = generate_stringee_token(user_id)
    if not token:
        raise HTTPException(status_code=500, detail="Token nahi bana")
    return {"token": token, "userId": clean_id, "expires_in": 86400}

@app.get("/vehicles")
def get_vehicles():
    try:
        res = supabase.table("vehicles").select("*").order("id").execute()
        return res.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/users/init")
def init_user(payload: UserInitRequest):
    try:
        supabase.table("users").delete().eq("id", payload.device_id).execute()
        new_user = {"id": payload.device_id, "device_id": payload.device_id, "created_at": datetime.now().isoformat(), "last_active": datetime.now().isoformat()}
        res = supabase.table("users").insert(new_user).execute()
        return res.data[0]
    except:
        return {"id": payload.device_id, "device_id": payload.device_id}

@app.post("/drivers/login")
def driver_login(payload: DriverLoginRequest):
    try:
        q = supabase.table("drivers").select("*")
        if payload.driver_id: q = q.eq("id", payload.driver_id)
        elif payload.phone: q = q.eq("phone", payload.phone)
        else: raise HTTPException(status_code=400, detail="driver_id ya phone bhejo")

        res = q.execute()
        if not res.data: raise HTTPException(status_code=404, detail="Driver nahi mila")
        driver = res.data[0]

        if driver.get("password")!= payload.password:
            raise HTTPException(status_code=401, detail="Password galat")

        # FCM Token Backend par banana / save karna
        final_fcm_token = payload.fcm_token or driver.get("fcm_token") or f"backend_gen_{uuid.uuid4().hex[:20]}"

        # Stringee Token Hamesha Backend par
        stringee_token, clean_id = generate_stringee_token(driver["id"])

        update_data = {
            "is_online": True,
            "available": True,
            "fcm_token": final_fcm_token,
            "stringee_token": stringee_token,
            "token_updated_at": datetime.now().isoformat(),
            "last_seen": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }
        if payload.current_latitude: update_data["current_latitude"] = payload.current_latitude
        if payload.current_longitude: update_data["current_longitude"] = payload.current_longitude

        updated = supabase.table("drivers").update(update_data).eq("id", driver["id"]).execute()

        print(f"Driver {driver['id']} LOGIN - Online True, FCM: {final_fcm_token[:20]}...")
        return {
            "success": True,
            "driver": updated.data[0],
            "stringee_token": stringee_token,
            "stringee_user_id": clean_id,
            "fcm_token_saved": final_fcm_token
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Login error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/drivers/logout")
def driver_logout(driver_id: str = Query(...)):
    supabase.table("drivers").update({"is_online": False, "available": False, "updated_at": datetime.now().isoformat()}).eq("id", driver_id).execute()
    return {"success": True, "message": "Offline ho gaye"}

@app.post("/drivers/register")
def register_driver(payload: DriverRegisterRequest):
    data = {"id": payload.driver_id, "fcm_token": payload.fcm_token, "name": payload.name or payload.driver_id, "vehicle_type": payload.vehicle_type, "is_online": True, "available": True}
    res = supabase.table("drivers").upsert(data).execute()
    return {"success": True, "data": res.data[0]}

@app.post("/rides")
async def create_ride(payload: RideCreateRequest, user_id: str = Query(...)):
    try:
        check = supabase.table("users").select("id").eq("id", user_id).execute()
        if not check.data:
            supabase.table("users").insert({"id": user_id, "device_id": user_id}).execute()

        stringee_token, clean_user_id = generate_stringee_token(user_id)
        if not clean_user_id: clean_user_id = user_id.replace("-", "_").strip()

        ride_data = {
            "user_id": user_id,
            "pickup_lat": payload.pickup_lat, "pickup_lng": payload.pickup_lng,
            "drop_lat": payload.drop_lat, "drop_lng": payload.drop_lng,
            "pickup_address": payload.pickup_address, "drop_address": payload.drop_address,
            "vehicle_type": payload.vehicle_type, "distance": payload.distance,
            "fare": payload.fare, "status": "pending", "otp": payload.otp,
            "trip_type": payload.trip_type, "city": "Sikar",
            "created_at": datetime.now().isoformat(),
            "stringee_token": stringee_token, "stringee_user_id": clean_user_id
        }
        if payload.scheduled_time: ride_data["scheduled_time"] = payload.scheduled_time

        res = supabase.table("rides").insert(ride_data).execute()
        new_ride = res.data[0]

        # Alert bhejo
        await manager.broadcast_new_ride(new_ride)
        send_fcm_to_drivers(new_ride)

        print(f"Ride {new_ride['id']} created + Alert sent")
        return new_ride
    except Exception as e:
        print(f"Ride create error: {e}")
        raise e

@app.get("/rides/{ride_id}")
def get_ride(ride_id: int):
    res = supabase.table("rides").select("*").eq("id", ride_id).execute()
    if not res.data: return {"error": "Ride not found"}
    return res.data[0]

@app.get("/rides/ongoing/{user_id}")
def get_ongoing_ride(user_id: str):
    res = supabase.table("rides").select("*").eq("user_id", user_id).in_("status", ["pending", "accepted", "on_the_way"]).order("created_at", desc=True).limit(1).execute()
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
        user_id = ride_res.data[0]['user_id']
        supabase.table("rides").update({"status": "completed"}).eq("id", ride_id).execute()
        supabase.table("users").delete().eq("id", user_id).execute()
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
    return {"status": "Rivo API Final Running", "online_drivers_ws": len(manager.driver_connections)}

