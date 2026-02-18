import time
import requests
import os
import json
from cache import update_cache, mark_failed

GENIEACS_URL = os.getenv("GENIEACS_URL")
PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", 5000))
INTERVAL = int(os.getenv("FETCH_INTERVAL", 600))
TIMEOUT = 60

projection = {
    "_id": 1,
    "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Stats": 1,
    "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.Stats": 1,
    "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1": 1,
    "InternetGatewayDevice.LANDevice.1.WLANConfiguration.2": 1
}

def safe_get(d, key):
    if isinstance(d, dict):
        v = d.get(key)
        if isinstance(v, dict) and "_value" in v:
            return v["_value"]
        return v
    return 0

# def extract_stats(device):
#     stats = []

#     paths = {
#         "ppp": ["InternetGatewayDevice","WANDevice","1","WANConnectionDevice","1","WANPPPConnection","1","Stats","1"],
#         "ip":  ["InternetGatewayDevice","WANDevice","1","WANConnectionDevice","1","WANIPConnection","1","Stats","1"],
#         "wlan":["InternetGatewayDevice","LANDevice","1","WLANConfiguration","1","Stats"]
#     }

#     for iface, path in paths.items():
#         base = device
#         for p in path:
#             if not isinstance (base, dict):
#                 base = {}
#                 break
#             base = base.get(p, {})
#         if not isinstance(base, dict):
#             continue

#         rx = safe_get(base, "EthernetBytesReceived") or safe_get(base, "TotalBytesReceived")
#         tx = safe_get(base, "EthernetBytesSent") or safe_get(base, "TotalBytesSent")

#         if rx or tx:
#             stats.append((iface, rx or 0, tx or 0))

#     return stats


def extract_stats(device):
    stats = []

    wlan = device.get("InternetGatewayDevice", {}) \
        .get("LANDevice", {}) \
        .get("1", {}) \
        .get("WLANConfiguration", {})

    if not isinstance(wlan, dict):
        return stats

    for idx, cfg in wlan.items():
        if not isinstance(cfg, dict):
            continue

        rx = safe_get(cfg, "TotalBytesReceived")
        tx = safe_get(cfg, "TotalBytesSent")

        if rx or tx:
            stats.append((f"wlan{idx}", rx or 0, tx or 0))

    # -------- WAN IP --------
    ip = device.get("InternetGatewayDevice", {}) \
        .get("WANDevice", {}) \
        .get("1", {}) \
        .get("WANConnectionDevice", {}) \
        .get("1", {}) \
        .get("WANIPConnection", {})

    if isinstance(ip, dict):
        for idx, cfg in ip.items():
            if not isinstance(cfg, dict):
                continue

            rx = safe_get(cfg, "TotalBytesReceived")
            tx = safe_get(cfg, "TotalBytesSent")

            if rx or tx:
                stats.append((f"ip{idx}", rx or 0, tx or 0))
    
    # -------- WAN PPP --------
    ppp = device.get("InternetGatewayDevice", {}) \
        .get("WANDevice", {}) \
        .get("1", {}) \
        .get("WANConnectionDevice", {}) \
        .get("1", {}) \
        .get("WANPPPConnection", {})

    if isinstance(ppp, dict):
        for idx, cfg in ppp.items():
            if not isinstance(cfg, dict):
                continue

            rx = safe_get(cfg, "TotalBytesReceived")
            tx = safe_get(cfg, "TotalBytesSent")

            if rx or tx:
                stats.append((f"ppp{idx}", rx or 0, tx or 0))

    return stats

def run_worker():
    session = requests.Session()
    session.headers.update({"Connection": "keep-alive"})

    while True:
        try:
            skip = 0
            count = 0
            lines = []

            lines.append("# HELP genieacs_rx_bytes RX bytes")
            lines.append("# TYPE genieacs_rx_bytes counter")
            lines.append("# HELP genieacs_tx_bytes TX bytes")
            lines.append("# TYPE genieacs_tx_bytes counter")

            lines.append("# HELP genieacs_device_info Device information")
            lines.append("# TYPE genieacs_device_info gauge")


            while True:
                r = session.get(
                    GENIEACS_URL,
                    params={
                        "limit": PAGE_LIMIT,
                        "skip": skip,
                        "projection": json.dumps(projection)
                    },
                    timeout=TIMEOUT
                )

                r.raise_for_status()
                batch = r.json()

                if not batch:
                    break

                for d in batch:
                    device_id = str(d.get("_id", "")).replace('"','').replace("\\","")
                    count += 1

                    for iface, rx, tx in extract_stats(d):
                        lines.append(
                            f'genieacs_rx_bytes{{device="{device_id}",iface="{iface}"}} {rx}'
                        )
                        lines.append(
                            f'genieacs_tx_bytes{{device="{device_id}",iface="{iface}"}} {tx}'
                        )

                    info = d.get("DeviceID", {})
                    lines.append(
                        f'genieacs_device_info{{device="{device_id}"}} 1'
                    )


                skip += PAGE_LIMIT

            lines.append(f"genieacs_devices_total {count}")

            update_cache("\n".join(lines) + "\n", count, device_id)
            print(f"[worker] updated cache: {count} devices")

        except Exception as e:
            print("[worker] error:", e)
            mark_failed()

        time.sleep(INTERVAL)

if __name__ == "__main__":
    run_worker()
