import os, json, time, jwt, uuid, math, httpx
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
app = FastAPI(title="Rivo Taxi API - Global 3.0 Vehicle Range")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

STRINGEE_API_KEY_SID = os.getenv("STRINGEE_API_KEY_SID")
STRINGEE_API_KEY_SECRET = os.getenv("STRINGEE_API_KEY_SECRET")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

try:
    fj = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if fj and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(fj))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase OK")
except Exception as e:
    print(f"❌ Firebase Error: {e}")

def haversine(lat1, lon1, lat2, lon2):
    try:
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        return R * c
    except:
        return 99999.0

def get_range_by_vehicle(vehicle_type: str, db_range=None):
    if db_range is not None:
        try:
            return float(db_range)
        except:
            pass
    vt = str(vehicle_type).lower()
    if "bike" in vt: return 5.0
    if "auto" in vt or "e-rickshaw" in vt: return 7.0
    if "parcel" in vt or "delivery" in vt or "courier" in vt: return 8.0
    if "mini" in vt: return 10.0
    if "sedan" in vt: return 12.0
    if "suv" in vt or "xl" in vt: return 15.0
    return 10.0

async def get_city_country_currency(lat: float, lng: float):
    try:
        if not GOOGLE_MAPS_API_KEY:
            return "Unknown", "India", "INR"
        url = f"https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lng}&key={GOOGLE_MAPS_API_KEY}&language=en"
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url)
            data = r.json()
            city = "Unknown"; country = "India"; currency = "INR"
            if data.get("results"):
                for comp in data["results"][0]["address_components"]:
                    if "locality" in comp["types"] or "administrative_area_level_2" in comp["types"]:
                        if city == "Unknown": city = comp["long_name"]
                    if "country" in comp["types"]:
                        country = comp["long_name"]
                        cmap = {"United Arab Emirates": "AED", "United States": "USD", "United Kingdom": "GBP", "Saudi Arabia": "SAR", "Qatar": "QAR", "Kuwait": "KWD", "Oman": "OMR", "Canada": "CAD", "Australia": "AUD", "Singapore": "SGD", "India": "INR"}
                        currency = cmap.get(country, "USD")
            return city, country, currency
    except:
        return "Unknown", "India", "INR"

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, list[WebSocket]] = {}
        self.driver_connections: list[dict] = []
    async def connect(self, ride_id: int, ws: WebSocket):
        await ws.accept()
        if ride_id not in self.active_connections: self.active_connections[ride_id] = []
        self.active_connections[ride_id].append(ws)
    def disconnect(self, ride_id: int, ws: WebSocket):
        if ride_id in self.active_connections and ws in self.active_connections[ride_id]: self.active_connections[ride_id].remove(ws)
    async def broadcast(self, ride_id: int, msg: dict):
        if ride_id in self.active_connections:
            for c in list(self.active_connections[ride_id]):
                try: await c.send_text(json.dumps(msg))
                except: pass
    async def connect_driver(self, ws: WebSocket, vehicle_type: str = "", lat: float = None, lng: float = None, driver_range: float = None):
        await ws.accept()
        self.driver_connections = [d for d in self.driver_connections if d["ws"] != ws]
        self.driver_connections.append({"ws": ws, "vehicle_type": vehicle_type.lower().strip(), "lat": lat, "lng": lng, "range": driver_range})
    def disconnect_driver(self, ws: WebSocket):
        self.driver_connections = [d for d in self.driver_connections if d["ws"] != ws]
    async def broadcast_new_ride(self, ride_data: dict):
        req_type = str(ride_data.get('vehicle_type','')).lower().strip()
        p_lat = float(ride_data.get('pickup_lat', 0)); p_lng = float(ride_data.get('pickup_lng', 0))
        dead = []
        for driver in list(self.driver_connections):
            ws = driver["ws"]
            driver_v = str(driver.get("vehicle_type","")).lower().strip()
            if driver_v and req_type and driver_v != req_type: continue
            d_lat = driver.get("lat"); d_lng = driver.get("lng")
            if d_lat and d_lng:
                drange = driver.get("range") or get_range_by_vehicle(driver_v)
                dist = haversine(d_lat, d_lng, p_lat, p_lng)
                if dist > float(drange): continue
            try: await ws.send_text(json.dumps({"type":"new_ride_alert","ride":ride_data}))
            except: dead.append(driver)
        for d in dead:
            try: self.driver_connections.remove(d)
            except: pass

