import os, json, time, jwt, uuid, math, httpx, asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, BackgroundTasks, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, messaging
from deep_translator import MyMemoryTranslator
import re

load_dotenv()

app = FastAPI(title="Rivo Taxi API - Final 23.0 Optimized (All Features Same)")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

STRINGEE_API_KEY_SID = os.getenv("STRINGEE_API_KEY_SID")
STRINGEE_API_KEY_SECRET = os.getenv("STRINGEE_API_KEY_SECRET")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "rivo_jaipur_2026_default")

try:
    fj = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if fj and not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(fj))
        firebase_admin.initialize_app(cred)
        print("Firebase OK")
except Exception as e:
    print(f"Firebase Error: {e}")

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status":"Rivo API 23.0 Optimized OK", "ok": True}

@app.api_route("/health", methods=["GET", "HEAD"])
def health_check():
    return {"status":"ok", "ok": True}

def to_hindi_free(text: str) -> str:
    if not text or not text.strip():
        return ""
    if re.search('[\u0900-\u097F]', text):
        return text
    try:
        result = MyMemoryTranslator(source='en-US', target='hi-IN').translate(text)
        if result and re.search('[\u0900-\u097F]', result):
            return result
    except Exception as e:
        print(f"Translate Error: {e}")
    return text

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

_vehicle_range_cache = {}
_all_vehicles_cache = []
_all_vehicles_cache_time = 0

def get_all_vehicle_names():
    global _all_vehicles_cache, _all_vehicles_cache_time, _vehicle_range_cache
    try:
        now = time.time()
        if now - _all_vehicles_cache_time > 300 or not _all_vehicles_cache:
            res = supabase.table("vehicles").select("name,range").execute()
            temp = []
            for r in (res.data or []):
                nm = str(r.get("name","")).lower().strip()
                if nm:
                    temp.append(nm)
                    if r.get("range"):
                        try:
                            _vehicle_range_cache[nm] = float(r.get("range"))
                        except:
                            pass
            _all_vehicles_cache = temp
            _all_vehicles_cache_time = now
    except Exception as e:
        print(f"Vehicle list load error: {e}")
    return _all_vehicles_cache

def normalize_vehicle_type(raw: str):
    raw = str(raw or "").lower().strip()
    if not raw:
        return ""
    all_v = get_all_vehicle_names()
    if not all_v:
        return raw
    if raw in all_v:
        return raw
    for v in all_v:
        if v in raw or raw in v:
            return v
    return raw

def get_range_from_vehicle_table(vehicle_type: str):
    vt = normalize_vehicle_type(vehicle_type)
    if not vt:
        return 5.0
    if vt in _vehicle_range_cache:
        return _vehicle_range_cache[vt]
    try:
        res = supabase.table("vehicles").select("range").ilike("name", vt).limit(1).execute()
        if res.data and res.data[0].get("range"):
            r = float(res.data[0].get("range"))
            if r > 0:
                _vehicle_range_cache[vt] = r
                return r
    except Exception as e:
        print(f"Range fetch error {vt}: {e}")
    return 5.0

def get_range_by_vehicle(vehicle_type: str, db_range=None):
    if db_range is not None:
        try:
            r = float(db_range)
            if r > 0:
                return r
        except:
            pass
    return get_range_from_vehicle_table(vehicle_type)

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

def is_point_along_route(curr_lat, curr_lng, drop_lat, drop_lng, new_lat, new_lng, max_dist_km=3.0):
    try:
        total_dist = haversine(curr_lat, curr_lng, drop_lat, drop_lng)
        d1 = haversine(curr_lat, curr_lng, new_lat, new_lng)
        d2 = haversine(new_lat, new_lng, drop_lat, drop_lng)
        if abs((d1 + d2) - total_dist) <= max_dist_km and d1 < total_dist and d1 > 0.2:
            return True, d1
        return False, 999
    except:
        return False, 999

