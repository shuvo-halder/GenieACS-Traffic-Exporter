import redis
import time

r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

def update_cache(metrics, total_devices, online_devices, device_ids=None):
    """Update exporter cache"""

    pipe = r.pipeline()

    pipe.set("metrics", metrics)
    pipe.set("device_count", total_devices)
    pipe.set("online_count", online_devices)
    pipe.set("offline_count", total_devices - online_devices)
    pipe.set("last_update", time.time())
    pipe.set("success", 1)

    if device_ids is not None:
        pipe.delete("device_ids")
        if device_ids:
            pipe.sadd("device_ids", *device_ids)

    pipe.execute()


def mark_failed():
    r.set("success", 0)

def read_cache():
    """Read exporter cache"""
    return {
        "metrics": r.get("metrics") or "",
        "device_count": int(r.get("device_count") or 0),
        "online_count": int(r.get("online_count") or 0),
        "offline_count": int(r.get("offline_count") or 0),
        "device_ids": list(r.smembers("device_ids")),
        "last_update": float(r.get("last_update") or 0),
        "success": int(r.get("success") or 0),
    }