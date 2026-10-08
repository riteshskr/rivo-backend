import os, json, time, jwt, uuid, math, httpx, asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, messaging

load_dotenv()
app = FastAPI(title="Rivo Taxi API - Final 18.1 Fixed")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

STRINGEE_API_KEY_SID = os.getenv("STRINGEE_API_KEY_SID")
STRINGEE_API_KEY_SECRET = os.getenv("STRINGEE_API_KEY_SECRET")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

try:
    fj = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if fj and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(fj))
        firebase_admin.initialize_app(cred)
        print("Firebase OK")
except Exception as e:
    print(f"Firebase Error: {e}")

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
            r = float(db_range)
            if r > 0:
                return r
        except:
            pass
    return None

def is_within_range(driver_range, distance):
    if driver_range is None:
        return True
    try:
        return float(distance) <= float(driver_range)
    except:
        return True

def get_time_slot(scheduled_time_str=None):
    try:
        if scheduled_time_str:
            dt = datetime.fromisoformat(str(scheduled_time_str).replace('Z','+00:00'))
            hour = dt.hour if dt.tzinfo is None else dt.astimezone(timezone(timedelta(hours=5, minutes=30))).hour
        else:
            now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            hour = now_ist.hour
        if hour >= 20 or hour < 6:
            return "night"
        return "day"
    except:
        return "day"

def calculate_fare_by_type(vehicle_row, distance_km, trip_type, scheduled_time_str=None):
    slot = get_time_slot(scheduled_time_str)
    distance_km = float(distance_km)
    if trip_type == "pool":
        rate = float(vehicle_row.get("pool_night_per_km") or vehicle_row.get("pool_per_km") or 8.0) if slot == "night" else float(vehicle_row.get("pool_per_km") or 6.0)
        min_fare = float(vehicle_row.get("pool_min_fare") or 30.0)
    elif trip_type == "parcel":
        rate = float(vehicle_row.get("parcel_night_per_km") or vehicle_row.get("parcel_per_km") or 10.0) if slot == "night" else float(vehicle_row.get("parcel_per_km") or 8.0)
        min_fare = float(vehicle_row.get("parcel_min_fare") or 50.0)
    else:
        rate = float(vehicle_row.get("night_fare_per_km") or vehicle_row.get("fare_per_km") or 12.0) if slot == "night" else float(vehicle_row.get("fare_per_km") or 10.0)
        min_fare = float(vehicle_row.get("min_fare") or 50.0)
    fare = rate * distance_km
    if fare < min_fare:
        fare = min_fare
    return round(fare, 2), slot, rate

def is_point_along_route(curr_lat, curr_lng, drop_lat, drop_lng, new_lat, new_lng, max_dist_km=2.5):
    try:
        total_dist = haversine(curr_lat, curr_lng, drop_lat, drop_lng)
        d1 = haversine(curr_lat, curr_lng, new_lat, new_lng)
        d2 = haversine(new_lat, new_lng, drop_lat, drop_lng)
        if abs((d1 + d2) - total_dist) <= max_dist_km and d1 < total_dist and d1 > 0.3:
            return True, d1
        return False, 999
    except:
        return False, 999

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
    async def connect_driver(self, ws: WebSocket, vehicle_type: str = "", lat: float = None, lng: float = None, driver_range: float = None, driver_id: str = ""):
        await ws.accept()
        self.driver_connections = [d for d in self.driver_connections if d["ws"]!= ws]
        if driver_id:
            self.driver_connections = [d for d in self.driver_connections if d.get("driver_id")!= driver_id]
        self.driver_connections.append({"ws": ws, "vehicle_type": vehicle_type.lower().strip(), "lat": lat, "lng": lng, "range": driver_range, "driver_id": driver_id})
    def disconnect_driver(self, ws: WebSocket):
        self.driver_connections = [d for d in self.driver_connections if d["ws"]!= ws]
    async def broadcast_new_ride(self, ride_data: dict):
        req_type = str(ride_data.get('vehicle_type','')).lower().strip()
        p_lat = float(ride_data.get('pickup_lat', 0))
        p_lng = float(ride_data.get('pickup_lng', 0))
        dead = []
        for driver in list(self.driver_connections):
            ws = driver["ws"]
            driver_v = str(driver.get("vehicle_type","")).lower().strip()
            if driver_v and req_type and driver_v!= req_type:
                continue
            d_lat = driver.get("lat")
            d_lng = driver.get("lng")
            if d_lat and d_lng:
                drange = get_range_by_vehicle(driver_v, driver.get("range"))
                dist = haversine(d_lat, d_lng, p_lat, p_lng)
                if not is_within_range(drange, dist):
                    continue
            try:
                await ws.send_text(json.dumps({"type":"new_ride_alert","ride":ride_data}))
            except:
                dead.append(driver)
        for d in dead:
            try:
                self.driver_connections.remove(d)
            except:
                pass
    async def broadcast_ride_taken(self, ride_id: int):
        dead = []
        for driver in list(self.driver_connections):
            try:
                await driver["ws"].send_text(json.dumps({"type":"ride_taken","ride_id":ride_id}))
            except:
                dead.append(driver)
        for d in dead:
            try:
                self.driver_connections.remove(d)
            except:
                pass