def enrich_ride_with_driver(ride: dict):
    driver_id = ride.get("driver_id")
    if not driver_id:
        return ride
    try:
        d_res = supabase.table("drivers").select("name,phone,vehicle_number,vehicle_type,photo_url,current_latitude,current_longitude,stringee_user_id").or_(f"id.eq.{driver_id},driver_id.eq.{driver_id}").limit(1).execute()
        if d_res.data:
            d = d_res.data[0]
            ride["driver_name"] = d.get("name") or ride.get("driver_name")
            ride["driver_phone"] = d.get("phone") or ride.get("driver_phone")
            ride["vehicle_number"] = d.get("vehicle_number") or ride.get("vehicle_number")
            ride["vehicle_type"] = ride.get("vehicle_type") or d.get("vehicle_type")
            ride["driver_photo"] = d.get("photo_url")
            ride["photo_url"] = d.get("photo_url")
            ride["driver_photo_url"] = d.get("photo_url")
            ride["driver_stringee_user_id"] = d.get("stringee_user_id") or ride.get("driver_stringee_user_id")
            ride["driver_current_latitude"] = d.get("current_latitude")
            ride["driver_current_longitude"] = d.get("current_longitude")
    except Exception as e:
        print(f"enrich error {e}")
    return ride

def enrich_offers_with_photos(offers: list):
    if not offers:
        return offers
    try:
        driver_ids = list(set([o.get("driver_id") for o in offers if o.get("driver_id")]))
        if not driver_ids:
            return offers
        d_res = supabase.table("drivers").select("id,driver_id,photo_url,vehicle_number,name").in_("id", driver_ids).execute()
        d_res2 = supabase.table("drivers").select("id,driver_id,photo_url,vehicle_number,name").in_("driver_id", driver_ids).execute()
        all_d = (d_res.data or []) + (d_res2.data or [])
        d_map = {}
        for d in all_d:
            d_map[d.get("id")] = d
            d_map[d.get("driver_id")] = d
        for offer in offers:
            d = d_map.get(offer.get("driver_id"))
            if d:
                offer["driver_photo"] = d.get("photo_url")
                offer["photo_url"] = d.get("photo_url")
                offer["vehicle_number"] = offer.get("vehicle_number") or d.get("vehicle_number")
                offer["driver_name"] = d.get("name") or offer.get("driver_name")
    except Exception as e:
        print(f"enrich offers error {e}")
    return offers

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
                    await c.send_text(json.dumps(msg, default=str))
                except:
                    pass

    async def broadcast_new_offer(self, ride_id: int, offers: list):
        if ride_id in self.active_connections:
            for c in list(self.active_connections[ride_id]):
                try:
                    await c.send_text(json.dumps({"type":"new_driver_offer","ride_id":ride_id,"offers":offers}, default=str))
                except:
                    pass

    async def connect_driver(self, ws: WebSocket, vehicle_type: str = "", lat: float = None, lng: float = None, driver_range: float = None, driver_id: str = ""):
        await ws.accept()
        self.driver_connections = [d for d in self.driver_connections if d["ws"] != ws]
        if driver_id:
            self.driver_connections = [d for d in self.driver_connections if d.get("driver_id") != driver_id]
        self.driver_connections.append({
            "ws": ws,
            "vehicle_type": normalize_vehicle_type(vehicle_type),
            "lat": lat,
            "lng": lng,
            "range": driver_range,
            "driver_id": driver_id
        })

    def update_driver_location(self, driver_id: str, lat: float, lng: float):
        for d in self.driver_connections:
            if d.get("driver_id") == driver_id:
                d["lat"] = lat
                d["lng"] = lng

    def disconnect_driver(self, ws: WebSocket):
        self.driver_connections = [d for d in self.driver_connections if d["ws"] != ws]

    async def broadcast_new_ride(self, ride_data: dict):
        req_type = normalize_vehicle_type(ride_data.get('vehicle_type'))
        if not req_type:
            print("Broadcast: No vehicle type")
            return
        p_lat = float(ride_data.get('pickup_lat', 0) or 0)
        p_lng = float(ride_data.get('pickup_lng', 0) or 0)
        if p_lat == 0 or p_lng == 0:
            print("Broadcast: Invalid pickup lat/lng")
            return
        safe_ride = {k: v for k, v in ride_data.items() if k != 'otp'}
        dead = []
        for driver in list(self.driver_connections):
            ws = driver["ws"]
            driver_v = normalize_vehicle_type(driver.get("vehicle_type"))
            if not driver_v or driver_v != req_type:
                continue
            d_lat = driver.get("lat")
            d_lng = driver.get("lng")
            if d_lat is None or d_lng is None:
                continue
            try:
                d_lat_f = float(d_lat)
                d_lng_f = float(d_lng)
                if d_lat_f == 0 or d_lng_f == 0:
                    continue
                drange = get_range_from_vehicle_table(driver_v)
                dist = haversine(d_lat_f, d_lng_f, p_lat, p_lng)
                if dist > float(drange):
                    continue
            except Exception as e:
                print(f"Broadcast distance error: {e}")
                continue
            try:
                await ws.send_text(json.dumps({"type":"new_ride_alert","ride":safe_ride}, default=str))
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
        v_type = normalize_vehicle_type(vehicle_type or ride_data.get('vehicle_type'))
        if not v_type:
            print("FCM: No vehicle type")
            return
        all_drivers = supabase.table("drivers").select("fcm_token,vehicle_type,current_latitude,current_longitude,is_active,is_online").eq("is_online", True).neq("fcm_token", "").execute().data or []
        tokens = []
        p_lat = float(ride_data.get('pickup_lat', 0) or 0)
        p_lng = float(ride_data.get('pickup_lng', 0) or 0)
        for d in all_drivers:
            if d.get('is_active') == False:
                continue
            d_v = normalize_vehicle_type(d.get('vehicle_type'))
            if not d_v or d_v != v_type:
                continue
            d_lat = d.get('current_latitude')
            d_lng = d.get('current_longitude')
            if d_lat is None or d_lng is None:
                continue
            try:
                if float(d_lat) == 0 or float(d_lng) == 0:
                    continue
                drange = get_range_from_vehicle_table(d_v)
                dist = haversine(float(d_lat), float(d_lng), p_lat, p_lng)
                if dist > float(drange):
                    continue
            except:
                continue
            if d.get('fcm_token'):
                tokens.append(d['fcm_token'])
        tokens = list(set(tokens))
        if not tokens:
            print(f"No tokens for {v_type} - कोई driver range में नहीं")
            return
        trip = str(ride_data.get('trip_type','ride')).lower()
        seats_info = ""
        if "pool" in trip:
            seats_info = f" Seats:{ride_data.get('seats_booked',1)}/{ride_data.get('total_seats',3)}"
        if "parcel" in trip:
            title = f"PARCEL {v_type.upper()}!"
        elif "pool" in trip:
            title = f"POOL {v_type.upper()}!{seats_info}"
        else:
            title = f"RIDE {v_type.upper()}!"
        body = f"[{trip.upper()}{seats_info}] {ride_data.get('pickup_address','')[:40]} -> {ride_data.get('drop_address','')[:25]} | ₹{ride_data.get('fare','')}"
        android_notif = messaging.AndroidNotification(channel_id='ride_channel_v6_final', priority='max', visibility='public')
        android_config = messaging.AndroidConfig(priority='high', notification=android_notif)
        apns_config = messaging.APNSConfig(payload=messaging.APNSPayload(aps=messaging.Aps(sound='default', badge=1, content_available=True)))
        safe_ride = {k: v for k, v in ride_data.items() if k!= 'otp'}
        for token in tokens:
            msg = messaging.Message(token=token, notification=messaging.Notification(title=title, body=body), data={'vehicle_type': v_type, 'trip_type': trip, 'ride_id': str(ride_data.get('id','')), 'type': 'new_ride_alert', 'fare': str(ride_data.get('fare','')), 'click_action': 'FLUTTER_NOTIFICATION_CLICK', 'ride_json': json.dumps(safe_ride, default=str)}, android=android_config, apns=apns_config)
            try:
                messaging.send(msg)
            except Exception as e:
                print(f"FCM fail {e}")
                continue
        print(f"FCM Alert sent to {len(tokens)} drivers for {v_type}")
    except Exception as e:
        print(f"FCM Error {e}")