manager = ConnectionManager()

def send_fcm_global(ride_data: dict, vehicle_type: str = ""):
    try:
        if not firebase_admin._apps: return
        v_type = vehicle_type.strip().lower()
        all_drivers = supabase.table("drivers").select("fcm_token,vehicle_type,current_latitude,current_longitude,range").eq("is_online", True).neq("fcm_token", "").execute().data or []
        tokens = []; p_lat = float(ride_data.get('pickup_lat', 0)); p_lng = float(ride_data.get('pickup_lng', 0))
        for d in all_drivers:
            d_v = str(d.get('vehicle_type','')).lower().strip()
            if d_v != v_type: continue
            d_lat = d.get('current_latitude'); d_lng = d.get('current_longitude')
            if d_lat and d_lng:
                drange = get_range_by_vehicle(d_v, d.get('range'))
                dist = haversine(float(d_lat), float(d_lng), p_lat, p_lng)
                if dist > float(drange): continue
            if d.get('fcm_token'): tokens.append(d['fcm_token'])
        tokens = list(set(tokens))
        if not tokens: return
        country = ride_data.get('country','India')
        is_parcel = "parcel" in str(ride_data.get('trip_type','')).lower()
        title = "📦 New Parcel!" if is_parcel else f"🔔 New {vehicle_type} Ride!"
        if "India" in country or "भारत" in country:
            title = "📦 नई Parcel आई है!" if is_parcel else f"🔔 नई {vehicle_type} Ride आई है!"
        body = f"{ride_data.get('pickup_address','')[:40]} -> {ride_data.get('drop_address','')[:40]}"
        android_config = messaging.AndroidConfig(priority='high', notification=messaging.AndroidNotification(channel_id='ride_channel_v5', priority='max', visibility='public', sound='alert'))
        msg = messaging.MulticastMessage(notification=messaging.Notification(title=title, body=body), data={'vehicle_type': v_type, 'ride_id': str(ride_data.get('id','')), 'type': 'new_ride_alert'}, tokens=tokens, android=android_config)
        messaging.send_each_for_multicast(msg)
    except Exception as e:
        print(f"FCM Error {e}")

class RideCreateRequest(BaseModel):
    pickup_lat: float; pickup_lng: float; drop_lat: float; drop_lng: float
    pickup_address: Optional[str]=None; drop_address: Optional[str]=None
    vehicle_type: str="Mini"; distance: float; fare: float
    scheduled_time: Optional[str]=None; trip_type: str="ride"; otp: str

class DriverLoginRequest(BaseModel):
    driver_id: Optional[str]=None; phone: Optional[str]=None; password: str; fcm_token: Optional[str]=None
    mobile: Optional[str]=None
    username: Optional[str]=None

class OtpVerifyRequest(BaseModel):
    otp: str

class DriverLocationRequest(BaseModel):
    lat: Optional[float]=None; lng: Optional[float]=None
    latitude: Optional[float]=None; longitude: Optional[float]=None

def generate_stringee_token(user_id: str, ride_id: str=""):
    if not STRINGEE_API_KEY_SID or not STRINGEE_API_KEY_SECRET: return None, None
    clean_id = str(user_id).replace("+","").replace(" ","_").replace("-","_").strip()
    now = int(time.time())
    jti = f"{STRINGEE_API_KEY_SID}-{now}-{clean_id}-{ride_id}-{uuid.uuid4().hex[:6]}"
    payload = {"jti": jti, "iss": STRINGEE_API_KEY_SID, "exp": now+86400, "userId": clean_id}
    return jwt.encode(payload, STRINGEE_API_KEY_SECRET, algorithm="HS256"), clean_id