manager = ConnectionManager()

def send_fcm_global(ride_data: dict, vehicle_type: str = ""):
    try:
        if not firebase_admin._apps:
            return
        v_type = vehicle_type.strip().lower()
        all_drivers = supabase.table("drivers").select("fcm_token,vehicle_type,current_latitude,current_longitude,range").eq("is_online", True).neq("fcm_token", "").execute().data or []
        tokens = []
        p_lat = float(ride_data.get('pickup_lat', 0))
        p_lng = float(ride_data.get('pickup_lng', 0))
        for d in all_drivers:
            d_v = str(d.get('vehicle_type','')).lower().strip()
            if d_v!= v_type:
                continue
            d_lat = d.get('current_latitude')
            d_lng = d.get('current_longitude')
            if d_lat and d_lng:
                drange = get_range_by_vehicle(d_v, d.get('range'))
                dist = haversine(float(d_lat), float(d_lng), p_lat, p_lng)
                if not is_within_range(drange, dist):
                    continue
            if d.get('fcm_token'):
                tokens.append(d['fcm_token'])
        tokens = list(set(tokens))
        if not tokens:
            return
        trip = str(ride_data.get('trip_type','ride')).lower()
        seats_info = ""
        if "pool" in trip:
            seats_info = f" Seats:{ride_data.get('seats_booked',1)}/{ride_data.get('total_seats',3)}"
        if "parcel" in trip:
            title = f"PARCEL {vehicle_type}!"
        elif "pool" in trip:
            title = f"POOL {vehicle_type}!{seats_info}"
        else:
            title = f"RIDE {vehicle_type}!"
        body = f"[{trip.upper()}{seats_info}] {ride_data.get('pickup_address','')[:40]} -> {ride_data.get('drop_address','')[:25]} | {ride_data.get('fare','')}"
        android_notif = messaging.AndroidNotification(channel_id='ride_channel_v5', priority='max', visibility='public', sound='alert', default_sound=False)
        android_config = messaging.AndroidConfig(priority='high', notification=android_notif)
        apns_config = messaging.APNSConfig(payload=messaging.APNSPayload(aps=messaging.Aps(sound='default', badge=1)))
        safe_ride = {k: v for k, v in ride_data.items() if k!= 'otp'}
        msg = messaging.MulticastMessage(notification=messaging.Notification(title=title, body=body), data={'vehicle_type': v_type, 'trip_type': trip, 'ride_id': str(ride_data.get('id','')), 'type': 'new_ride_alert', 'fare': str(ride_data.get('fare','')), 'click_action': 'FLUTTER_NOTIFICATION_CLICK', 'sound': 'alert', 'ride_json': json.dumps(safe_ride, default=str)}, tokens=tokens, android=android_config, apns=apns_config)
        messaging.send_each_for_multicast(msg)
    except Exception as e:
        print(f"FCM Error {e}")

class RideCreateRequest(BaseModel):
    pickup_lat: float
    pickup_lng: float
    drop_lat: float
    drop_lng: float
    pickup_address: Optional[str]=None
    drop_address: Optional[str]=None
    vehicle_type: str="Mini"
    distance: float
    fare: float
    scheduled_time: Optional[str]=None
    trip_type: str="ride"
    otp: str
    seats: int = 1

class DriverLoginRequest(BaseModel):
    driver_id: Optional[str]=None
    phone: Optional[str]=None
    password: str
    fcm_token: Optional[str]=None
    mobile: Optional[str]=None
    username: Optional[str]=None

class OtpVerifyRequest(BaseModel):
    otp: str

