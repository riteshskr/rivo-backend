import os, json, time, jwt, uuid, math, httpx, asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, messaging

load_dotenv()
app = FastAPI(title="Rivo Taxi API - Final 22.0 With Driver Photo")

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

# ===== DRIVER PHOTO ENRICH FUNCTION - NAYA =====
def enrich_ride_with_driver(ride: dict):
    driver_id = ride.get("driver_id")
    if not driver_id:
        return ride
    try:
        d_res = supabase.table("drivers").select("name,phone,vehicle_number,vehicle_type,photo_url,current_latitude,current_longitude").eq("id", driver_id).execute()
        if not d_res.data:
            d_res = supabase.table("drivers").select("name,phone,vehicle_number,vehicle_type,photo_url,current_latitude,current_longitude").eq("driver_id", driver_id).execute()
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
    async def broadcast_new_offer(self, ride_id: int, offers: list):
        if ride_id in self.active_connections:
            for c in list(self.active_connections[ride_id]):
                try:
                    await c.send_text(json.dumps({"type":"new_driver_offer","ride_id":ride_id,"offers":offers}))
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
        all_drivers = supabase.table("drivers").select("fcm_token,vehicle_type,current_latitude,current_longitude,range,is_active").eq("is_online", True).neq("fcm_token", "").execute().data or []
        tokens = []
        p_lat = float(ride_data.get('pickup_lat', 0))
        p_lng = float(ride_data.get('pickup_lng', 0))
        for d in all_drivers:
            if d.get('is_active') == False:
                continue
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

class DriverOfferRequest(BaseModel):
    driver_id: str
    driver_name: Optional[str]=None
    offered_fare: float
    vehicle_type: Optional[str]=None

class AcceptOfferRequest(BaseModel):
    driver_id: str
    offered_fare: float

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
        try:
            supabase.table("driver_offers").delete().eq("ride_id", int(original_id)).execute()
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
                        try:
                            supabase.table("driver_offers").delete().eq("ride_id", ride_id).execute()
                        except:
                            pass
                        archive_and_delete_ride(ride_id)
                        await manager.broadcast_ride_taken(ride_id)
                    except:
                        pass
            try:
                rider_acc_res = supabase.table("rides").select("id,offer_expires_at,driver_id").eq("status", "rider_accepted").limit(50).execute()
                for r in rider_acc_res.data or []:
                    try:
                        exp_str = r.get("offer_expires_at")
                        if exp_str:
                            exp_dt = datetime.fromisoformat(str(exp_str).replace('Z','+00:00'))
                            if exp_dt.tzinfo is None:
                                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                            if now >= exp_dt:
                                print(f"Offer Expired for ride {r['id']}")
                                supabase.table("rides").update({"status":"pending", "driver_id": None, "driver_name": None, "driver_phone": None, "vehicle_number": None, "offer_expires_at": None}).eq("id", r["id"]).execute()
                                try:
                                    supabase.table("driver_offers").delete().eq("ride_id", r["id"]).execute()
                                except:
                                    pass
                                try:
                                    new_data = supabase.table("rides").select("*").eq("id", r["id"]).execute().data
                                    if new_data:
                                        await manager.broadcast_new_ride(new_data[0])
                                        send_fcm_global(new_data[0], new_data[0].get('vehicle_type',''))
                                except:
                                    pass
                    except Exception as e:
                        print(f"rider_accepted timeout error {e}")
            except Exception as e:
                print(f"rider_accepted check error {e}")
        except:
            pass
        await asyncio.sleep(30)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_timeout_checker())
    print("Rivo API 22.0 With Driver Photo OK")

@app.get("/admin/check-timeout-now")
async def check_timeout_now():
    now = datetime.now(timezone.utc)
    pending = supabase.table("rides").select("id,created_at,scheduled_time,seats_booked,total_seats,pool_group_id,status").in_("status", ["pending","rider_accepted"]).execute().data or []
    return {"now_utc": now.isoformat(), "pending_count": len(pending), "rides": pending}

@app.get("/")
def root():
    return {"status":"Rivo API 22.0 With Driver Photo OK"}

