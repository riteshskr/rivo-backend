import os, json, time, jwt, uuid
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
app = FastAPI(title="Taxi API - FCM High Priority Fixed")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

STRINGEE_API_KEY_SID = os.getenv("STRINGEE_API_KEY_SID")
STRINGEE_API_KEY_SECRET = os.getenv("STRINGEE_API_KEY_SECRET")

# Firebase Init
try:
    fj = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if fj and not firebase_admin._apps:
        cred_dict = json.loads(fj)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        print("Firebase OK - FCM Ready")
except Exception as e:
    print(f"Firebase Error (Ignored): {e}")

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, list[WebSocket]] = {}
        self.driver_connections: list[WebSocket] = []
    async def connect(self, ride_id: int, ws: WebSocket):
        await ws.accept()
        if ride_id not in self.active_connections:
            self.active_connections[ride_id]=[]
        self.active_connections[ride_id].append(ws)
    def disconnect(self, ride_id: int, ws: WebSocket):
        if ride_id in self.active_connections and ws in self.active_connections[ride_id]:
            self.active_connections[ride_id].remove(ws)
    async def broadcast(self, ride_id: int, msg: dict):
        if ride_id in self.active_connections:
            for c in list(self.active_connections[ride_id]):
                try: await c.send_text(json.dumps(msg))
                except: pass
    async def connect_driver(self, ws: WebSocket):
        await ws.accept()
        self.driver_connections.append(ws)
        print(f"Driver WS Connected: Total {len(self.driver_connections)}")
    def disconnect_driver(self, ws: WebSocket):
        if ws in self.driver_connections:
            self.driver_connections.remove(ws)
    async def broadcast_new_ride(self, ride_data: dict):
        print(f"Broadcasting new ride to {len(self.driver_connections)} drivers via WS")
        for ws in list(self.driver_connections):
            try:
                await ws.send_text(json.dumps({"type":"new_ride_alert","data":ride_data}))
            except:
                try: self.driver_connections.remove(ws)
                except: pass

manager = ConnectionManager()

# ===== FCM HIGH PRIORITY FUNCTION =====
def send_fcm_high_priority_to_drivers(ride_data: dict, vehicle_type: str = ""):
    try:
        if not firebase_admin._apps:
            print("Firebase not initialized, skipping FCM")
            return

        # सभी Online Drivers के Token निकालो
        q = supabase.table("drivers").select("fcm_token,is_online").eq("is_online", True).neq("fcm_token", None).neq("fcm_token", "")
        drivers = q.execute().data or []

        tokens = [d['fcm_token'] for d in drivers if d.get('fcm_token')]
        # Duplicate हटाओ
        tokens = list(set(tokens))

        if not tokens:
            print("No FCM tokens found for drivers")
            return

        print(f"Sending FCM High Priority to {len(tokens)} drivers")

        # High Priority Message with Custom Sound alert.mp3
        message = messaging.MulticastMessage(
            android=messaging.AndroidConfig(
                priority='high',
                notification=messaging.AndroidNotification(
                    channel_id='ride_channel_v3',
                    sound='alert',
                    priority='high',
                    visibility='public',
                    default_vibrate_timings=False,
                ),
            ),
            notification=messaging.Notification(
                title='🔔 नई Ride आई है!',
                body=f"{ride_data.get('pickup_address','New Ride')} -> {ride_data.get('drop_address','')} | ₹{ride_data.get('fare','')}"
            ),
            data={
                'type': 'new_ride_alert',
                'ride_id': str(ride_data.get('id','')),
                'pickup': str(ride_data.get('pickup_address','')),
                'fare': str(ride_data.get('fare','')),
                'click_action': 'FLUTTER_NOTIFICATION_CLICK'
            },
            tokens=tokens
        )
        response = messaging.send_multicast(message)
        print(f"FCM Sent: {response.success_count} success, {response.failure_count} fail")
        if response.failure_count > 0:
            for idx, resp in enumerate(response.responses):
                if not resp.success:
                    print(f"Failed token {tokens[idx][:20]}... Error: {resp.exception}")
    except Exception as e:
        print(f"FCM Error: {e}")

class UserInitRequest(BaseModel): device_id: str
class RideCreateRequest(BaseModel):
    pickup_lat: float; pickup_lng: float; drop_lat: float; drop_lng: float
    pickup_address: Optional[str]=None; drop_address: Optional[str]=None
    vehicle_type: str="Mini"; distance: float; fare: float
    scheduled_time: Optional[str]=None; trip_type: str="ride"; otp: str

class DriverLoginRequest(BaseModel):
    driver_id: Optional[str]=None; phone: Optional[str]=None; password: str
    fcm_token: Optional[str]=None

class OtpVerifyRequest(BaseModel):
    otp: str