class DriverLocationRequest(BaseModel):
    lat: Optional[float]=None
    lng: Optional[float]=None
    latitude: Optional[float]=None
    longitude: Optional[float]=None

class PoolDropRequest(BaseModel):
    driver_id: str

def generate_stringee_token(user_id: str, ride_id: str=""):
    if not STRINGEE_API_KEY_SID or not STRINGEE_API_KEY_SECRET:
        return None, None
    raw = str(user_id).strip().replace(" ", "_").replace("+", "").replace("-", "_").lower()
    clean_id = "".join(c for c in raw if c.isalnum() or c == "_")
    if len(clean_id) < 3:
        clean_id = f"user_{clean_id}_{uuid.uuid4().hex[:4]}"
    now = int(time.time())
    jti = f"{STRINGEE_API_KEY_SID}-{now}-{clean_id}-{ride_id}-{uuid.uuid4().hex[:6]}"
    payload = {"jti": jti, "iss": STRINGEE_API_KEY_SID, "exp": now+86400*7, "userId": clean_id, "icd": True, "rest_api": True}
    token = jwt.encode(payload, STRINGEE_API_KEY_SECRET, algorithm="HS256", headers={"cty": "stringee-api;v=1"})
    return token, clean_id

def archive_and_delete_ride(ride_id: int):
    try:
        local_supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        res = local_supabase.table("rides").select("*").eq("id", ride_id).execute()
        if not res.data:
            return
        ride_data = res.data[0]
        original_id = ride_data.get('id')
        ride_data.pop('id', None)
        ride_data['original_ride_id'] = original_id
        ride_data['archived_at'] = datetime.now(timezone.utc).isoformat()
        try:
            local_supabase.table("rides_archive").insert(ride_data).execute()
        except:
            try:
                minimal = {"original_ride_id": original_id, "user_id": ride_data.get('user_id'), "status": ride_data.get('status'), "fare": ride_data.get('fare'), "pickup_address": ride_data.get('pickup_address'), "drop_address": ride_data.get('drop_address'), "archived_at": datetime.now(timezone.utc).isoformat(), "vehicle_type": ride_data.get('vehicle_type'), "trip_type": ride_data.get('trip_type'), "seats_booked": ride_data.get('seats_booked'), "total_seats": ride_data.get('total_seats')}
                local_supabase.table("rides_archive").insert(minimal).execute()
            except:
                return
        try:
            local_supabase.table("rides").delete().eq("id", int(original_id)).execute()
        except:
            pass
        try:
            supabase.table("rides").delete().eq("id", int(original_id)).execute()
        except:
            pass
    except Exception as e:
        print(f"Archive Error {e}")

TIMEOUT_MINUTES = 10

async def auto_timeout_checker():
    await asyncio.sleep(5)
    while True:
        try:
            now = datetime.now(timezone.utc)
            pending_res = supabase.table("rides").select("id,created_at,scheduled_time,status").eq("status", "pending").limit(100).execute()
            for r in pending_res.data or []:
                ride_id = r['id']
                created_at_str = r.get('created_at')
                scheduled_str = r.get('scheduled_time')
                should_timeout = False
                if not scheduled_str or str(scheduled_str).strip() in ["", "null", "None"]:
                    if created_at_str:
                        try:
                            created_dt = datetime.fromisoformat(str(created_at_str).replace('Z','+00:00'))
                            if created_dt.tzinfo is None:
                                created_dt = created_dt.replace(tzinfo=timezone.utc)
                            if (now - created_dt).total_seconds()/60 >= TIMEOUT_MINUTES:
                                should_timeout=True
                        except:
                            pass
                else:
                    try:
                        sched_dt = datetime.fromisoformat(str(scheduled_str).replace('Z','+00:00'))
                        if sched_dt.tzinfo is None:
                            sched_dt = sched_dt.replace(tzinfo=timezone.utc)
                        if now >= sched_dt - timedelta(minutes=TIMEOUT_MINUTES):
                            should_timeout=True
                    except:
                        pass
                if should_timeout:
                    try:
                        supabase.table("rides").update({"status": "timeout"}).eq("id", ride_id).execute()
                        archive_and_delete_ride(ride_id)
                        await manager.broadcast_ride_taken(ride_id)
                    except:
                        pass
        except:
            pass
        await asyncio.sleep(30)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_timeout_checker())
    print("Rivo API 18.1 Fixed OK")