# ========== BIDDING SYSTEM ==========
@app.put("/rides/{ride_id}/offer")
async def driver_make_offer(ride_id: int, payload: DriverOfferRequest):
    r_res = supabase.table("rides").select("id,status").eq("id", ride_id).execute()
    if not r_res.data:
        raise HTTPException(404, "Ride नहीं मिली")
    if r_res.data[0].get("status")!= "pending":
        raise HTTPException(400, "Ride pending में नहीं है")
    busy = supabase.table("rides").select("id").eq("driver_id", payload.driver_id).in_("status", ["accepted","started","arrived","rider_accepted"]).limit(1).execute()
    if busy.data:
        same = supabase.table("rides").select("id").eq("id", ride_id).eq("driver_id", payload.driver_id).execute()
        if not same.data:
            raise HTTPException(409, "आप पहले से एक Ride पर हैं")
    offer_data = {
        "ride_id": ride_id,
        "driver_id": payload.driver_id,
        "driver_name": payload.driver_name or payload.driver_id,
        "vehicle_type": payload.vehicle_type or "Mini",
        "offered_fare": float(payload.offered_fare),
        "status": "pending"
    }
    supabase.table("driver_offers").upsert(offer_data, on_conflict="ride_id,driver_id").execute()
    offers_res = supabase.table("driver_offers").select("*").eq("ride_id", ride_id).eq("status","pending").order("offered_fare", desc=False).execute()
    offers = offers_res.data or []
    # enrich offers with photo
    for offer in offers:
        try:
            d_id = offer.get("driver_id")
            d_res = supabase.table("drivers").select("photo_url,vehicle_number,vehicle_type,name").eq("id", d_id).execute()
            if not d_res.data:
                d_res = supabase.table("drivers").select("photo_url,vehicle_number,vehicle_type,name").eq("driver_id", d_id).execute()
            if d_res.data:
                offer["driver_photo"] = d_res.data[0].get("photo_url")
                offer["photo_url"] = d_res.data[0].get("photo_url")
                offer["vehicle_number"] = offer.get("vehicle_number") or d_res.data[0].get("vehicle_number")
                offer["driver_name"] = d_res.data[0].get("name") or offer.get("driver_name")
        except:
            pass
    await manager.broadcast_new_offer(ride_id, offers)
    await manager.broadcast(ride_id, {"type":"new_driver_offer","ride_id":ride_id,"offers":offers})
    return {"success": True, "offers": offers, "count": len(offers)}

@app.get("/rides/{ride_id}/offers")
def get_ride_offers(ride_id: int):
    res = supabase.table("driver_offers").select("*").eq("ride_id", ride_id).eq("status","pending").order("offered_fare", desc=False).limit(50).execute()
    offers = res.data or []
    for offer in offers:
        try:
            d_id = offer.get("driver_id")
            d_res = supabase.table("drivers").select("photo_url,vehicle_number,vehicle_type,name").eq("id", d_id).execute()
            if not d_res.data:
                d_res = supabase.table("drivers").select("photo_url,vehicle_number,vehicle_type,name").eq("driver_id", d_id).execute()
            if d_res.data:
                offer["driver_photo"] = d_res.data[0].get("photo_url")
                offer["photo_url"] = d_res.data[0].get("photo_url")
                offer["vehicle_number"] = d_res.data[0].get("vehicle_number")
                offer["driver_name"] = d_res.data[0].get("name") or offer.get("driver_name")
        except:
            pass
    return {"ride_id": ride_id, "offers": offers, "count": len(offers)}

