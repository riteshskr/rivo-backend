import requests
import time

URL = "https://rivo-api-ezoo.onrender.com/health"

while True:
    try:
        # timeout=90 सेकंड रखें ताकि सर्वर को जागने का पूरा समय मिले
        response = requests.get(URL, timeout=180)
        print(f"✅ {time.strftime('%H:%M:%S')} - Status: {response.status_code}")
    except Exception as e:
        print(f"❌ {time.strftime('%H:%M:%S')} - Error: {e}")
    
    # 5 मिनट (300 सेकंड) तक सोएं
    time.sleep(300)
