# exp.py
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple, List

import aiohttp
from dateutil import parser as dateparser
from fastapi import FastAPI, Response
from prometheus_client import CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily, REGISTRY

# -----------------------
# Configuration via env
# -----------------------
GENIEACS_URL = os.getenv("GENIEACS_URL", "http://genieacs:7557/devices")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
ACTIVE_WINDOW_SECONDS = int(os.getenv("ACTIVE_WINDOW_SECONDS", str(5 * 60)))  # default 5 minutes
KEEP_LAST_KNOWN_SECONDS = int(os.getenv("KEEP_LAST_KNOWN_SECONDS", str(60 * 60)))  # keep last-known values for 1 hour
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF_BASE = float(os.getenv("RETRY_BACKOFF_BASE", "0.5"))
CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY_LIMIT", "50"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", "1000"))  # for pagination if needed

# -----------------------
# Logging
# -----------------------
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s:%(name)s: %(message)s")
logger = logging.getLogger("exp")

# -----------------------
# In-memory store
# -----------------------
# device_metrics: device_id -> {
#   "bytes_received": int,
#   "bytes_sent": int,
#   "last_inform": iso,
#   "found_keys": [...],
#   "last_seen": iso,
#   "active": bool
# }
device_metrics: Dict[str, Dict[str, Any]] = {}
device_metrics_lock = asyncio.Lock()

# -----------------------
# Internal Prometheus registry for exporter metrics
# -----------------------
PROM_REG = CollectorRegistry()
poll_success = Gauge("genieacs_poll_success", "Last poll success (1=success,0=failure)", registry=PROM_REG)
poll_duration = Histogram("genieacs_poll_duration_seconds", "Duration of last poll", registry=PROM_REG)
active_devices_g = Gauge("genieacs_active_devices", "Number of active devices in memory", registry=PROM_REG)

# -----------------------
# Custom collector for device metrics
# -----------------------
class GenieACSCollector:
    def collect(self):
        g_recv = GaugeMetricFamily(
            "genieacs_device_bytes_received",
            "Latest BytesReceived from GenieACS per device",
            labels=["device_id"],
        )
        g_sent = GaugeMetricFamily(
            "genieacs_device_bytes_sent",
            "Latest BytesSent from GenieACS per device",
            labels=["device_id"],
        )
        g_active = GaugeMetricFamily(
            "genieacs_device_active",
            "Device active flag from GenieACS per device (1=active,0=inactive)",
            labels=["device_id"],
        )
        g_last_seen = GaugeMetricFamily(
            "genieacs_device_last_seen_seconds",
            "Last seen time as unix epoch seconds per device",
            labels=["device_id"],
        )

        # snapshot copy (atomic enough because poller replaces dict under lock)
        snapshot = dict(device_metrics)
        for device_id, vals in snapshot.items():
            try:
                br = vals.get("bytes_received", 0) or 0
                bs = vals.get("bytes_sent", 0) or 0
                active = 1 if vals.get("active") else 0
                last_seen_iso = vals.get("last_seen") or vals.get("last_inform")
                last_seen_ts = None
                if last_seen_iso:
                    dt = parse_last_inform(last_seen_iso)
                    if dt:
                        last_seen_ts = dt.timestamp()

                g_recv.add_metric([device_id], float(br))
                g_sent.add_metric([device_id], float(bs))
                g_active.add_metric([device_id], float(active))
                if last_seen_ts is not None:
                    g_last_seen.add_metric([device_id], float(last_seen_ts))
            except Exception:
                logger.exception("Error adding metric for device %s", device_id)

        yield g_recv
        yield g_sent
        yield g_active
        yield g_last_seen

# Register collector after class definition
REGISTRY.register(GenieACSCollector())