@app.put("/rides/{ride_id}/accept-offer")
async def rider_accept_offer(ride_id: int, payload: AcceptOfferRequest):
    d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type,fcm_token,photo_url,stringee_user_id").eq("id", payload.driver_id).execute()
    if not d_res.data:
        d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type,fcm_token,photo_url,stringee_user_id").eq("driver_id", payload.driver_id).execute()
    if not d_res.data:
        raise HTTPException(404, "Driver not found")
    driver = d_res.data[0]
    busy = supabase.table("rides").select("id").eq("driver_id", driver["id"]).in_("status", ["accepted","started","arrived","rider_accepted"]).limit(1).execute()
    if busy.data:
        raise HTTPException(409, "Driver दूसरी Ride पर है, दूसरा Offer चुनो")
    updated = supabase.table("rides").update({
        "driver_id": driver["id"],
        "driver_name": driver.get("name"),
        "driver_phone": driver.get("phone"),
        "vehicle_number": driver.get("vehicle_number"),
        "fare": float(payload.offered_fare),
        "status": "rider_accepted",
        "rider_accepted_at": datetime.now(timezone.utc).isoformat(),
        "offer_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(),
    }).eq("id", ride_id).eq("status","pending").execute()
    if not updated.data:
        raise HTTPException(409, "Ride पहले ही ले ली गई")
    ride_data = updated.data[0]
    ride_data = enrich_ride_with_driver(ride_data)
    for d in list(manager.driver_connections):
        if d.get("driver_id") == driver["id"] or d.get("driver_id") == payload.driver_id:
            try:
                await d["ws"].send_text(json.dumps({
                    "type": "rider_accepted_your_offer",
                    "ride_id": ride_id,
                    "ride": ride_data,
                    "offered_fare": float(payload.offered_fare),
                    "expires_in": 120
                }))
            except:
                pass
    try:
        if driver.get("fcm_token"):
            msg = messaging.Message(
                notification=messaging.Notification(title="Offer Accepted! 🎉", body=f"Rider ने आपका ₹{payload.offered_fare} वाला Offer Accept किया, 2 मिनट में Confirm करो"),
                data={"type": "rider_accepted_your_offer", "ride_id": str(ride_id), "fare": str(payload.offered_fare)},
                token=driver.get("fcm_token")
            )
            messaging.send(msg)
    except Exception as e:
        print(f"FCM offer accept error {e}")
    await manager.broadcast_ride_taken(ride_id)
    return {"success": True, "ride": ride_data, "message": "Driver confirmation का इंतजार है (2 min)"}

@app.put("/rides/{ride_id}/driver-final-accept")
async def driver_final_accept(ride_id: int, driver_id: str = Query(...)):
    r_res = supabase.table("rides").select("*").eq("id", ride_id).eq("status", "rider_accepted").eq("driver_id", driver_id).execute()
    if not r_res.data:
        raise HTTPException(409, "Time Out! Ride वापस Pending में चली गई")
    ride = r_res.data[0]
    try:
        exp_str = ride.get("offer_expires_at")
        if exp_str:
            exp_dt = datetime.fromisoformat(str(exp_str).replace('Z','+00:00'))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp_dt:
                supabase.table("rides").update({"status":"pending", "driver_id": None, "driver_name": None, "driver_phone": None, "vehicle_number": None, "offer_expires_at": None}).eq("id", ride_id).execute()
                supabase.table("driver_offers").delete().eq("ride_id", ride_id).execute()
                raise HTTPException(409, "2 मिनट खत्म, Ride Expire हो गई")
    except HTTPException:
        raise
    except Exception as e:
        print(f"final accept time check error {e}")
    driver_token, driver_clean_id = generate_stringee_token(driver_id, str(ride_id))
    updated = supabase.table("rides").update({
        "status": "accepted",
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "driver_stringee_token": driver_token,
        "driver_stringee_user_id": driver_clean_id,
        "offer_expires_at": None
    }).eq("id", ride_id).execute()
    if not updated.data:
        raise HTTPException(409, "Failed")
    try:
        supabase.table("driver_offers").delete().eq("ride_id", ride_id).execute()
    except:
        pass
    await manager.broadcast_ride_taken(ride_id)
    ride_final = enrich_ride_with_driver(updated.data[0])
    return {"success": True, "ride": ride_final}

