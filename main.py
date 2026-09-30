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
app = FastAPI(title="Taxi API - Call Both Side")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

STRINGEE_API_KEY_SID = os.getenv("STRINGEE_API_KEY_SID")
STRINGEE_API_KEY_SECRET = os.getenv("STRINGEE_API_KEY_SECRET")

try:
    fj = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if fj:
        cred = credentials.Certificate(json.loads(fj))
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
except Exception as e:
    print(e)

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, list[WebSocket]] = {}
        self.driver_connections: list[WebSocket] = []
    async def connect(self, ride_id: int, ws: WebSocket):
        await ws.accept()
        if ride_id not in self.active_connections: self.active_connections[ride_id]=[]
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
        await ws.accept(); self.driver_connections.append(ws)
    def disconnect_driver(self, ws: WebSocket):
        if ws in self.driver_connections: self.driver_connections.remove(ws)
    async def broadcast_new_ride(self, ride_data: dict):
        for ws in list(self.driver_connections):
            try: await ws.send_text(json.dumps({"type":"new_ride_alert","data":ride_data}))
            except: self.driver_connections.remove(ws)
manager = ConnectionManager()

class UserInitRequest(BaseModel): device_id: str
class RideCreateRequest(BaseModel):
    pickup_lat: float; pickup_lng: float; drop_lat: float; drop_lng: float
    pickup_address: Optional[str]=None; drop_address: Optional[str]=None
    vehicle_type: str="Mini"; distance: float; fare: float
    scheduled_time: Optional[str]=None; trip_type: str="ride"; otp: str
class DriverLoginRequest(BaseModel):
    driver_id: Optional[str]=None; phone: Optional[str]=None; password: str
    fcm_token: Optional[str]=None

def generate_stringee_token(user_id: str, ride_id: str=""):
    if not STRINGEE_API_KEY_SID or not STRINGEE_API_KEY_SECRET: return None, None
    clean_id = user_id.replace("+","").replace(" ","_").replace("-","_").strip()
    now = int(time.time())
    jti = f"{STRINGEE_API_KEY_SID}-{now}-{clean_id}-{ride_id}-{uuid.uuid4().hex[:6]}"
    payload = {"jti": jti, "iss": STRINGEE_API_KEY_SID, "exp": now+3600, "userId": clean_id}
    token = jwt.encode(payload, STRINGEE_API_KEY_SECRET, algorithm="HS256")
    return token, clean_id

@app.get("/stringee/token")
def get_token(user_id: str=Query(...), ride_id: str=Query("")):
    token, cid = generate_stringee_token(user_id, ride_id)
    if not token: raise HTTPException(status_code=500, detail="Keys missing")
    return {"token":token,"access_token":token,"userId":cid}

@app.post("/users/init")
def init_user(p: UserInitRequest):
    try:
        supabase.table("users").delete().eq("id", p.device_id).execute()
        res = supabase.table("users").insert({"id":p.device_id,"device_id":p.device_id}).execute()
        return res.data[0]
    except: return {"id":p.device_id}

@app.post("/drivers/login")
def driver_login(p: DriverLoginRequest):
    q = supabase.table("drivers").select("*")
    if p.driver_id: q=q.eq("id",p.driver_id)
    else: q=q.eq("phone",p.phone)
    res = q.execute()
    if not res.data: raise HTTPException(status_code=404, detail="Driver not found")
    driver=res.data[0]
    if driver.get("password")!=p.password: raise HTTPException(status_code=401, detail="Password galat")
    supabase.table("drivers").update({"is_online":True,"available":True,"fcm_token":p.fcm_token or driver.get("fcm_token"),"last_seen":datetime.now().isoformat()}).eq("id",driver["id"]).execute()
    d = supabase.table("drivers").select("*").eq("id",driver["id"]).execute().data[0]
    return {"success":True,"driver":d}

# LOGIN PAR TOKEN NAHI, SIRF ACCEPT PAR
@app.put("/rides/{ride_id}/accept")
def accept_ride(ride_id: int, driver_id: str=Query(...)):
    d_res = supabase.table("drivers").select("id,name,phone").eq("id",driver_id).execute()
    if not d_res.data: raise HTTPException(status_code=404, detail="Driver not found")
    driver=d_res.data[0]
    new_token, clean_id = generate_stringee_token(driver_id, str(ride_id))
    if not new_token: raise HTTPException(status_code=500, detail="STRINGEE keys missing")

    ride_update = {
        "driver_id": driver["id"], "status":"accepted",
        "driver_name": driver.get("name"), "driver_phone": driver.get("phone"),
        "driver_stringee_token": new_token,
        "driver_stringee_user_id": clean_id,
        "accepted_at": datetime.now().isoformat()
    }
    supabase.table("rides").update(ride_update).eq("id",ride_id).eq("status","pending").execute()
    ride = supabase.table("rides").select("*").eq("id",ride_id).execute().data[0]
    return {"success":True,"ride":ride,"stringee_token":new_token,"stringee_user_id":clean_id}

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
    await manager.broadcast_new_ride(new_ride)
    return new_ride

@app.get("/rides/{ride_id}")
def get_ride(ride_id: int):
    res=supabase.table("rides").select("*").eq("id",ride_id).execute()
    return res.data[0] if res.data else {"error":"not found"}

@app.get("/vehicles")
def get_vehicles():
    res=supabase.table("vehicles").select("*").order("id").execute()
    return res.data or []

@app.put("/rides/{ride_id}/complete")
def complete_ride(ride_id: int):
    supabase.table("rides").update({"status":"completed"}).eq("id",ride_id).execute()
    return {"success":True}

@app.put("/rides/{ride_id}/cancel")
def cancel_ride(ride_id: int, user_id: str=Query(...)):
    supabase.table("rides").update({"status":"cancelled"}).eq("id",ride_id).execute()
    return {"success":True}

@app.get("/")
def root(): return {"status":"Final - driver_stringee_token only"}