class RideCreateRequest(BaseModel):
    pickup_lat: float; pickup_lng: float; drop_lat: float; drop_lng: float
    pickup_address: Optional[str]=None; pickup_address_hi: Optional[str]=None; drop_address: Optional[str]=None; drop_address_hi: Optional[str]=None
    vehicle_type: str="Mini"; distance: float=0; fare: float=0; scheduled_time: Optional[str]=None; trip_type: str="ride"; otp: str="1234"; seats: int=1
    fcm_token: Optional[str]=None; rider_fcm_token: Optional[str]=None; rider_name: Optional[str]=None; rider_phone: Optional[str]=None; rider_id: Optional[str]=None; device_id: Optional[str]=None
class DriverLoginRequest(BaseModel):
    driver_id: Optional[str]=None; phone: Optional[str]=None; password: str=""; fcm_token: Optional[str]=None; mobile: Optional[str]=None; username: Optional[str]=None
class OtpVerifyRequest(BaseModel):
    otp: str
class DriverLocationRequest(BaseModel):
    lat: Optional[float]=None; lng: Optional[float]=None; latitude: Optional[float]=None; longitude: Optional[float]=None
class DriverOfferRequest(BaseModel):
    driver_id: str; driver_name: Optional[str]=None; offered_fare: float; vehicle_type: Optional[str]=None
