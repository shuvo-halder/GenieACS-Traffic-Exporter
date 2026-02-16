from flask import Flask, Response
import requests
import os
import time

GENIEACS_URL = os.getenv("GENIEACS_URL", "http://127.0.0.1:7557/devices")
TIMEOUT = int(os.getenv("GENIEACS_TIMEOUT", "10"))
PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", "1000"))

app = Flask(__name__)

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

@app.route("/metrics")
def metrics():
    def generate():
        start = time.time()
        yield "# HELP genieacs_rx_bytes RX bytes\n"
        yield "# TYPE genieacs_rx_bytes counter\n"
        yield "# HELP genieacs_tx_bytes TX bytes\n"
        yield "# TYPE genieacs_tx_bytes counter\n"

        try:
            skip = 0
            total_devices = 0

            while True:
                r = requests.get(GENIEACS_URL, params={"limit": PAGE_LIMIT, "skip": skip}, timeout=TIMEOUT)
                batch = r.json()
                if not batch:
                    break

                for d in batch:
                    device_id = d.get("_id", "unknown")
                    for iface, rx, tx in extract_stats(d):
                        yield f'genieacs_rx_bytes{{device="{device_id}",iface="{iface}"}} {rx}\n'
                        yield f'genieacs_tx_bytes{{device="{device_id}",iface="{iface}"}} {tx}\n'

                skip += PAGE_LIMIT
                total_devices += len(batch)

            duration = time.time() - start
            yield f"# HELP genieacs_devices_total Total devices\n"
            yield f"# TYPE genieacs_devices_total gauge\n"
            yield f"genieacs_devices_total {total_devices}\n"

            yield "# HELP genieacs_scrape_duration_seconds Scrape duration\n"
            yield "# TYPE genieacs_scrape_duration_seconds gauge\n"
            yield f"genieacs_scrape_duration_seconds {duration}\n"

            yield "# HELP genieacs_last_scrape_success Last scrape success\n"
            yield "# TYPE genieacs_last_scrape_success gauge\n"
            yield "genieacs_last_scrape_success 1\n"

        except Exception:
            yield "# HELP genieacs_last_scrape_success Last scrape success\n"
            yield "# TYPE genieacs_last_scrape_success gauge\n"
            yield "genieacs_last_scrape_success 0\n"

    return Response(generate(), mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9105)