@app.put("/rides/{ride_id}/driver-reject-offer")
async def driver_reject_offer(ride_id: int, driver_id: str = Query(...)):
    supabase.table("rides").update({
        "status":"pending",
        "driver_id": None,
        "driver_name": None,
        "driver_phone": None,
        "vehicle_number": None,
        "offer_expires_at": None,
        "rider_accepted_at": None
    }).eq("id", ride_id).eq("status","rider_accepted").eq("driver_id", driver_id).execute()
    supabase.table("driver_offers").delete().eq("ride_id", ride_id).eq("driver_id", driver_id).execute()
    remaining_res = supabase.table("driver_offers").select("*").eq("ride_id", ride_id).eq("status","pending").order("offered_fare", desc=False).execute()
    remaining_offers = remaining_res.data or []
    for offer in remaining_offers:
        try:
            d_id = offer.get("driver_id")
            d_res = supabase.table("drivers").select("photo_url,vehicle_number,name").eq("id", d_id).execute()
            if not d_res.data:
                d_res = supabase.table("drivers").select("photo_url,vehicle_number,name").eq("driver_id", d_id).execute()
            if d_res.data:
                offer["driver_photo"] = d_res.data[0].get("photo_url")
                offer["photo_url"] = d_res.data[0].get("photo_url")
        except:
            pass
    await manager.broadcast(ride_id, {
        "type": "driver_rejected",
        "ride_id": ride_id,
        "rejected_driver_id": driver_id,
        "message": "Driver Busy है, दूसरा Offer चुनो",
        "offers": remaining_offers
    })
    try:
        new_data = supabase.table("rides").select("*").eq("id", ride_id).execute().data
        if new_data:
            await manager.broadcast_new_ride(new_data[0])
            send_fcm_global(new_data[0], new_data[0].get('vehicle_type',''))
    except Exception as e:
        print(f"broadcast new ride error {e}")
    return {"success": True, "message": "Rejected, Ride वापस Pending में", "offers": remaining_offers}

# ========== EXISTING APIS ==========
@app.put("/rides/{ride_id}/complete")
def complete_ride(ride_id: int, background_tasks: BackgroundTasks):
    supabase.table("rides").update({"status":"completed", "completed_at": datetime.now(timezone.utc).isoformat(), "passenger_status": "dropped"}).eq("id",ride_id).execute()
    background_tasks.add_task(archive_and_delete_ride, ride_id)
    return {"success":True}

@app.put("/rides/{ride_id}/drop-complete")
def drop_single_pool_ride(ride_id: int, group_id: str = Query(None), driver_id: str = Query(None), background_tasks: BackgroundTasks = None):
    res = supabase.table("rides").select("*").eq("id", ride_id).execute()
    if not res.data:
        raise HTTPException(404, "Ride नहीं मिली")
    ride = res.data[0]
    gid = group_id or ride.get("pool_group_id")
    supabase.table("rides").update({"status": "completed","passenger_status": "dropped","completed_at": datetime.now(timezone.utc).isoformat(),}).eq("id", ride_id).execute()
    if background_tasks is not None:
        background_tasks.add_task(archive_and_delete_ride, ride_id)
    else:
        try:
            archive_and_delete_ride(ride_id)
        except Exception:
            pass
    booked = 0
    total = 4
    if gid:
        remaining_res = supabase.table("rides").select("seats_booked,total_seats,max_pool_seats").eq("pool_group_id", gid).in_("status", ["accepted", "started"]).execute()
        remaining_data = remaining_res.data or []
        booked = sum([int(r.get("seats_booked") or 1) for r in remaining_data])
        if remaining_data:
            total = int(remaining_data[0].get("total_seats") or remaining_data[0].get("max_pool_seats") or 4)
    return {"success": True,"ride_id": ride_id,"status": "completed","booked_seats": booked,"total_seats": total,"left_seats": total - booked}