class AcceptOfferRequest(BaseModel):
    driver_id: str; offered_fare: float
class PoolDropRequest(BaseModel):
    driver_id: str
class CompleteRideRequest(BaseModel):
    total_km_driven: Optional[float]=0; actual_km: Optional[float]=0; actual_distance: Optional[float]=0
class CallNotifyRequest(BaseModel):
    rider_user_id: str; driver_name: str; driver_id: str
class UserFcmRequest(BaseModel):
    user_id: str; fcm_token: str; device_id: Optional[str]=None

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
        res = supabase.table("rides").select("*").eq("id", ride_id).execute()
        if not res.data:
            return
        ride_data = res.data[0]
        original_id = ride_data.get('id')
        ride_data.pop('id', None)
        ride_data['original_ride_id'] = original_id
        ride_data['archived_at'] = datetime.now(timezone.utc).isoformat()
        try:
            supabase.table("rides_archive").insert(ride_data).execute()
        except:
            pass
        try:
            supabase.table("rides").delete().eq("id", int(original_id)).execute()
            supabase.table("driver_offers").delete().eq("ride_id", int(original_id)).execute()
        except:
            pass
    except Exception as e:
        print(f"Archive Error {e}")

TIMEOUT_MINUTES = 10
is_timeout_running = False
async def auto_timeout_checker():
    await asyncio.sleep(10)
    global is_timeout_running
    while True:
        if is_timeout_running:
            await asyncio.sleep(30)
            continue
        is_timeout_running = True
        try:
            now = datetime.now(timezone.utc)
            pending_res = await asyncio.to_thread(lambda: supabase.table("rides").select("id,created_at,scheduled_time,status").in_("status", ["pending","searching"]).limit(20).execute())
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
                                should_timeout = True
                        except:
                            pass
                if should_timeout:
                    try:
                        await asyncio.to_thread(lambda rid=ride_id: supabase.table("rides").update({"status": "timeout"}).eq("id", rid).execute())
                        await manager.broadcast_ride_taken(ride_id)
                    except:
                        pass
        except Exception as e:
            print(f"auto_timeout error {e}")
        finally:
            is_timeout_running = False
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_timeout_checker())
    print("Rivo API 23.0 Optimized OK")

@app.get("/vehicles")
def get_vehicles(country_code: str = Query(None)):
    try:
        q = supabase.table("vehicles").select("*").order("name", desc=False)
        if country_code:
            q = q.eq("country_code", country_code.lower())
        res = q.execute()
        return res.data or []
    except Exception as e:
        return []

