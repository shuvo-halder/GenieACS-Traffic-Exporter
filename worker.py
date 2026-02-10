import time
import requests
import os
import json
from cache import update_cache, mark_failed

GENIEACS_URL = os.getenv("GENIEACS_URL")
PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", 1000))
INTERVAL = int(os.getenv("FETCH_INTERVAL", 300))
TIMEOUT = 15
CACHE_FILE = "/opt/genieacs-exporter/cache.json"

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

    wan = device.get("InternetGatewayDevice", {}).get("WANDevice", {})
    for _, wdev in wan.items():
        wcd = wdev.get("WANConnectionDevice", {})
        for _, conn_dev in wcd.items():

            for conn_type, iface in [
                ("WANPPPConnection", "ppp"),
                ("WANIPConnection", "ip"),
            ]:
                conns = conn_dev.get(conn_type, {})
                for _, conn in conns.items():
                    stats_block = conn.get("Stats", {})
                    if isinstance(stats_block, dict):
                        stats_block = stats_block.get("1", stats_block)

                    rx = safe_get(stats_block, "TotalBytesReceived")
                    tx = safe_get(stats_block, "TotalBytesSent")

                    if rx or tx:
                        stats.append((iface, rx or 0, tx or 0))

    return stats


def run_worker():
    while True:
        try:
            skip = 0
            count = 0
            lines = []

            lines.append("# HELP genieacs_rx_bytes RX bytes")
            lines.append("# TYPE genieacs_rx_bytes counter")
            lines.append("# HELP genieacs_tx_bytes TX bytes")
            lines.append("# TYPE genieacs_tx_bytes counter")

            while True:
                r = requests.get(
                    GENIEACS_URL,
                    params={"limit": PAGE_LIMIT, "skip": skip},
                    timeout=TIMEOUT
                )
                batch = r.json()
                if not batch:
                    break

                for d in batch:
                    device_id = d.get("_id")
                    for iface, rx, tx in extract_stats(d):
                        lines.append(
                            f'genieacs_rx_bytes{{iface="{iface}"}} {rx}'
                        )
                        lines.append(
                            f'genieacs_tx_bytes{{iface="{iface}"}} {tx}'
                        )

                count += len(batch)
                skip += PAGE_LIMIT

            lines.append(f"genieacs_devices_total {count}")

            update_cache("\n".join(lines) + "\n", count)
            print(f"[worker] updated cache: {count} devices")

        except Exception as e:
            print("[worker] error:", e)
            mark_failed()

        time.sleep(INTERVAL)

if __name__ == "__main__":
    run_worker()