@app.get("/rides/pool/group/{group_id}/driver/{driver_id}")
def get_my_pooled_rides(group_id: str, driver_id: str):
    res = supabase.table("rides").select("*").eq("pool_group_id", group_id).eq("driver_id", driver_id).in_("status", ["accepted","started","arrived","pending","rider_accepted"]).order("id", desc=False).execute()
    rides = res.data or []
    active = rides
    booked = sum([int(r.get("seats_booked") or 1) for r in active])
    total = 4
    if active:
        total = int(active[0].get("total_seats") or active[0].get("max_pool_seats") or 4)
    farthest = None
    max_dist = 0
    if active:
        first_pickup_lat = float(active[0].get("pickup_lat",0))
        first_pickup_lng = float(active[0].get("pickup_lng",0))
        for r in active:
            try:
                d_lat = float(r.get("drop_lat",0))
                d_lng = float(r.get("drop_lng",0))
                dist = haversine(first_pickup_lat, first_pickup_lng, d_lat, d_lng)
                if dist > max_dist:
                    max_dist = dist
                    farthest = {"lat": d_lat, "lng": d_lng, "ride_id": r["id"]}
            except:
                pass
    return {"rides": active,"booked_seats": booked,"total_seats": total,"left_seats": total - booked,"farthest_drop": farthest,"active_count": len(active),"total_count": len(active)}

@app.put("/rides/{ride_id}/cancel")
def cancel_ride(ride_id: int, background_tasks: BackgroundTasks, user_id: str = Query(None)):
    supabase.table("rides").update({"status": "cancelled"}).eq("id", ride_id).execute()
    try:
        supabase.table("driver_offers").delete().eq("ride_id", ride_id).execute()
    except:
        pass
    try:
        background_tasks.add_task(archive_and_delete_ride, ride_id)
    except:
        pass
    return {"success": True}

@app.put("/rides/{ride_id}/timeout")
def timeout_ride(ride_id: int, background_tasks: BackgroundTasks):
    supabase.table("rides").update({"status":"timeout"}).eq("id", ride_id).execute()
    try:
        supabase.table("driver_offers").delete().eq("ride_id", ride_id).execute()
    except:
        pass
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
    if driver.get('is_active') == False:
        raise HTTPException(403, "आपका Account Admin ने Pause किया है")
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
    return enrich_ride_with_driver(res.data[0])

@app.get("/rides/ongoing/{user_id}")
def get_ongoing(user_id: str):
    res = supabase.table("rides").select("*").eq("user_id", user_id).in_("status", ["pending","accepted","started","arrived","rider_accepted"]).order("id", desc=True).limit(1).execute()
    if not res.data:
        return None
    return enrich_ride_with_driver(res.data[0])

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
    d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type,photo_url").eq("id",driver_id).execute()
    if not d_res.data:
        d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type,photo_url").eq("driver_id",driver_id).execute()
    if not d_res.data:
        raise HTTPException(404, "Driver not found")
    driver = d_res.data[0]
    driver_token, driver_clean_id = generate_stringee_token(driver["id"], str(ride_id))
    ride_update = {
        "driver_id": driver["id"],
        "status": "accepted",
        "driver_name": driver.get("name"),
        "driver_phone": driver.get("phone"),
        "vehicle_number": driver.get("vehicle_number"),
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "driver_stringee_token": driver_token,
        "driver_stringee_user_id": driver_clean_id
    }
    updated = supabase.table("rides").update(ride_update).eq("id",ride_id).eq("status","pending").execute()
    if not updated.data:
        raise HTTPException(409, "Already taken")
    try:
        supabase.table("driver_offers").delete().eq("ride_id", ride_id).execute()
    except:
        pass
    await manager.broadcast_ride_taken(ride_id)
    final_ride = enrich_ride_with_driver(updated.data[0])
    return {"success":True, "ride":final_ride}

