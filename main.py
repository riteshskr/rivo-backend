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
app = FastAPI(title="Taxi API - Fixed")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

STRINGEE_API_KEY_SID = os.getenv("STRINGEE_API_KEY_SID")
STRINGEE_API_KEY_SECRET = os.getenv("STRINGEE_API_KEY_SECRET")

try:
    fj = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if fj and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(fj))
        firebase_admin.initialize_app(cred)
        print("Firebase OK")
except Exception as e:
    print(f"Firebase Error: {e}")

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, list[WebSocket]] = {}
        self.driver_connections: list[dict] = []

    async def connect(self, ride_id: int, ws: WebSocket):
        await ws.accept()
        if ride_id not in self.active_connections:
            self.active_connections[ride_id] = []
        self.active_connections[ride_id].append(ws)

    def disconnect(self, ride_id: int, ws: WebSocket):
        if ride_id in self.active_connections and ws in self.active_connections[ride_id]:
            self.active_connections[ride_id].remove(ws)

    async def broadcast(self, ride_id: int, msg: dict):
        if ride_id in self.active_connections:
            for c in list(self.active_connections[ride_id]):
                try:
                    await c.send_text(json.dumps(msg))
                except:
                    pass

    async def connect_driver(self, ws: WebSocket, vehicle_type: str = ""):
        await ws.accept()
        self.driver_connections.append({"ws": ws, "vehicle_type": vehicle_type.lower().strip()})
        print(f"Driver WS: {vehicle_type} Total {len(self.driver_connections)}")

    def disconnect_driver(self, ws: WebSocket):
        self.driver_connections = [d for d in self.driver_connections if d["ws"]!= ws]

    async def broadcast_new_ride(self, ride_data: dict):
        req_type = str(ride_data.get('vehicle_type','')).lower().strip()
        trip = str(ride_data.get('trip_type','ride')).lower()
        print(f"WS Broadcast {req_type} {trip} to {len(self.driver_connections)} drivers")
        for driver in list(self.driver_connections):
            ws = driver["ws"]
            driver_v = str(driver.get("vehicle_type","")).lower().strip()
            if not driver_v:
                continue
            if req_type and driver_v!= req_type:
                print(f"WS SKIP {driver_v}!= {req_type}")
                continue
            try:
                await ws.send_text(json.dumps({"type":"new_ride_alert","ride":ride_data,"vehicle_type":req_type,"trip_type":trip}))
            except:
                try:
                    self.driver_connections.remove(driver)
                except:
                    pass

manager = ConnectionManager()

def send_fcm_high_priority_to_drivers(ride_data: dict, vehicle_type: str = ""):
    try:
        if not firebase_admin._apps:
            return
        v_type = vehicle_type.strip().lower()
        if not v_type:
            print("No vehicle_type STOP")
            return
        all_drivers = supabase.table("drivers").select("fcm_token,vehicle_type,driver_id").eq("is_online", True).neq("fcm_token", "").execute().data or []
        tokens = []
        for d in all_drivers:
            d_v = str(d.get('vehicle_type','')).lower().strip()
            if d_v == v_type and d.get('fcm_token'):
                tokens.append(d['fcm_token'])
        tokens = list(set(tokens))
        print(f"RIDE {v_type} ALL:{len(all_drivers)} MATCH:{len(tokens)}")
        for d in all_drivers:
            d_v = str(d.get('vehicle_type','')).lower().strip()
            status = "SEND" if d_v == v_type else "SKIP"
            print(f" {d.get('driver_id')}={d_v} -> {status}")
        if not tokens:
            return
        trip = ride_data.get('trip_type','ride')
        msg = messaging.MulticastMessage(
            data={'vehicle_type': v_type, 'trip_type': str(trip), 'ride_id': str(ride_data.get('id','')), 'type': 'new_ride_alert'},
            tokens=tokens
        )
        res = messaging.send_each_for_multicast(msg)
        print(f"FCM Sent {res.success_count}")
    except Exception as e:
        print(f"FCM Error {e}")

class UserInitRequest(BaseModel):
    device_id: str

class RideCreateRequest(BaseModel):
    pickup_lat: float; pickup_lng: float; drop_lat: float; drop_lng: float
    pickup_address: Optional[str]=None; drop_address: Optional[str]=None
    vehicle_type: str="Mini"; distance: float; fare: float
    scheduled_time: Optional[str]=None; trip_type: str="ride"; otp: str

class DriverLoginRequest(BaseModel):
    driver_id: Optional[str]=None; phone: Optional[str]=None; password: str; fcm_token: Optional[str]=None

class OtpVerifyRequest(BaseModel):
    otp: str

def generate_stringee_token(user_id: str, ride_id: str=""):
    if not STRINGEE_API_KEY_SID or not STRINGEE_API_KEY_SECRET:
        return None, None
    clean_id = str(user_id).replace("+","").replace(" ","_").replace("-","_").strip()
    now = int(time.time())
    jti = f"{STRINGEE_API_KEY_SID}-{now}-{clean_id}-{ride_id}-{uuid.uuid4().hex[:6]}"
    payload = {"jti": jti, "iss": STRINGEE_API_KEY_SID, "exp": now+86400, "userId": clean_id}
    return jwt.encode(payload, STRINGEE_API_KEY_SECRET, algorithm="HS256"), clean_id