@app.get("/rides/pending/list")
def pending_rides(vehicle_type: str = Query(None), driver_id: str = Query(None), driver_lat: float = Query(None), driver_lng: float = Query(None)):
    if not driver_id:
        raise HTTPException(400, "driver_id required")
    d_res = supabase.table("drivers").select("current_latitude, current_longitude, vehicle_type").or_(f"id.eq.{driver_id},driver_id.eq.{driver_id}").limit(1).execute()
    if not d_res.data:
        raise HTTPException(404, "Driver not found")
    drv = d_res.data[0]
    d_lat = driver_lat if driver_lat is not None else drv.get("current_latitude")
    d_lng = driver_lng if driver_lng is not None else drv.get("current_longitude")
    d_vehicle = (vehicle_type or drv.get("vehicle_type") or "").strip()
    driver_range = get_range_from_vehicle_table(d_vehicle)
    q = supabase.table("rides").select("*").in_("status",["pending","searching"]).order("id", desc=True).limit(50)
    if d_vehicle:
        q = q.eq("vehicle_type", d_vehicle)
    rides = q.execute().data or []
    if d_lat is None or d_lng is None:
        return []
    filtered = []
    for ride in rides:
        try:
            p_lat = float(ride.get("pickup_lat", 0))
            p_lng = float(ride.get("pickup_lng", 0))
            if p_lat==0 or p_lng==0:
                continue
            dist = haversine(float(d_lat), float(d_lng), p_lat, p_lng)
            if dist <= float(driver_range):
                ride["distance_from_driver"] = round(dist, 2)
                ride.pop("otp", None)
                filtered.append(ride)
        except:
            continue
    filtered.sort(key=lambda x: x.get("distance_from_driver", 999))
    return filtered

@app.post("/rides")
async def create_ride(payload: RideCreateRequest, user_id: str = Query(...)):
    token, clean_id = generate_stringee_token(user_id, "new")
    canonical_v = normalize_vehicle_type(payload.vehicle_type)
    v_res = supabase.table("vehicles").select("*").ilike("name", canonical_v).limit(1).execute()
    vehicle_row = v_res.data[0] if v_res.data else {"name": canonical_v, "fare_per_km": 10.0, "min_fare": 50.0, "pool_per_km": 6.0, "pool_min_fare": 30.0, "parcel_per_km": 8.0, "parcel_min_fare": 50.0, "max_pool_seats": 3}
    correct_fare, slot, rate = calculate_fare_by_type(vehicle_row, payload.distance, payload.trip_type, payload.scheduled_time)
    ride_data = {
        "user_id": user_id, "pickup_lat": payload.pickup_lat, "pickup_lng": payload.pickup_lng,
        "drop_lat": payload.drop_lat, "drop_lng": payload.drop_lng,
        "pickup_address": payload.pickup_address, "drop_address": payload.drop_address,
        "vehicle_type": canonical_v, "distance": payload.distance, "fare": correct_fare,
        "trip_type": payload.trip_type, "status": "pending", "otp": payload.otp,
        "stringee_token": token, "stringee_user_id": clean_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    res = supabase.table("rides").insert(ride_data).execute()
    new_ride = res.data[0]
    await manager.broadcast_new_ride(new_ride)
    send_fcm_global(new_ride, canonical_v)
    return new_ride

@app.websocket("/ws/drivers")
async def ws_drivers(ws: WebSocket, vehicle_type: str = Query(""), lat: float = Query(None), lng: float = Query(None), driver_id: str = Query(None)):
    db_vehicle = ""
    if driver_id:
        try:
            d_res = supabase.table("drivers").select("vehicle_type, current_latitude, current_longitude").or_(f"id.eq.{driver_id},driver_id.eq.{driver_id}").limit(1).execute()
            if d_res.data:
                row = d_res.data[0]
                db_vehicle = str(row.get("vehicle_type") or "").lower().strip()
                if lat is None:
                    lat = row.get("current_latitude")
                if lng is None:
                    lng = row.get("current_longitude")
        except:
            pass
    final_v = normalize_vehicle_type(vehicle_type or db_vehicle)
    drange = get_range_from_vehicle_table(final_v)
    await manager.connect_driver(ws, final_v, lat, lng, drange, driver_id)
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

@app.get("/rides/{ride_id}")
def get_ride(ride_id: int):
    res = supabase.table("rides").select("*").eq("id", ride_id).execute()
    if not res.data:
        raise HTTPException(404, "Ride not found")
    return enrich_ride_with_driver(res.data[0])

@app.get("/admin/stats")
def get_admin_stats():
    try:
        pending = supabase.table("rides").select("id", count="exact").eq("status", "pending").execute()
        return {"pending_rides": pending.count or 0}
    except Exception as e:
        return {"error": str(e)}