@app.put("/rides/{ride_id}/accept-pool")
def accept_pool_along_route(ride_id: int, driver_id: str = Query(...), group_id: str = Query(...)):
    d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type,photo_url").eq("id",driver_id).execute()
    if not d_res.data:
        d_res = supabase.table("drivers").select("id,name,phone,vehicle_number,vehicle_type,photo_url").eq("driver_id",driver_id).execute()
    if not d_res.data:
        raise HTTPException(404, "Driver not found")
    driver = d_res.data[0]
    group_rides = supabase.table("rides").select("seats_booked,total_seats,max_pool_seats").eq("pool_group_id", group_id).in_("status",["accepted","started"]).execute().data or []
    booked = sum([int(r.get("seats_booked") or 1) for r in group_rides])
    max_seats = int(group_rides[0].get("total_seats") or group_rides[0].get("max_pool_seats") or 4) if group_rides else 4
    driver_token, driver_clean_id = generate_stringee_token(driver["id"], str(ride_id))
    res = supabase.table("rides").update({
        "driver_id": driver["id"],
        "driver_name": driver.get("name"),
        "driver_phone": driver.get("phone"),
        "vehicle_number": driver.get("vehicle_number"),
        "vehicle_type": driver.get("vehicle_type"),
        "pool_group_id": group_id,
        "status": "accepted",
        "passenger_status": "waiting",
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "driver_stringee_token": driver_token,
        "driver_stringee_user_id": driver_clean_id
    }).eq("id", ride_id).eq("status","pending").execute()
    if not res.data:
        raise HTTPException(409, "Already taken or seat full")
    try:
        supabase.table("driver_offers").delete().eq("ride_id", ride_id).execute()
    except:
        pass
    final_ride = enrich_ride_with_driver(res.data[0])
    return {"success": True, "ride": final_ride, "booked": booked + int(res.data[0].get("seats_booked") or 1), "total": max_seats}

@app.get("/rides/pool/along-route/{active_ride_id}")
def get_pool_rides_along_route(active_ride_id: int, driver_id: str = Query(...), driver_lat: float = Query(None), driver_lng: float = Query(None), farthest_drop_lat: float = Query(None), farthest_drop_lng: float = Query(None)):
    active_res = supabase.table("rides").select("*").eq("id", active_ride_id).eq("driver_id", driver_id).in_("status", ["accepted","started"]).execute()
    if not active_res.data:
        return []
    active = active_res.data[0]
    curr_lat = driver_lat if driver_lat is not None else float(active.get("pickup_lat",0))
    curr_lng = driver_lng if driver_lng is not None else float(active.get("pickup_lng",0))
    if farthest_drop_lat is not None and farthest_drop_lng is not None:
        a_drop_lat = farthest_drop_lat
        a_drop_lng = farthest_drop_lng
    else:
        group_id = active.get("pool_group_id")
        if group_id:
            group_active = supabase.table("rides").select("drop_lat,drop_lng,pickup_lat,pickup_lng").eq("pool_group_id", group_id).in_("status", ["accepted","started"]).execute().data or []
            max_dist = 0
            farthest_lat = float(active.get("drop_lat",0))
            farthest_lng = float(active.get("drop_lng",0))
            first_pick_lat = float(active.get("pickup_lat",0))
            first_pick_lng = float(active.get("pickup_lng",0))
            for g in group_active:
                try:
                    d_lat = float(g.get("drop_lat",0))
                    d_lng = float(g.get("drop_lng",0))
                    dist = haversine(first_pick_lat, first_pick_lng, d_lat, d_lng)
                    if dist > max_dist:
                        max_dist = dist
                        farthest_lat = d_lat
                        farthest_lng = d_lng
                except:
                    pass
            a_drop_lat = farthest_lat
            a_drop_lng = farthest_lng
        else:
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
            ok, d_on = is_point_along_route(curr_lat, curr_lng, a_drop_lat, a_drop_lng, n_lat, n_lng, 3.0)
            if ok:
                r["distance_from_driver"] = round(haversine(curr_lat,curr_lng,n_lat,n_lng),2)
                r["dist_on_route"] = round(d_on,2)
                r["seats_left"] = seats_left
                r["seats_needed"] = seats_needed
                r["farthest_drop_lat"] = a_drop_lat
                r["farthest_drop_lng"] = a_drop_lng
                r.pop("otp",None)
                along.append(r)
        except:
            continue
    along.sort(key=lambda x: x.get("dist_on_route",999))
    return along[:8]

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
    res = supabase.table("rides").select("*").eq("driver_id", driver_id).in_("status", ["accepted", "started", "arrived", "rider_accepted"]).order("id", desc=True).limit(10).execute()
    if res.data:
        enriched = [enrich_ride_with_driver(r) for r in res.data]
        for r in enriched:
            if r.get("status") == "rider_accepted":
                return {"active": True, "rides": enriched, "ride": r, "need_final_confirm": True}
        return {"active": True, "rides": enriched, "ride": enriched[0]}
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