def generate_stringee_token(user_id: str, ride_id: str=""):
    if not STRINGEE_API_KEY_SID or not STRINGEE_API_KEY_SECRET:
        print("STRINGEE Keys Missing!")
        return None, None
    clean_id = str(user_id).replace("+","").replace(" ","_").replace("-","_").strip()
    now = int(time.time())
    jti = f"{STRINGEE_API_KEY_SID}-{now}-{clean_id}-{ride_id}-{uuid.uuid4().hex[:6]}"
    payload = {"jti": jti, "iss": STRINGEE_API_KEY_SID, "exp": now+86400, "userId": clean_id}
    token = jwt.encode(payload, STRINGEE_API_KEY_SECRET, algorithm="HS256")
    return token, clean_id

@app.get("/stringee/token")
def get_token(user_id: str=Query(...), ride_id: str=Query("")):
    token, cid = generate_stringee_token(user_id, ride_id)
    if not token: raise HTTPException(status_code=500, detail="STRINGEE Keys missing in Env")
    return {"token":token,"access_token":token,"userId":cid}

@app.post("/users/init")
def init_user(p: UserInitRequest):
    try:
        supabase.table("users").delete().eq("id", p.device_id).execute()
        res = supabase.table("users").insert({"id":p.device_id,"device_id":p.device_id}).execute()
        return res.data[0]
    except Exception as e:
        print(e)
        return {"id":p.device_id}

@app.post("/drivers/login")
def driver_login(p: DriverLoginRequest):
    try:
        login_id = (p.driver_id or p.phone or "").strip()
        if not login_id:
            raise HTTPException(status_code=400, detail="Driver ID भेजो")

        res = supabase.table("drivers").select("*").eq("driver_id", login_id).execute()
        if not res.data:
            res = supabase.table("drivers").select("*").eq("id", login_id).execute()
        if not res.data:
            res = supabase.table("drivers").select("*").eq("phone", login_id).execute()

        if not res.data:
            raise HTTPException(status_code=404, detail=f"Driver {login_id} नहीं मिला")

        driver=res.data[0]
        if str(driver.get("password","")).strip()!= str(p.password).strip():
            raise HTTPException(status_code=401, detail="Password गलत है")

        supabase.table("drivers").update({
            "is_online":True,
            "available":True,
            "fcm_token":p.fcm_token or driver.get("fcm_token"),
            "last_seen":datetime.now().isoformat()
        }).eq("id",driver["id"]).execute()

        d = supabase.table("drivers").select("*").eq("id",driver["id"]).execute().data[0]
        return {"success":True,"driver":d}
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Login Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/rides/pending/list")
def pending_rides(vehicle_type: str = Query(None)):
    try:
        q = supabase.table("rides").select("*").eq("status","pending").order("id", desc=True)
        if vehicle_type and vehicle_type.strip()!= "":
            q = q.ilike("vehicle_type", f"%{vehicle_type.strip()}%")
        res = q.execute()
        data = res.data or []
        for ride in data:
            ride.pop("otp", None)
            ride.pop("stringee_token", None)
        return data
    except Exception as e:
        print(f"Pending Error: {e}")
        return []

@app.get("/vehicles")
def get_vehicles():
    try:
        res = supabase.table("vehicles").select("*").execute()
        if res.data and len(res.data) > 0:
            return res.data
        return [
            {"id": 1, "name": "Mini", "type": "Mini", "base_fare": 50, "per_km": 12, "capacity": 4},
            {"id": 2, "name": "Sedan", "type": "Sedan", "base_fare": 80, "per_km": 15, "capacity": 4},
            {"id": 3, "name": "Auto", "type": "Auto", "base_fare": 30, "per_km": 10, "capacity": 3},
            {"id": 4, "name": "Bike", "type": "Bike", "base_fare": 20, "per_km": 8, "capacity": 1},
        ]
    except Exception as e:
        print(f"Vehicles Error: {e}")
        return [
            {"id": 1, "name": "Mini", "type": "Mini", "base_fare": 50, "per_km": 12},
            {"id": 2, "name": "Sedan", "type": "Sedan", "base_fare": 80, "per_km": 15},
        ]

