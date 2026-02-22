import time
import requests
import os
import json
from cache import update_cache, mark_failed

GENIEACS_URL = os.getenv("GENIEACS_URL")
PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", 5000))
INTERVAL = int(os.getenv("FETCH_INTERVAL", 600))
TIMEOUT = 60
ONLINE_THRESHOLD = int(os.getenv("ONLINE_THRESHOLD", 300))

projection = {
    "_id": 1,
    "_lastInform": 1,
    "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Stats": 1,
    "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.Stats": 1,
    "InternetGatewayDevice.LANDevice.1.WLANConfiguration": 1
}

def safe_get(d, key):
    """Safely extract GenieACS value"""
    if isinstance(d, dict):
        v = d.get(key)
        if isinstance(v, dict) and "_value" in v:
            return v["_value"]
        return v
    return 0


def extract_stats(device):
    """Extract WLAN traffic stats"""
    stats = []

    wlan = (
        device.get("InternetGatewayDevice", {})
        .get("LANDevice", {})
        .get("1", {})
        .get("WLANConfiguration", {})
    )

    if not isinstance(wlan, dict):
        return stats

    for idx, cfg in wlan.items():
        if not isinstance(cfg, dict):
            continue

        rx = safe_get(cfg, "TotalBytesReceived")
        tx = safe_get(cfg, "TotalBytesSent")

        if rx or tx:
            stats.append((f"wlan{idx}", rx or 0, tx or 0))

    return stats

def run_worker():
    session = requests.Session()
    session.headers.update({"Connection": "keep-alive"})

    while True:
        try:
            skip = 0
            total_devices = 0
            online_count = 0
            offline_count = 0
            lines = []

            now = int(time.time())

            # ---- Prometheus HELP ----
            lines += [
                "# HELP genieacs_rx_bytes RX bytes",
                "# TYPE genieacs_rx_bytes counter",
                "# HELP genieacs_tx_bytes TX bytes",
                "# TYPE genieacs_tx_bytes counter",
                "# HELP genieacs_device_online Device online status",
                "# TYPE genieacs_device_online gauge",
                "# HELP genieacs_devices_total Total devices",
                "# TYPE genieacs_devices_total gauge",
                "# HELP genieacs_devices_online Online devices",
                "# TYPE genieacs_devices_online gauge",
                "# HELP genieacs_devices_offline Offline devices",
                "# TYPE genieacs_devices_offline gauge",
            ]

            # ---- Pagination Loop ----
            while True:
                r = session.get(
                    GENIEACS_URL,
                    params={
                        "limit": PAGE_LIMIT,
                        "skip": skip,
                        "projection": json.dumps(projection),
                    },
                    timeout=TIMEOUT,
                )
                r.raise_for_status()
                batch = r.json()

                if not batch:
                    break

                for d in batch:
                    total_devices += 1

                    device_id = str(d.get("_id", "")).replace('"', "").replace("\\", "")

                    # ---- Online detection ----
                    last_inform = int(d.get("_lastInform", 0))
                    is_online = last_inform > 0 and (now - last_inform) <= ONLINE_THRESHOLD

                    if is_online:
                        online_count += 1
                        status = 1
                    else:
                        offline_count += 1
                        status = 0

                    lines.append(
                        f'genieacs_device_online{{device="{device_id}"}} {status}'
                    )

                    # ---- Traffic stats ----
                    for iface, rx, tx in extract_stats(d):
                        lines.append(
                            f'genieacs_rx_bytes{{device="{device_id}",iface="{iface}"}} {rx}'
                        )
                        lines.append(
                            f'genieacs_tx_bytes{{device="{device_id}",iface="{iface}"}} {tx}'
                        )

                skip += PAGE_LIMIT

            # ---- Summary metrics ----
            lines.append(f"genieacs_devices_total {total_devices}")
            lines.append(f"genieacs_devices_online {online_count}")
            lines.append(f"genieacs_devices_offline {offline_count}")

            update_cache("\n".join(lines) + "\n", total_devices, online_count)
            print(
                f"[worker] updated: total={total_devices}, online={online_count}, offline={offline_count}"
            )

        except Exception as e:
            print("[worker] error:", e)
            mark_failed()

        time.sleep(INTERVAL)


if __name__ == "__main__":
    run_worker()