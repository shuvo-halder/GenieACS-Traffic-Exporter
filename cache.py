import threading
import time

lock = threading.Lock()

CACHE = {
    "metrics": "",
    "device_count": 0,
    "last_update": 0,
    "success": 0
}

def update_cache(metrics, count):
    with lock:
        CACHE["metrics"] = metrics
        CACHE["device_count"] = count
        CACHE["last_update"] = time.time()
        CACHE["success"] = 1

def mark_failed():
    with lock:
        CACHE["success"] = 0

def read_cache():
    with lock:
        return CACHE.copy()
