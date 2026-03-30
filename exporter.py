import os
import asyncio
import aiohttp
import time
from prometheus_client import start_http_server, Gauge

GENIEACS_API_URL = os.getenv("GENIEACS_API_URL", "http://localhost:7557")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
DEVICE_QUERY_FILTER = os.getenv("DEVICE_QUERY_FILTER", "")

# Prometheus Metrics
rx_bytes_gauge = Gauge("genieacs_device_rx_bytes", "Received bytes", ["device_id"])
tx_bytes_gauge = Gauge("genieacs_device_tx_bytes", "Transmitted bytes", ["device_id"])
device_online = Gauge("genieacs_device_online", "Device online status", ["device_id"])

# Config
BATCH_SIZE = 500
TIMEOUT = aiohttp.ClientTimeout(total=30)

async def fetch_devices(session, skip=0):
    url = f"{GENIEACS_API_URL}/devices?limit={BATCH_SIZE}&skip={skip}&query={DEVICE_QUERY_FILTER}"
    async with session.get(url) as resp:
        return await resp.json()

def extract_bytes(device):
    try:
        params = device.get("InternetGatewayDevice", {})
        # Simplified traversal (you can extend for multi-WAN)
        wan = params["WANDevice"][1]["WANConnectionDevice"][1]["WANIPConnection"][1]["Stats"]

        rx = wan.get("BytesReceived", {}).get("_value", 0)
        tx = wan.get("BytesSent", {}).get("_value", 0)

        return int(rx), int(tx)
    except Exception:
        return 0, 0

def is_online(device):
    try:
        last_inform = device["_lastInform"]
        return (time.time() - last_inform) < 300  # 5 min threshold
    except:
        return False

async def process_batch(session, skip):
    devices = await fetch_devices(session, skip)
    if not devices:
        return False

    for device in devices:
        device_id = device["_id"]

        online = is_online(device)
        device_online.labels(device_id=device_id).set(1 if online else 0)

        if not online:
            continue

        rx, tx = extract_bytes(device)

        rx_bytes_gauge.labels(device_id=device_id).set(rx)
        tx_bytes_gauge.labels(device_id=device_id).set(tx)

    return True

async def collect():
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        skip = 0
        while True:
            has_more = await process_batch(session, skip)
            if not has_more:
                break
            skip += BATCH_SIZE

async def loop():
    while True:
        try:
            await collect()
        except Exception as e:
            print("Error:", e)

        await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    start_http_server(9105)
    asyncio.run(loop())