@app.get("/admin/archived")
def get_archived():
    try:
        res = supabase.table("rides_archive").select("*").order("archived_at", desc=True).limit(100).execute()
        return res.data
    except Exception as e:
        return {"error": str(e)}

@app.get("/rides/{ride_id}/driver-location")
def get_driver_location_for_ride(ride_id: int):
    try:
        ride_res = supabase.table("rides").select("driver_id").eq("id", ride_id).execute()
        if not ride_res.data:
            raise HTTPException(404, "Ride not found")
        driver_id = ride_res.data[0].get("driver_id")
        if not driver_id:
            return {"lat": None, "lng": None, "current_latitude": None, "current_longitude": None}
        d_res = supabase.table("drivers").select("id,current_latitude,current_longitude,last_seen,photo_url").eq("id", driver_id).execute()
        if not d_res.data:
            d_res = supabase.table("drivers").select("id,current_latitude,current_longitude,last_seen,photo_url").eq("driver_id", driver_id).execute()
        if not d_res.data:
            raise HTTPException(404, "Driver not found")
        d = d_res.data[0]
        lat = d.get("current_latitude")
        lng = d.get("current_longitude")
        return {"driver_id": driver_id,"lat": lat,"lng": lng,"current_latitude": lat,"current_longitude": lng,"latitude": lat,"longitude": lng,"last_seen": d.get("last_seen"),"driver_photo": d.get("photo_url"),"photo_url": d.get("photo_url")}
    except HTTPException:
        raise
    except Exception as e:
        print(f"driver-location error {e}")
        raise HTTPException(500, str(e))

@app.put("/rides/pool/group/{group_id}/complete")
def complete_pool_group(group_id: str, driver_id: str = Query(None), background_tasks: BackgroundTasks = None):
    try:
        res = supabase.table("rides").select("id").eq("pool_group_id", group_id).in_("status", ["accepted","started","arrived","pending"]).execute()
        rides = res.data or []
        if not rides:
            return {"success": True, "message": "Already completed"}
        for r in rides:
            supabase.table("rides").update({"status": "completed","passenger_status": "dropped","completed_at": datetime.now(timezone.utc).isoformat()}).eq("id", r["id"]).execute()
            if background_tasks:
                background_tasks.add_task(archive_and_delete_ride, r["id"])
        return {"success": True, "completed_count": len(rides), "group_id": group_id}
    except Exception as e:
        print(f"Group Complete Error {e}")
        raise HTTPException(500, str(e))

@app.get("/rides/{ride_id}/with-driver-location")
def get_ride_with_driver_location(ride_id: int):
    res = supabase.table("rides").select("*").eq("id", ride_id).execute()
    if not res.data:
        raise HTTPException(404, "Ride not found")
    ride = enrich_ride_with_driver(res.data[0])
    driver_id = ride.get("driver_id")
    if driver_id:
        try:
            d_res = supabase.table("drivers").select("current_latitude,current_longitude").eq("id", driver_id).execute()
            if not d_res.data:
                d_res = supabase.table("drivers").select("current_latitude,current_longitude").eq("driver_id", driver_id).execute()
            if d_res.data:
                d = d_res.data[0]
                ride["driver_current_latitude"] = d.get("current_latitude")
                ride["driver_current_longitude"] = d.get("current_longitude")
                ride["current_latitude"] = d.get("current_latitude")
                ride["current_longitude"] = d.get("current_longitude")
                ride["driver_lat"] = d.get("current_latitude")
                ride["driver_lng"] = d.get("current_longitude")
        except:
            pass
    return ride

@app.get("/admin/drivers")
def get_all_drivers_admin():
    try:
        res = supabase.table("drivers").select("*").order("id", desc=False).execute()
        return res.data or []
    except Exception as e:
        return {"error": str(e)}