@app.get("/admin/check-timeout-now")
async def check_timeout_now():
    now = datetime.now(timezone.utc)
    pending = supabase.table("rides").select("id,created_at,scheduled_time,seats_booked,total_seats").eq("status", "pending").execute().data or []
    return {"now_utc": now.isoformat(), "pending_count": len(pending), "rides": pending}

@app.put("/rides/{ride_id}/complete")
def complete_ride(ride_id: int, background_tasks: BackgroundTasks):
    supabase.table("rides").update({"status":"completed", "completed_at": datetime.now(timezone.utc).isoformat(), "passenger_status": "dropped"}).eq("id",ride_id).execute()
    background_tasks.add_task(archive_and_delete_ride, ride_id)
    return {"success":True}

@app.put("/rides/{ride_id}/drop-passenger")
def drop_passenger(ride_id: int, payload: PoolDropRequest, background_tasks: BackgroundTasks):
    res = supabase.table("rides").select("*").eq("id", ride_id).execute()
    if not res.data:
        raise HTTPException(404, "Ride nahi mili")
    ride = res.data[0]
    supabase.table("rides").update({"passenger_status": "dropped", "status": "completed", "completed_at": datetime.now(timezone.utc).isoformat()}).eq("id", ride_id).execute()
    background_tasks.add_task(archive_and_delete_ride, ride_id)
    gid = ride.get("pool_group_id")
    if gid:
        remaining = supabase.table("rides").select("id").eq("pool_group_id", gid).neq("status","completed").execute().data or []
        return {"success": True, "group_completed": len(remaining)==0, "remaining": len(remaining)}
    return {"success": True, "group_completed": True}

@app.put("/rides/{ride_id}/cancel")
def cancel_ride(ride_id: int, background_tasks: BackgroundTasks, user_id: str = Query(None)):
    supabase.table("rides").update({"status": "cancelled"}).eq("id", ride_id).execute()
    try:
        background_tasks.add_task(archive_and_delete_ride, ride_id)
    except:
        pass
    return {"success": True}

@app.put("/rides/{ride_id}/timeout")
def timeout_ride(ride_id: int, background_tasks: BackgroundTasks):
    supabase.table("rides").update({"status":"timeout"}).eq("id", ride_id).execute()
    background_tasks.add_task(archive_and_delete_ride, ride_id)
    return {"success": True}

