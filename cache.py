import redis
import json
import time

r = redis.Redis(host="localhost", port=6379, db=0)

def update_cache(metrics, count, device_id):
    r.set("metrics", metrics)
    r.set("device_count", count)
    r.sadd("device_id", device_id)
    r.set("last_update", time.time())
    r.set("success", 1)

def mark_failed():
    r.set("success", 0)

def read_cache():
    return {
        "metrics": r.get("metrics").decode() if r.get("metrics") else "",
        "device_count": int(r.get("device_count") or 0),
        "device_id": [id.decode() for id in r.smembers("device_id")],
        "last_update": float(r.get("last_update") or 0),
        "success": int(r.get("success") or 0)
    }
