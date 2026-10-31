from fastapi import FastAPI
import asyncio, httpx, os, time
app = FastAPI()
OTHER = os.getenv("OTHER_SERVER_URL")
@app.api_route("/", methods=["GET","HEAD"])
@app.api_route("/health", methods=["GET","HEAD"])
def health(): return {"ok": True}
async def ping():
    await asyncio.sleep(60)
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.get(f"{OTHER}/health?t={int(time.time())}")
                print("pinged main")
        except Exception as e:
            print(e)
        await asyncio.sleep(240)
@app.on_event("startup")
async def start():
    asyncio.create_task(ping())