@app.get("/stringee/token")
def get_token(user_id: str=Query(...), ride_id: str=Query("")):
    token, cid = generate_stringee_token(user_id, ride_id)
    if not token:
        raise HTTPException(status_code=500, detail="STRINGEE Keys missing")
    return {"token":token,"access_token":token,"userId":cid}

@app.post("/users/init")
def init_user(p: UserInitRequest):
    try:
        supabase.table("users").delete().eq("id", p.device_id).execute()
        res = supabase.table("users").insert({"id":p.device_id,"device_id":p.device_id}).execute()
        return res.data[0]
    except:
        return {"id":p.device_id}

@app.post("/drivers/login")
def driver_login(p: DriverLoginRequest):
    login_id = (p.driver_id or p.phone or "").strip()
    if not login_id:
        raise HTTPException(status_code=400, detail="Driver ID")
    res = supabase.table("drivers").select("*").eq("driver_id", login_id).execute()
    if not res.data:
        res = supabase.table("drivers").select("*").eq("id", login_id).execute()
    if not res.data:
        res = supabase.table("drivers").select("*").eq("phone", login_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Driver not found")
    driver=res.data[0]
    if str(driver.get("password","")).strip()!= str(p.password).strip():
        raise HTTPException(status_code=401, detail="Password गलत")
    supabase.table("drivers").update({"is_online":True,"available":True,"fcm_token":p.fcm_token or driver.get("fcm_token"),"last_seen":datetime.now().isoformat()}).eq("id",driver["id"]).execute()
    d = supabase.table("drivers").select("*").eq("id",driver["id"]).execute().data[0]
    return {"success":True,"driver":d}

@app.get("/rides/pending/list")
def pending_rides(vehicle_type: str = Query(None)):
    q = supabase.table("rides").select("*").eq("status","pending").order("id", desc=True)
    if vehicle_type and vehicle_type.strip()!= "":
        q = q.eq("vehicle_type", vehicle_type.strip())
    res = q.execute()
    data = res.data or []
    for ride in data:
        ride.pop("otp", None)
    return data

@app.get("/vehicles")
def get_vehicles():
    try:
        res = supabase.table("vehicles").select("*").execute()
        if res.data:
            return res.data
    except:
        pass
    return [{"id":1,"name":"Mini"}]

@app.put("/rides/{ride_id}/accept")
def accept_ride(ride_id: int, driver_id: str=Query(...)):
    d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type").eq("id",driver_id).execute()
    if not d_res.data:
        d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type").eq("driver_id",driver_id).execute()
    if not d_res.data:
        raise HTTPException(status_code=404, detail="Driver not found")
    driver = d_res.data[0]
    new_token, clean_id = generate_stringee_token(driver["id"], str(ride_id))
    ride_update = {"driver_id": driver["id"],"status": "accepted","driver_name": driver.get("name"),"driver_phone": driver.get("phone"),"vehicle_number": driver.get("vehicle_number"),"vehicle_type": driver.get("vehicle_type"),"driver_stringee_token": new_token,"driver_stringee_user_id": clean_id,"accepted_at": datetime.now().isoformat()}
    updated = supabase.table("rides").update(ride_update).eq("id",ride_id).eq("status","pending").execute()
    if not updated.data:
        raise HTTPException(status_code=404, detail="Ride taken")
    return {"success":True, "ride":updated.data[0], "stringee_token":new_token, "stringee_user_id":clean_id}

@app.post("/rides/{ride_id}/verify-otp")
def verify_ride_otp(ride_id: int, payload: OtpVerifyRequest):
    res = supabase.table("rides").select("id,otp,status").eq("id", ride_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Ride not found")
    ride = res.data[0]
    if ride["status"] == "started":
        return {"success": True}
    if str(ride.get("otp","")).strip()!= str(payload.otp).strip():
        raise HTTPException(status_code=400, detail="Galat OTP")
    supabase.table("rides").update({"status": "started","started_at": datetime.now().isoformat()}).eq("id", ride_id).execute()
    return {"success": True}

@app.post("/rides")
async def create_ride(payload: RideCreateRequest, user_id: str=Query(...)):
    token, clean_id = generate_stringee_token(user_id)
    ride_data = {"user_id":user_id,"pickup_lat":payload.pickup_lat,"pickup_lng":payload.pickup_lng,"drop_lat":payload.drop_lat,"drop_lng":payload.drop_lng,"pickup_address":payload.pickup_address,"drop_address":payload.drop_address,"vehicle_type":payload.vehicle_type,"distance":payload.distance,"fare":payload.fare,"trip_type": payload.trip_type,"scheduled_time": payload.scheduled_time,"status":"pending","otp":payload.otp,"city":"Sikar","created_at":datetime.now().isoformat(),"stringee_token":token,"stringee_user_id":clean_id}
    res = supabase.table("rides").insert(ride_data).execute()
    new_ride = res.data[0]
    await manager.broadcast_new_ride(new_ride)
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
async def ws_drivers(ws: WebSocket, city: str = Query("Sikar"), vehicle_type: str = Query("")):
    await manager.connect_driver(ws, vehicle_type)
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
    res = supabase.table("rides").select("*").eq("driver_id", driver_id).in_("status", ["accepted", "started", "arrived"]).order("id", desc=True).limit(1).execute()
    if res.data:
        return {"active": True, "ride": res.data[0]}
    return {"active": False, "ride": None}

@app.get("/")
def root():
    return {"status":"Fixed"}