@app.post("/drivers/login")
def driver_login(payload: DriverLoginRequest):
    raw_input = payload.phone or payload.mobile or payload.driver_id or payload.username or ""
    driver_input = str(raw_input).strip()
    password_input = str(payload.password or "").strip()
    if not driver_input:
        raise HTTPException(400, "Phone/ID required")
    res = supabase.table("drivers").select("*").or_(f"id.ilike.{driver_input},driver_id.ilike.{driver_input},phone.eq.{driver_input}").limit(1).execute()
    if not res.data:
        res = supabase.table("drivers").select("*").ilike("id", driver_input).limit(1).execute()
    if not res.data:
        res = supabase.table("drivers").select("*").ilike("driver_id", driver_input).limit(1).execute()
    if not res.data:
        res = supabase.table("drivers").select("*").eq("phone", driver_input).limit(1).execute()
    if not res.data:
        raise HTTPException(404, "Driver not found")
    driver = res.data[0]
    if str(driver.get('password','')).strip()!= password_input:
        raise HTTPException(401, "Wrong password")
    if payload.fcm_token:
        try:
            supabase.table("drivers").update({"fcm_token": payload.fcm_token, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", driver["id"]).execute()
        except:
            pass
    return driver

@app.get("/maps/directions")
async def get_directions_secure(origin: str = Query(...), dest: str = Query(...)):
    if not GOOGLE_MAPS_API_KEY:
        raise HTTPException(500, "GOOGLE_MAPS_API_KEY not set")
    url = f"https://maps.googleapis.com/maps/api/directions/json?origin={origin}&destination={dest}&key={GOOGLE_MAPS_API_KEY}&language=en&overview=full"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url)
        data = r.json()
        if data.get("status") == "OK":
            route = data["routes"][0]
            leg = route["legs"][0]
            return {"points": route["overview_polyline"]["points"], "distance_text": leg["distance"]["text"], "distance_value": leg["distance"]["value"], "duration_text": leg["duration"]["text"]}
        else:
            raise HTTPException(400, f"Google: {data.get('status')}")

@app.put("/drivers/{driver_id}/location")
async def update_driver_location(driver_id: str, payload: DriverLocationRequest):
    lat = payload.latitude if payload.latitude is not None else payload.lat
    lng = payload.longitude if payload.longitude is not None else payload.lng
    if lat is None or lng is None:
        raise HTTPException(400, "lat/lng required")
    d_res = supabase.table("drivers").select("id,range,vehicle_type").eq("id", driver_id).execute()
    if not d_res.data:
        d_res = supabase.table("drivers").select("id,range,vehicle_type").eq("driver_id", driver_id).execute()
    if not d_res.data:
        raise HTTPException(404, "Driver not found")
    real_id = d_res.data[0]["id"]
    supabase.table("drivers").update({"current_latitude": lat, "current_longitude": lng, "last_seen": datetime.now(timezone.utc).isoformat(), "updated_at": datetime.now(timezone.utc).isoformat(), "is_online": True}).eq("id", real_id).execute()
    return {"success": True}

@app.get("/rides/pending/list")
def pending_rides(vehicle_type: str = Query(None), driver_id: str = Query(None), driver_lat: float = Query(None), driver_lng: float = Query(None)):
    if not driver_id:
        raise HTTPException(400, "driver_id required")
    d_res = supabase.table("drivers").select("current_latitude, current_longitude, range, vehicle_type").eq("id", driver_id).execute()
    if not d_res.data:
        d_res = supabase.table("drivers").select("current_latitude, current_longitude, range, vehicle_type").eq("driver_id", driver_id).execute()
    if not d_res.data:
        raise HTTPException(404, "Driver not found")
    drv = d_res.data[0]
    d_lat = driver_lat if driver_lat is not None else drv.get("current_latitude")
    d_lng = driver_lng if driver_lng is not None else drv.get("current_longitude")
    d_vehicle = (vehicle_type or drv.get("vehicle_type") or "").strip()
    driver_range = get_range_by_vehicle(d_vehicle, drv.get("range"))
    q = supabase.table("rides").select("*").eq("status","pending").order("id", desc=True).limit(50)
    if d_vehicle:
        q = q.eq("vehicle_type", d_vehicle)
    rides = q.execute().data or []
    if d_lat is None or d_lng is None:
        for r in rides:
            r.pop("otp", None)
        return rides
    filtered = []
    for ride in rides:
        try:
            p_lat = float(ride.get("pickup_lat", 0))
            p_lng = float(ride.get("pickup_lng", 0))
            if p_lat==0 or p_lng==0:
                continue
            dist = haversine(float(d_lat), float(d_lng), p_lat, p_lng)
            if driver_range is None or dist <= float(driver_range):
                ride["distance_from_driver"] = round(dist, 2)
                ride.pop("otp", None)
                filtered.append(ride)
        except:
            continue
    filtered.sort(key=lambda x: x.get("distance_from_driver", 999))
    return filtered

@app.get("/vehicles")
def get_vehicles():
    try:
        res = supabase.table("vehicles").select("id, name, fare_per_km, night_fare_per_km, min_fare, parcel_per_km, parcel_night_per_km, parcel_min_fare, pool_per_km, pool_night_per_km, pool_min_fare, max_pool_seats, icon_path").order("id", desc=False).execute()
        return res.data or []
    except:
        return []

@app.get("/rides/{ride_id}")
def get_ride(ride_id: int):
    res = supabase.table("rides").select("*").eq("id", ride_id).execute()
    if not res.data:
        raise HTTPException(404, "Ride not found")
    return res.data[0]

@app.get("/rides/ongoing/{user_id}")
def get_ongoing(user_id: str):
    res = supabase.table("rides").select("*").eq("user_id", user_id).in_("status", ["pending","accepted","started","arrived"]).order("id", desc=True).limit(1).execute()
    if not res.data:
        return None
    return res.data[0]

@app.post("/rides")
async def create_ride(payload: RideCreateRequest, user_id: str=Query(...)):
    token, clean_id = generate_stringee_token(user_id, "new")
    final_trip = "parcel" if "parcel" in payload.trip_type.lower() else "pool" if "pool" in payload.trip_type.lower() else "ride"
    v_res = supabase.table("vehicles").select("*").ilike("name", payload.vehicle_type).limit(1).execute()
    if not v_res.data:
        v_res = supabase.table("vehicles").select("*").limit(1).execute()
    vehicle_row = v_res.data[0] if v_res.data else {}
    correct_fare, slot, rate = calculate_fare_by_type(vehicle_row, payload.distance, final_trip, payload.scheduled_time)
    max_seats = int(vehicle_row.get("max_pool_seats") or 3)
    seats_needed = int(payload.seats or 1)
    if seats_needed < 1:
        seats_needed = 1
    if seats_needed > max_seats:
        seats_needed = max_seats
    pool_group_id = f"POOL_{uuid.uuid4().hex[:6].upper()}" if final_trip=="pool" else None
    if final_trip == "pool":
        try:
            active_pools = supabase.table("rides").select("pool_group_id, pickup_lat, pickup_lng, drop_lat, drop_lng").eq("trip_type","pool").in_("status",["pending","accepted","started"]).eq("vehicle_type", payload.vehicle_type).limit(30).execute().data or []
            found_group = None
            for p in active_pools:
                gid = p.get("pool_group_id")
                if not gid:
                    continue
                try:
                    d_pick = haversine(payload.pickup_lat, payload.pickup_lng, float(p["pickup_lat"]), float(p["pickup_lng"]))
                    d_drop = haversine(payload.drop_lat, payload.drop_lng, float(p["drop_lat"]), float(p["drop_lng"]))
                    if d_pick <= 2.0 and d_drop <= 5.0:
                        group_rides = supabase.table("rides").select("seats_booked").eq("pool_group_id", gid).in_("status",["pending","accepted","started"]).execute().data or []
                        booked = sum([int(r.get("seats_booked") or 1) for r in group_rides])
                        left = max_seats - booked
                        if left >= seats_needed:
                            found_group = gid
                            break
                except:
                    continue
            if found_group:
                pool_group_id = found_group
        except Exception as e:
            print(f"Pool seat check error {e}")
        correct_fare = correct_fare * seats_needed
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
        "fare": correct_fare,
        "trip_type": final_trip,
        "status": "pending",
        "otp": payload.otp,
        "currency": "INR",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scheduled_time": payload.scheduled_time,
        "stringee_token": token,
        "stringee_user_id": clean_id,
        "pool_group_id": pool_group_id,
        "is_pool_ride": final_trip=="pool",
        "passenger_status": "waiting",
        "seats_booked": seats_needed,
        "total_seats": max_seats if final_trip=="pool" else 1,
        "max_pool_seats": max_seats if final_trip=="pool" else None,
    }
    res = supabase.table("rides").insert(ride_data).execute()
    if not res.data:
        raise HTTPException(500, "Failed")
    new_ride = res.data[0]
    await manager.broadcast_new_ride(new_ride)
    send_fcm_global(new_ride, payload.vehicle_type)
    return new_ride