@app.put("/rides/{ride_id}/accept")
def accept_ride(ride_id: int, driver_id: str=Query(...)):
    try:
        # 1. Driver निकालो vehicle_number के साथ
        d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type,vehicle_model").eq("id",driver_id).execute()
        if not d_res.data:
            d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type,vehicle_model").eq("driver_id",driver_id).execute()
        if not d_res.data:
            raise HTTPException(status_code=404, detail="Driver not found")

        driver = d_res.data[0]
        new_token, clean_id = generate_stringee_token(driver["id"], str(ride_id))
        if not new_token:
            raise HTTPException(status_code=500, detail="STRINGEE keys missing")

        # 2. Rides table में update - vehicle_number save होगा
        ride_update = {
            "driver_id": driver["id"],
            "status": "accepted",
            "driver_name": driver.get("name"),
            "driver_phone": driver.get("phone"),
            "vehicle_number": driver.get("vehicle_number"), # यही Main Fix है
            "vehicle_type": driver.get("vehicle_type"),
            "driver_stringee_token": new_token,
            "driver_stringee_user_id": clean_id,
            "accepted_at": datetime.now().isoformat()
        }

        # सिर्फ pending ride ही accept हो
        updated = supabase.table("rides").update(ride_update).eq("id",ride_id).eq("status","pending").execute()

        if not updated.data:
            raise HTTPException(status_code=404, detail="Ride already taken or not found")

        return {"success":True, "ride":updated.data[0], "stringee_token":new_token, "stringee_user_id":clean_id}

    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Accept Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/rides/{ride_id}/verify-otp")
def verify_ride_otp(ride_id: int, payload: OtpVerifyRequest):
    try:
        res = supabase.table("rides").select("id,otp,status").eq("id", ride_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Ride not found")
        ride = res.data[0]
        if ride["status"] not in ["accepted", "arrived"]:
            if ride["status"] == "started":
                return {"success": True, "message": "Already verified"}
        db_otp = str(ride.get("otp", "")).strip()
        user_otp = str(payload.otp).strip()
        print(f"Verify Ride {ride_id}: DB OTP={db_otp} vs User OTP={user_otp}")
        if db_otp!= user_otp:
            raise HTTPException(status_code=400, detail="Galat OTP")
        supabase.table("rides").update({
            "status": "started",
             "started_at": datetime.now().isoformat()
        }).eq("id", ride_id).execute()
        return {"success": True, "message": "OTP Verified, Ride Started"}
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"OTP Verify Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/rides")
async def create_ride(payload: RideCreateRequest, user_id: str=Query(...)):
    token, clean_id = generate_stringee_token(user_id)
    ride_data = {
        "user_id":user_id,"pickup_lat":payload.pickup_lat,"pickup_lng":payload.pickup_lng,
        "drop_lat":payload.drop_lat,"drop_lng":payload.drop_lng,
        "pickup_address":payload.pickup_address,"drop_address":payload.drop_address,
        "vehicle_type":payload.vehicle_type,"distance":payload.distance,"fare":payload.fare,
        "status":"pending","otp":payload.otp,"city":"Sikar",
        "created_at":datetime.now().isoformat(),
        "stringee_token":token,"stringee_user_id":clean_id
    }
    res = supabase.table("rides").insert(ride_data).execute()
    new_ride=res.data[0]

    # 1. WebSocket Broadcast (Foreground)
    broadcast_data = {k: v for k, v in new_ride.items() if k!= "otp"}
    await manager.broadcast_new_ride(broadcast_data)

    # 2. FCM High Priority (Background + Kill)
    send_fcm_high_priority_to_drivers(new_ride, payload.vehicle_type)

    return new_ride

@app.get("/rides/{ride_id}")
def get_ride(ride_id: int):
    res=supabase.table("rides").select("*").eq("id",ride_id).execute()
    return res.data[0] if res.data else {"error":"not found"}

@app.put("/rides/{ride_id}/complete")
def complete_ride(ride_id: int):
    supabase.table("rides").update({"status":"completed", "completed_at": datetime.now().isoformat()}).eq("id",ride_id).execute()
    return {"success":True}

@app.put("/rides/{ride_id}/cancel")
def cancel_ride(ride_id: int, user_id: str=Query(...)):
    supabase.table("rides").update({"status":"cancelled"}).eq("id",ride_id).execute()
    return {"success":True}

@app.websocket("/ws/drivers")
async def ws_drivers(ws: WebSocket, city: str = Query("Sikar")):
    await manager.connect_driver(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_driver(ws)

@app.websocket("/ws/rides/{ride_id}")
async def ws_ride(ws: WebSocket, ride_id: int):
    await manager.connect(ride_id, ws)
    try:
        while True:
            data = await ws.receive_text()
            await manager.broadcast(ride_id, json.loads(data))
    except WebSocketDisconnect:
        manager.disconnect(ride_id, ws)

@app.get("/drivers/{driver_id}/active-ride")
def get_active_ride(driver_id: str):
    try:
        res = supabase.table("rides").select("*").eq("driver_id", driver_id).in_("status", ["accepted", "started", "arrived"]).order("id", desc=True).limit(1).execute()
        if res.data and len(res.data) > 0:
            return {"active": True, "ride": res.data[0]}
        return {"active": False, "ride": None}
    except Exception as e:
        print(f"Active Ride Error: {e}")
        return {"active": False, "ride": None}

@app.get("/")
def root(): return {"status":"FCM High Priority Fixed - Background Alert Working"}