@app.post("/admin/drivers")
def create_driver_admin(payload: dict):
    try:
        if "driver_id" not in payload and "id" in payload:
            payload["driver_id"] = payload["id"]
        if "id" not in payload and "driver_id" in payload:
            payload["id"] = payload["driver_id"]
        if "is_active" not in payload:
            payload["is_active"] = True
        if "is_online" not in payload:
            payload["is_online"] = True
        res = supabase.table("drivers").insert(payload).execute()
        return res.data
    except Exception as e:
        raise HTTPException(400, str(e))

@app.put("/admin/drivers/{driver_id}")
def update_driver_admin(driver_id: str, payload: dict):
    try:
        payload.pop("id", None)
        res = supabase.table("drivers").update(payload).eq("id", driver_id).execute()
        if not res.data:
            res = supabase.table("drivers").update(payload).eq("driver_id", driver_id).execute()
        return res.data or {"success": True}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.put("/admin/drivers/{driver_id}/pause")
def pause_driver_admin(driver_id: str, payload: dict):
    try:
        is_active = payload.get("is_active", True)
        update_data = {"is_active": is_active, "is_online": is_active}
        res = supabase.table("drivers").update(update_data).eq("id", driver_id).execute()
        if not res.data:
            res = supabase.table("drivers").update(update_data).eq("driver_id", driver_id).execute()
        if not is_active:
            manager.driver_connections = [d for d in manager.driver_connections if d.get("driver_id") != driver_id]
        return {"success": True, "is_active": is_active}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.delete("/admin/drivers/{driver_id}")
def delete_driver_admin(driver_id: str):
    try:
        supabase.table("drivers").delete().eq("id", driver_id).execute()
        supabase.table("drivers").delete().eq("driver_id", driver_id).execute()
        return {"success": True}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/admin/vehicles")
def create_vehicle_admin(payload: dict):
    try:
        res = supabase.table("vehicles").insert(payload).execute()
        return res.data
    except Exception as e:
        raise HTTPException(400, str(e))

@app.put("/admin/vehicles/{vid}")
def update_vehicle_admin(vid: str, payload: dict):
    try:
        res = supabase.table("vehicles").update(payload).eq("id", vid).execute()
        if not res.data:
            try:
                int_vid = int(vid)
                res = supabase.table("vehicles").update(payload).eq("id", int_vid).execute()
            except:
                res = supabase.table("vehicles").update(payload).eq("name", vid).execute()
        return res.data or {"success": True}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/admin/vehicles/{vid}")
def delete_vehicle_admin(vid: str):
    try:
        supabase.table("vehicles").delete().eq("id", vid).execute()
    except:
        try:
            supabase.table("vehicles").delete().eq("name", vid).execute()
        except Exception as e:
            raise HTTPException(400, str(e))
    return {"success": True}

@app.get("/admin/rides/all")
def get_all_rides_admin(status: str = Query(None), limit: int = Query(100)):
    try:
        q = supabase.table("rides").select("*").order("id", desc=True).limit(limit)
        if status and status != "all":
            q = q.eq("status", status)
        res = q.execute()
        rides = res.data or []
        enriched = []
        for r in rides:
            if r.get("driver_id"):
                r = enrich_ride_with_driver(r)
            enriched.append(r)
        return enriched
    except Exception as e:
        return {"error": str(e)}

@app.get("/admin/stats")
def get_admin_stats():
    try:
        pending = supabase.table("rides").select("id", count="exact").eq("status", "pending").execute()
        accepted = supabase.table("rides").select("id", count="exact").in_("status", ["accepted","started","rider_accepted"]).execute()
        drivers = supabase.table("drivers").select("id", count="exact").execute()
        active_drivers = supabase.table("drivers").select("id", count="exact").eq("is_active", True).execute()
        vehicles = supabase.table("vehicles").select("id", count="exact").execute()
        archived = supabase.table("rides_archive").select("id", count="exact").execute()
        return {
            "pending_rides": pending.count or 0,
            "active_rides": accepted.count or 0,
            "total_drivers": drivers.count or 0,
            "active_drivers": active_drivers.count or 0,
            "total_vehicles": vehicles.count or 0,
            "completed_rides": archived.count or 0,
        }
    except Exception as e:
        return {"error": str(e)}