@app.put("/rides/{ride_id}/accept")
async def accept_ride(ride_id: int, driver_id: str=Query(...)):
    d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type").eq("id",driver_id).execute()
    if not d_res.data:
        d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type").eq("driver_id",driver_id).execute()
    if not d_res.data:
        raise HTTPException(404, "Driver not found")
    driver = d_res.data[0]
    ride_update = {"driver_id": driver["id"], "status": "accepted", "driver_name": driver.get("name"), "driver_phone": driver.get("phone"), "vehicle_number": driver.get("vehicle_number"), "accepted_at": datetime.now(timezone.utc).isoformat()}
    updated = supabase.table("rides").update(ride_update).eq("id",ride_id).eq("status","pending").execute()
    if not updated.data:
        raise HTTPException(409, "Already taken")
    await manager.broadcast_ride_taken(ride_id)
    return {"success":True, "ride":updated.data[0]}

@app.get("/rides/pool/along-route/{active_ride_id}")
def get_pool_rides_along_route(active_ride_id: int, driver_id: str = Query(...), driver_lat: float = Query(None), driver_lng: float = Query(None)):
    active_res = supabase.table("rides").select("*").eq("id", active_ride_id).eq("driver_id", driver_id).in_("status", ["accepted","started"]).execute()
    if not active_res.data:
        return []
    active = active_res.data[0]
    curr_lat = driver_lat if driver_lat is not None else float(active.get("pickup_lat",0))
    curr_lng = driver_lng if driver_lng is not None else float(active.get("pickup_lng",0))
    a_drop_lat = float(active.get("drop_lat",0))
    a_drop_lng = float(active.get("drop_lng",0))
    group_id = active.get("pool_group_id")
    v_res = supabase.table("vehicles").select("max_pool_seats").ilike("name", active.get("vehicle_type","")).limit(1).execute()
    max_seats = int(v_res.data[0].get("max_pool_seats") or 3) if v_res.data else 3
    group_rides = supabase.table("rides").select("seats_booked").eq("pool_group_id", group_id).in_("status",["accepted","started"]).execute().data or []
    booked = sum([int(r.get("seats_booked") or 1) for r in group_rides])
    seats_left = max_seats - booked
    if seats_left <= 0:
        return []
    pending_res = supabase.table("rides").select("*").eq("trip_type","pool").eq("status","pending").eq("vehicle_type", active.get("vehicle_type")).limit(30).execute()
    along = []
    for r in pending_res.data or []:
        try:
            seats_needed = int(r.get("seats_booked") or 1)
            if seats_needed > seats_left:
                continue
            n_lat = float(r.get("pickup_lat",0))
            n_lng = float(r.get("pickup_lng",0))
            ok, d_on = is_point_along_route(curr_lat, curr_lng, a_drop_lat, a_drop_lng, n_lat, n_lng, 2.5)
            if ok:
                r["distance_from_driver"] = round(haversine(curr_lat,curr_lng,n_lat,n_lng),2)
                r["dist_on_route"] = round(d_on,2)
                r["seats_left"] = seats_left
                r["seats_needed"] = seats_needed
                r.pop("otp",None)
                along.append(r)
        except:
            continue
    along.sort(key=lambda x: x.get("dist_on_route",999))
    return along[:5]