# ==================== DRIVER LOGIN - ID + PASSWORD ====================
@app.post("/drivers/login")
def driver_login(payload: DriverLoginRequest):
    try:
        phone_input = payload.phone or payload.mobile or payload.driver_id or payload.username
        password_input = payload.password

        print(f"LOGIN TRY: {phone_input} / {password_input}")

        if not phone_input:
            raise HTTPException(status_code=400, detail="Phone/ID required")

        phone_input = str(phone_input).strip()

        # 1. phone column
        res = supabase.table("drivers").select("*").eq("phone", phone_input).execute()
        # 2. id column (car1)
        if not res.data:
            res = supabase.table("drivers").select("*").eq("id", phone_input).execute()
        # 3. driver_id column
        if not res.data:
            res = supabase.table("drivers").select("*").eq("driver_id", phone_input).execute()
        # 4. mobile column (try)
        if not res.data:
            try:
                res = supabase.table("drivers").select("*").eq("mobile", phone_input).execute()
            except:
                pass

        if not res.data:
            print(f"Driver not found: {phone_input}")
            raise HTTPException(status_code=404, detail="Driver not found")

        driver = res.data[0]

        # Password check - allow both string and int
        db_pass = str(driver.get('password','')).strip()
        input_pass = str(password_input).strip()
        if db_pass != input_pass:
            print(f"Wrong password for {phone_input}: DB={db_pass} Input={input_pass}")
            raise HTTPException(status_code=401, detail="Wrong password")

        # FCM Token Update
        if payload.fcm_token:
            try:
                supabase.table("drivers").update({"fcm_token": payload.fcm_token, "updated_at": datetime.now().isoformat()}).eq("id", driver["id"]).execute()
                driver["fcm_token"] = payload.fcm_token
            except Exception as e:
                print(f"FCM Update Error: {e}")

        print(f"LOGIN SUCCESS: {driver['id']} - {driver.get('name')}")
        return driver

    except HTTPException:
        raise
    except Exception as e:
        print(f"LOGIN ERROR: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/maps/directions")
async def get_directions_secure(origin: str = Query(...), dest: str = Query(...)):
    if not GOOGLE_MAPS_API_KEY: raise HTTPException(status_code=500, detail="GOOGLE_MAPS_API_KEY not set")
    try:
        url = f"https://maps.googleapis.com/maps/api/directions/json?origin={origin}&destination={dest}&key={GOOGLE_MAPS_API_KEY}&language=en&overview=full"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url); data = r.json()
            if data.get("status") == "OK":
                route = data["routes"][0]; leg = route["legs"][0]
                return {"points": route["overview_polyline"]["points"], "distance_text": leg["distance"]["text"], "distance_value": leg["distance"]["value"], "duration_text": leg["duration"]["text"]}
            else: raise HTTPException(status_code=400, detail=f"Google Error: {data.get('status')}")
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.put("/drivers/{driver_id}/location")
def update_driver_location(driver_id: str, payload: DriverLocationRequest):
    lat = payload.latitude if payload.latitude is not None else payload.lat
    lng = payload.longitude if payload.longitude is not None else payload.lng
    if lat is None or lng is None: raise HTTPException(status_code=400, detail="lat/lng required")
    d_res = supabase.table("drivers").select("id").eq("id", driver_id).execute()
    if not d_res.data: d_res = supabase.table("drivers").select("id").eq("driver_id", driver_id).execute()
    if not d_res.data: raise HTTPException(status_code=404, detail="Driver not found")
    real_id = d_res.data[0]["id"]
    supabase.table("drivers").update({"current_latitude": lat, "current_longitude": lng, "last_seen": datetime.now().isoformat(), "updated_at": datetime.now().isoformat(), "is_online": True}).eq("id", real_id).execute()
    try: supabase.table("rides").update({"driver_lat": lat, "driver_lng": lng}).eq("driver_id", real_id).in_("status", ["accepted", "started"]).execute()
    except: pass
    return {"success": True}