# -----------------------
# Helpers: parsing and safe conversion
# -----------------------
def parse_last_inform(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = dateparser.parse(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        logger.debug("Failed to parse _lastInform: %s", ts)
        return None

def unwrap_value(v):
    """
    GenieACS often stores values as:
      { "_value": 123 } or { "value": 123 } or plain scalar
    """
    if isinstance(v, dict):
        if "_value" in v:
            return v["_value"]
        if "value" in v:
            return v["value"]
    return v

def safe_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        if isinstance(val, str):
            val = val.strip().replace(",", "")
        return int(val)
    except Exception:
        try:
            return int(float(val))
        except Exception:
            return None

def find_bytes_in_tree(obj: Any) -> Tuple[Optional[int], Optional[int], List[str]]:
    br = None
    bs = None
    found_keys: List[str] = []

    def norm_key(k: str) -> str:
        return k.lower().replace(" ", "")

    def extract_int_from_val(v) -> Optional[int]:
        v = unwrap_value(v)
        if v is None:
            return None
        if isinstance(v, (int, float)):
            try:
                return int(v)
            except Exception:
                return None
        if isinstance(v, str):
            s = v.strip().replace(",", "")
            import re
            m = re.search(r"(-?\d+(\.\d+)?)", s)
            if m:
                try:
                    return int(float(m.group(1)))
                except Exception:
                    return None
        return None

    def key_matches(k: str, want: str) -> bool:
        lk = norm_key(k)
        # match bytes/octet and common recv/sent tokens
        if "bytes" in lk or "octet" in lk:
            if want == "recv":
                return any(x in lk for x in ("recv", "received", "down", "rx", "in", "download", "total"))
            else:
                return any(x in lk for x in ("sent", "tx", "up", "upload", "total"))
        # fallback matches
        if want == "recv" and any(x in lk for x in ("received", "rx", "down")):
            return True
        if want == "sent" and any(x in lk for x in ("sent", "tx", "up")):
            return True
        return False

    def dfs(node: Any, path: str = ""):
        nonlocal br, bs
        if isinstance(node, dict):
            for k, v in node.items():
                full_key = f"{path}.{k}" if path else k
                try:
                    if isinstance(k, str):
                        if key_matches(k, "recv"):
                            ival = extract_int_from_val(v)
                            if ival is not None:
                                br = ival
                                found_keys.append(full_key)
                        if key_matches(k, "sent"):
                            ival = extract_int_from_val(v)
                            if ival is not None:
                                bs = ival
                                found_keys.append(full_key)
                except Exception:
                    logger.debug("Error checking key %s", full_key, exc_info=True)
                if isinstance(v, dict):
                    dfs(v, full_key)
                elif isinstance(v, list):
                    for idx, item in enumerate(v):
                        dfs(item, f"{full_key}[{idx}]")
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                dfs(item, f"{path}[{idx}]")

    dfs(obj)
    return br, bs, found_keys

# -----------------------
# HTTP fetch with retries and optional pagination
# -----------------------
async def fetch_with_retries(session: aiohttp.ClientSession, url: str, params=None) -> Optional[Any]:
    backoff = RETRY_BACKOFF_BASE
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
            async with session.get(url, params=params, timeout=timeout) as resp:
                text = await resp.text()
                if resp.status == 200:
                    try:
                        return await resp.json()
                    except Exception:
                        logger.warning("JSON parse error from %s; returning raw text for inspection", url)
                        return {"__raw_text": text}
                else:
                    logger.warning("Non-200 from %s: %s body: %.300s", url, resp.status, text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Error fetching %s (attempt %d/%d): %s", url, attempt, MAX_RETRIES, e)
        await asyncio.sleep(backoff)
        backoff *= 2
    logger.error("Failed to fetch %s after %d attempts", url, MAX_RETRIES)
    return None

async def fetch_all_devices(session: aiohttp.ClientSession) -> Optional[list]:
    """
    Fetch devices. If GenieACS returns paginated 'items', iterate pages using skip/limit.
    If single list returned, return it directly.
    """
    data = await fetch_with_retries(session, GENIEACS_URL)
    if data is None:
        return None

    # If raw text returned
    if isinstance(data, dict) and "__raw_text" in data:
        logger.error("GenieACS returned non-JSON payload; inspect raw text")
        logger.debug(data["__raw_text"][:2000])
        return None

    # If API returns {"items": [...], "total": N}
    if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
        items = data["items"]
        # try pagination if total > len(items)
        total = data.get("total")
        if total and total > len(items):
            # fetch remaining pages
            skip = len(items)
            while skip < total:
                params = {"limit": PAGE_LIMIT, "skip": skip}
                page = await fetch_with_retries(session, GENIEACS_URL, params=params)
                if page is None:
                    break
                if isinstance(page, dict) and "items" in page and isinstance(page["items"], list):
                    items.extend(page["items"])
                    skip += len(page["items"])
                else:
                    break
        return items

    # If API returns a list
    if isinstance(data, list):
        return data

    # If single device object
    if isinstance(data, dict):
        return [data]

    logger.warning("Unexpected payload type from GenieACS: %s", type(data))
    return None

# -----------------------
# Poller
# -----------------------
async def poller_loop():
    logger.info("Starting poller: %s every %ds", GENIEACS_URL, POLL_INTERVAL_SECONDS)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY_LIMIT)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            start = datetime.now(timezone.utc)
            try:
                with poll_duration.time():
                    devices = await fetch_all_devices(session)
                if devices is None:
                    poll_success.set(0)
                    logger.error("No payload from GenieACS")
                else:
                    now = datetime.now(timezone.utc)
                    cutoff = now - timedelta(seconds=ACTIVE_WINDOW_SECONDS)
                    keep_cutoff = now - timedelta(seconds=KEEP_LAST_KNOWN_SECONDS)
                    updated: Dict[str, Dict[str, Any]] = {}
                    parsed_count = 0
                    sample_examples = []

                    for dev in devices:
                        try:
                            device_id = dev.get("_id") or dev.get("id") or dev.get("serialNumber")
                            if not device_id:
                                continue
                            last_inform_dt = parse_last_inform(dev.get("_lastInform") or dev.get("lastInform"))
                            if last_inform_dt is None:
                                continue

                            # Prefer the InternetGatewayDevice subtree if present
                            params = dev.get("InternetGatewayDevice") or dev.get("parameters") or dev.get("data") or dev

                            br, bs, found = find_bytes_in_tree(params)
                            br = br or 0
                            bs = bs or 0

                            is_active = last_inform_dt >= cutoff

                            updated[device_id] = {
                                "bytes_received": br,
                                "bytes_sent": bs,
                                "last_inform": last_inform_dt.isoformat(),
                                "found_keys": found[:3],
                                "last_seen": last_inform_dt.isoformat(),
                                "active": is_active,
                            }
                            parsed_count += 1
                            if len(sample_examples) < 5:
                                sample_examples.append({"id": device_id, "br": br, "bs": bs, "found": found[:3], "active": is_active})
                        except Exception:
                            logger.exception("Error processing device entry")

                    # Merge with existing to keep last-known values for devices not present in this poll
                    async with device_metrics_lock:
                        merged = dict(updated)  # start with freshly seen devices
                        for dev_id, old in device_metrics.items():
                            if dev_id in merged:
                                continue
                            # determine old last seen
                            old_last_iso = old.get("last_seen") or old.get("last_inform")
                            old_last = parse_last_inform(old_last_iso) if old_last_iso else None
                            if old_last:
                                # if old_last is within KEEP_LAST_KNOWN_SECONDS, keep last-known values
                                if old_last >= keep_cutoff:
                                    # preserve bytes and last_seen, but mark active depending on cutoff
                                    preserved = dict(old)
                                    preserved["active"] = True if old_last >= cutoff else False
                                    merged[dev_id] = preserved
                                else:
                                    # older than keep window: keep last-known but mark inactive
                                    preserved = dict(old)
                                    preserved["active"] = False
                                    merged[dev_id] = preserved
                            else:
                                # no timestamp: keep as-is but mark inactive
                                preserved = dict(old)
                                preserved["active"] = False
                                merged[dev_id] = preserved

                        # atomic replace
                        device_metrics.clear()
                        device_metrics.update(merged)

                    active_count = sum(1 for v in merged.values() if v.get("active"))
                    active_devices_g.set(active_count)
                    poll_success.set(1)
                    logger.info("Poll complete: active=%d parsed=%d examples=%s", active_count, parsed_count, sample_examples)
            except Exception:
                poll_success.set(0)
                logger.exception("Unhandled exception in poller loop")
            # sleep until next poll
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

# -----------------------
# FastAPI app and endpoints
# -----------------------
app = FastAPI(title="GenieACS Prometheus Exporter")

@app.on_event("startup")
async def startup_event():
    # start poller
    asyncio.create_task(poller_loop())

@app.get("/health")
async def health():
    return {"status": "ok", "active_devices": len(device_metrics)}

@app.get("/debug/sample")
async def debug_sample():
    # return first 10 parsed entries for inspection
    async with device_metrics_lock:
        items = list(device_metrics.items())[:10]
    return {"sample_parsed": items}

@app.get("/metrics")
async def metrics():
    # combine device metrics (REGISTRY) and internal metrics (PROM_REG)
    data = generate_latest(REGISTRY) + generate_latest(PROM_REG)
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)

# -----------------------
# Run with:
# uvicorn exp:app --host 0.0.0.0 --port 9406
# -----------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("exp:app", host="0.0.0.0", port=int(os.getenv("PORT", "9406")), log_level="info")