@app.put("/rides/{ride_id}/accept-pool")
def accept_pool_along_route(ride_id: int, driver_id: str = Query(...), group_id: str = Query(...)):
    supabase.table("rides").update({"driver_id": driver_id, "pool_group_id": group_id, "status": "accepted", "passenger_status": "waiting", "accepted_at": datetime.now(timezone.utc).isoformat()}).eq("id", ride_id).eq("status","pending").execute()
    return {"success": True}

@app.post("/rides/{ride_id}/verify-otp")
def verify_ride_otp(ride_id: int, payload: OtpVerifyRequest):
    res = supabase.table("rides").select("id,otp,status").eq("id", ride_id).execute()
    if not res.data:
        raise HTTPException(404, "Ride not found")
    if str(res.data[0].get("otp","")).strip()!= str(payload.otp).strip():
        raise HTTPException(400, "Galat OTP")
    supabase.table("rides").update({"status": "started","started_at": datetime.now(timezone.utc).isoformat(), "passenger_status": "onboard"}).eq("id", ride_id).execute()
    return {"success": True}

@app.get("/drivers/{driver_id}/active-ride")
def get_active_ride(driver_id: str):
    res = supabase.table("rides").select("*").eq("driver_id", driver_id).in_("status", ["accepted", "started", "arrived"]).order("id", desc=True).limit(10).execute()
    if res.data:
        return {"active": True, "rides": res.data, "ride": res.data[0]}
    return {"active": False, "rides": [], "ride": None}

@app.websocket("/ws/drivers")
async def ws_drivers(ws: WebSocket, vehicle_type: str = Query(""), lat: float = Query(None), lng: float = Query(None), driver_id: str = Query(None)):
    drange = None
    if driver_id:
        try:
            d_res = supabase.table("drivers").select("range, vehicle_type").eq("id", driver_id).execute()
            if not d_res.data:
                d_res = supabase.table("drivers").select("range, vehicle_type").eq("driver_id", driver_id).execute()
            if d_res.data:
                drange = get_range_by_vehicle(d_res.data[0].get("vehicle_type"), d_res.data[0].get("range"))
                if not vehicle_type:
                    vehicle_type = d_res.data[0].get("vehicle_type","")
        except:
            pass
    await manager.connect_driver(ws, vehicle_type, lat, lng, drange, driver_id)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_driver(ws)

@app.websocket("/ws/ride/{ride_id}")
async def ws_ride(ws: WebSocket, ride_id: int):
    await manager.connect(ride_id, ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ride_id, ws)

@app.get("/")
def root():
    return {"status":"Rivo API 18.1 Fixed - total_seats OK"}

@app.get("/admin/archived")
def get_archived():
    try:
        res = supabase.table("rides_archive").select("*").order("archived_at", desc=True).limit(100).execute()
        return res.data
    except Exception as e:
        return {"error": str(e)}