@app.get("/rides/pending/list")
def pending_rides(vehicle_type: str = Query(None), driver_id: str = Query(None), driver_lat: float = Query(None), driver_lng: float = Query(None)):
    try:
        if not driver_id:
            raise HTTPException(status_code=400, detail="driver_id required")
        d_res = supabase.table("drivers").select("current_latitude, current_longitude, range, vehicle_type").eq("id", driver_id).execute()
        if not d_res.data:
            d_res = supabase.table("drivers").select("current_latitude, current_longitude, range, vehicle_type").eq("driver_id", driver_id).execute()
        if not d_res.data:
            raise HTTPException(status_code=404, detail="Driver not found")
        drv = d_res.data[0]
        d_lat = driver_lat if driver_lat is not None else drv.get("current_latitude")
        d_lng = driver_lng if driver_lng is not None else drv.get("current_longitude")
        d_vehicle = vehicle_type or drv.get("vehicle_type")
        db_range = drv.get("range")
        driver_range = get_range_by_vehicle(d_vehicle, db_range)
        print(f"Driver: {d_vehicle} | DB Range: {db_range} | Final Range: {driver_range}km | Lat: {d_lat}, Lng: {d_lng}")
        if d_lat is None or d_lng is None:
            q = supabase.table("rides").select("*").eq("status","pending").order("id", desc=True).limit(50)
            if d_vehicle: q = q.eq("vehicle_type", d_vehicle)
            res = q.execute()
            for r in res.data or []: r.pop("otp", None)
            return res.data or []
        q = supabase.table("rides").select("*").eq("status","pending").order("id", desc=True).limit(50)
        if d_vehicle and d_vehicle.strip() != "":
            q = q.eq("vehicle_type", d_vehicle.strip())
        res = q.execute()
        rides = res.data or []
        filtered = []
        for ride in rides:
            try:
                p_lat = float(ride.get("pickup_lat", 0))
                p_lng = float(ride.get("pickup_lng", 0))
                if p_lat == 0 or p_lng == 0: continue
                dist = haversine(float(d_lat), float(d_lng), p_lat, p_lng)
                if dist <= float(driver_range):
                    ride["distance_from_driver"] = round(dist, 2)
                    ride["driver_range"] = driver_range
                    ride["driver_vehicle"] = d_vehicle
                    ride.pop("otp", None)
                    filtered.append(ride)
            except Exception as ex:
                print(f"Filter error: {ex}")
                continue
        filtered.sort(key=lambda x: x.get("distance_from_driver", 999))
        return filtered
    except HTTPException:
        raise
    except Exception as e:
        print(f"Pending Error: {e}")
        return []

@app.post("/rides")
async def create_ride(payload: RideCreateRequest, user_id: str=Query(...)):
    city, country, currency = await get_city_country_currency(payload.pickup_lat, payload.pickup_lng)
    token, clean_id = generate_stringee_token(user_id)
    trip = str(payload.trip_type).lower().strip()
    final_trip = "parcel" if "parcel" in trip else "ride"
    ride_data = {
        "user_id":user_id,
        "pickup_lat":payload.pickup_lat,"pickup_lng":payload.pickup_lng,
        "drop_lat":payload.drop_lat,"drop_lng":payload.drop_lng,
        "pickup_address":payload.pickup_address,"drop_address":payload.drop_address,
        "vehicle_type":payload.vehicle_type,"distance":payload.distance,"fare":payload.fare,
        "trip_type": final_trip,"status":"pending","otp":payload.otp,
        "city": city, "country": country, "currency": currency,
        "created_at":datetime.now().isoformat(),"stringee_token":token,"stringee_user_id":clean_id
    }
    res = supabase.table("rides").insert(ride_data).execute()
    if not res.data: raise HTTPException(status_code=500, detail="Failed")
    new_ride = res.data[0]
    await manager.broadcast_new_ride(new_ride)
    send_fcm_global(new_ride, payload.vehicle_type)
    return new_ride

