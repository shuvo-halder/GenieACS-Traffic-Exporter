from flask import Flask, Response
import requests
import os
import time
import threading

GENIEACS_URL = os.getenv("GENIEACS_URL", "http://127.0.0.1:7557/devices")
TIMEOUT = int(os.getenv("GENIEACS_TIMEOUT", "10"))
FETCH_INTERVAL = int(os.getenv("FETCH_INTERVAL", "120"))
PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", "1000"))

app = Flask(__name__)

CACHE = {
    "metrics": "",
    "last_success": 0,
    "duration": 0,
    "device_count": 0
}

def safe_get(d, key):
    if isinstance(d, dict):
        v = d.get(key)
        if isinstance(v, dict) and "_value" in v:
            return v["_value"]
        return v
    return 0

def get_path(d, path):
    for p in path:
        d = d.get(p, {})
    return d if isinstance(d, dict) else {}

def extract_stats(device):
    stats = []

    paths = {
        "ppp": ["InternetGatewayDevice","WANDevice","1","WANConnectionDevice","1","WANPPPConnection","1","Stats","1"],
        "ip":  ["InternetGatewayDevice","WANDevice","1","WANConnectionDevice","1","WANIPConnection","1","Stats","1"],
        "wlan":["InternetGatewayDevice","LANDevice","1","WLANConfiguration","1","Stats"]
    }

    for iface, path in paths.items():
        base = get_path(device, path)
        if not base:
            continue

        rx = safe_get(base, "EthernetBytesReceived") or safe_get(base, "TotalBytesReceived")
        tx = safe_get(base, "EthernetBytesSent") or safe_get(base, "TotalBytesSent")

        if rx or tx:
            stats.append((iface, rx or 0, tx or 0))

    return stats

def fetch_devices():
    devices = []
    skip = 0

    while True:
        r = requests.get(
            GENIEACS_URL,
            params={"limit": PAGE_LIMIT, "skip": skip},
            timeout=TIMEOUT
        )
        batch = r.json()
        if not batch:
            break
        devices.extend(batch)
        skip += PAGE_LIMIT

    return devices

def collector_loop():
    while True:
        start = time.time()
        success = 0

        try:
            devices = fetch_devices()
            lines = []

            lines.append("# HELP genieacs_rx_bytes RX bytes")
            lines.append("# TYPE genieacs_rx_bytes counter")
            lines.append("# HELP genieacs_tx_bytes TX bytes")
            lines.append("# TYPE genieacs_tx_bytes counter")

            for d in devices:
                device_id = d.get("_id", "unknown")
                for iface, rx, tx in extract_stats(d):
                    lines.append(
                        f'genieacs_rx_bytes{{device_id="{device_id}",iface="{iface}"}} {rx}'
                    )
                    lines.append(
                        f'genieacs_tx_bytes{{device_id="{device_id}",iface="{iface}"}} {tx}'
                    )

            duration = time.time() - start

            lines.append("# HELP genieacs_devices_total Total devices")
            lines.append("# TYPE genieacs_devices_total gauge")
            lines.append(f"genieacs_devices_total {len(devices)}")

            lines.append("# HELP genieacs_scrape_duration_seconds Collector duration")
            lines.append("# TYPE genieacs_scrape_duration_seconds gauge")
            lines.append(f"genieacs_scrape_duration_seconds {duration}")

            lines.append("# HELP genieacs_last_scrape_success Last scrape success")
            lines.append("# TYPE genieacs_last_scrape_success gauge")
            lines.append("genieacs_last_scrape_success 1")

            CACHE["metrics"] = "\n".join(lines)
            CACHE["last_success"] = int(time.time())
            CACHE["duration"] = duration
            CACHE["device_count"] = len(devices)
            success = 1

        except Exception:
            CACHE["metrics"] = (
                "# HELP genieacs_last_scrape_success Last scrape success\n"
                "# TYPE genieacs_last_scrape_success gauge\n"
                "genieacs_last_scrape_success 0\n"
            )

        time.sleep(FETCH_INTERVAL)

@app.route("/metrics")
def metrics():
    return Response(CACHE["metrics"], mimetype="text/plain")

if __name__ == "__main__":
    t = threading.Thread(target=collector_loop, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=9105)