@app.put("/rides/{ride_id}/accept")
def accept_ride(ride_id: int, driver_id: str=Query(...)):
    d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type,range").eq("id",driver_id).execute()
    if not d_res.data: d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type,range").eq("driver_id",driver_id).execute()
    if not d_res.data: raise HTTPException(status_code=404, detail="Driver not found")
    driver = d_res.data[0]
    new_token, clean_id = generate_stringee_token(driver["id"], str(ride_id))
    if not new_token: new_token = f"temp_{driver['id']}"; clean_id = str(driver["id"])
    ride_update = {"driver_id": driver["id"],"status": "accepted","driver_name": driver.get("name"),"driver_phone": driver.get("phone"),"vehicle_number": driver.get("vehicle_number"),"vehicle_type": driver.get("vehicle_type"),"driver_stringee_token": new_token,"driver_stringee_user_id": clean_id,"accepted_at": datetime.now().isoformat()}
    updated = supabase.table("rides").update(ride_update).eq("id",ride_id).eq("status","pending").execute()
    if not updated.data: raise HTTPException(status_code=409, detail="Already taken")
    return {"success":True, "ride":updated.data[0], "stringee_token":new_token}

@app.post("/rides/{ride_id}/verify-otp")
def verify_ride_otp(ride_id: int, payload: OtpVerifyRequest):
    res = supabase.table("rides").select("id,otp,status").eq("id", ride_id).execute()
    if not res.data: raise HTTPException(status_code=404, detail="Ride not found")
    ride = res.data[0]
    if ride["status"] == "started": return {"success": True}
    if str(ride.get("otp","")).strip()!= str(payload.otp).strip(): raise HTTPException(status_code=400, detail="Galat OTP")
    supabase.table("rides").update({"status": "started","started_at": datetime.now().isoformat()}).eq("id", ride_id).execute()
    return {"success": True}

@app.put("/rides/{ride_id}/complete")
def complete_ride(ride_id: int):
    supabase.table("rides").update({"status":"completed", "completed_at": datetime.now().isoformat()}).eq("id",ride_id).execute()
    return {"success":True}

@app.get("/drivers/{driver_id}/active-ride")
def get_active_ride(driver_id: str):
    res = supabase.table("rides").select("*").eq("driver_id", driver_id).in_("status", ["accepted", "started", "arrived"]).order("id", desc=True).limit(1).execute()
    if not res.data:
        d = supabase.table("drivers").select("id").eq("driver_id", driver_id).execute()
        if d.data:
            res = supabase.table("rides").select("*").eq("driver_id", d.data[0]["id"]).in_("status", ["accepted", "started", "arrived"]).order("id", desc=True).limit(1).execute()
    if res.data: return {"active": True, "ride": res.data[0]}
    return {"active": False, "ride": None}

@app.websocket("/ws/drivers")
async def ws_drivers(ws: WebSocket, vehicle_type: str = Query(""), lat: float = Query(None), lng: float = Query(None), driver_id: str = Query(None)):
    drange = None
    if driver_id:
        try:
            d_res = supabase.table("drivers").select("range, vehicle_type").eq("id", driver_id).execute()
            if not d_res.data: d_res = supabase.table("drivers").select("range, vehicle_type").eq("driver_id", driver_id).execute()
            if d_res.data:
                drange = get_range_by_vehicle(d_res.data[0].get("vehicle_type"), d_res.data[0].get("range"))
                if not vehicle_type: vehicle_type = d_res.data[0].get("vehicle_type","")
        except: pass
    await manager.connect_driver(ws, vehicle_type, lat, lng, drange)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_driver(ws)

@app.get("/")
def root():
    return {"status":"Rivo API Fixed - Driver Login ID+Password Working", "login_examples": ["car1 / 123", "9875262306 / 123"]}
