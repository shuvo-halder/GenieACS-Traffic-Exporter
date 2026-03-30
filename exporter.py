#!/usr/bin/env python3
"""
Async Python exporter that polls GenieACS for device counters and exposes Prometheus metrics:
- genieacs_device_rx_bytes{device_id}
- genieacs_device_tx_bytes{device_id}
- genieacs_device_rx_rate_bps{device_id}
- genieacs_device_tx_rate_bps{device_id}

Features:
- Async fetching with aiohttp
- Batching / pagination
- Concurrency limit and backoff
- Handles counter resets
- Filters by ONLINE devices using lastInform
- Optional Redis persistence for previous counters (recommended for restarts)
- Dynamic parameter paths support via DEVICE_PARAM_PATHS env var
- Graceful handling of missing parameters
- Docker-friendly and configurable via env vars
"""

import os
import asyncio
import time
import json
import logging
from typing import Dict, Any, List, Optional, Tuple
import aiohttp
from aiohttp import ClientTimeout
from prometheus_client import start_http_server, Gauge, REGISTRY
import math

# Optional Redis for persistence across restarts
try:
    import aioredis
except Exception:
    aioredis = None

# ---------------------------
# Configuration via env vars
# ---------------------------
GENIEACS_API_URL = os.getenv("GENIEACS_API_URL", "http://genieacs:7557")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "15"))  # seconds
DEVICE_QUERY_FILTER = os.getenv("DEVICE_QUERY_FILTER", "{}")  # JSON filter for GenieACS devices endpoint
DEVICE_PARAM_PATHS = os.getenv(
    "DEVICE_PARAM_PATHS",
    "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.Stats.BytesReceived;InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.Stats.BytesSent"
)
# Comma or semicolon separated list of parameter paths for RX and TX (first two are used by default)
ONLINE_THRESHOLD_SECONDS = int(os.getenv("ONLINE_THRESHOLD_SECONDS", str(5 * 60)))  # lastInform within 5 minutes
CONCURRENCY = int(os.getenv("CONCURRENCY", "200"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "200"))  # devices per page
GENIEACS_TIMEOUT = int(os.getenv("GENIEACS_TIMEOUT", "20"))
EXPORTER_PORT = int(os.getenv("EXPORTER_PORT", "9410"))
REDIS_URL = os.getenv("REDIS_URL", "")  # optional, e.g., redis://redis:6379/0
CACHE_TTL = int(os.getenv("CACHE_TTL", str(60 * 60 * 24)))  # seconds for persisted state

# Optional authentication for GenieACS API (Basic Auth)
GENIEACS_API_USER = os.getenv("GENIEACS_API_USER", "")
GENIEACS_API_PASS = os.getenv("GENIEACS_API_PASS", "")

# Parameter mapping: allow dynamic vendor-specific paths
# Provide as semicolon separated list: rx_path;tx_path
param_paths = [p.strip() for p in DEVICE_PARAM_PATHS.replace(",", ";").split(";") if p.strip()]
RX_PARAM_PATH = param_paths[0] if len(param_paths) >= 1 else ""
TX_PARAM_PATH = param_paths[1] if len(param_paths) >= 2 else ""

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("genieacs_exporter")

# ---------------------------
# Prometheus metrics
# ---------------------------
g_rx_bytes = Gauge(
    "genieacs_device_rx_bytes",
    "Total bytes received (counter) reported by device",
    ["device_id", "serial", "ip"]
)
g_tx_bytes = Gauge(
    "genieacs_device_tx_bytes",
    "Total bytes sent (counter) reported by device",
    ["device_id", "serial", "ip"]
)
g_rx_rate = Gauge(
    "genieacs_device_rx_rate_bps",
    "Inbound bandwidth rate in bits per second",
    ["device_id", "serial", "ip"]
)
g_tx_rate = Gauge(
    "genieacs_device_tx_rate_bps",
    "Outbound bandwidth rate in bits per second",
    ["device_id", "serial", "ip"]
)
g_up = Gauge("genieacs_exporter_up", "Exporter health (1 = running)", [])
g_polled_devices = Gauge("genieacs_polled_devices_total", "Number of devices polled in last cycle", [])

# ---------------------------
# State management
# ---------------------------
class StateStore:
    """
    Simple pluggable state store. Uses Redis if REDIS_URL provided and aioredis installed,
    otherwise falls back to in-memory dict (non-persistent).
    Stores previous counters and timestamps for rate calculation.
    """

    def __init__(self):
        self._mem: Dict[str, Dict[str, Any]] = {}
        self._redis = None
        self._use_redis = False

    async def init(self):
        if REDIS_URL and aioredis:
            try:
                self._redis = await aioredis.from_url(REDIS_URL)
                self._use_redis = True
                logger.info("Using Redis state store at %s", REDIS_URL)
            except Exception as e:
                logger.warning("Failed to connect to Redis (%s). Falling back to in-memory store. Error: %s", REDIS_URL, e)
                self._use_redis = False
        else:
            if REDIS_URL and not aioredis:
                logger.warning("REDIS_URL provided but aioredis not installed. Falling back to in-memory store.")
            self._use_redis = False

    async def get(self, device_id: str) -> Optional[Dict[str, Any]]:
        if self._use_redis:
            try:
                raw = await self._redis.get(f"genieacs_state:{device_id}")
                if raw:
                    return json.loads(raw)
                return None
            except Exception as e:
                logger.debug("Redis get error: %s", e)
                return None
        else:
            return self._mem.get(device_id)

    async def set(self, device_id: str, value: Dict[str, Any]):
        if self._use_redis:
            try:
                await self._redis.set(f"genieacs_state:{device_id}", json.dumps(value), ex=CACHE_TTL)
            except Exception as e:
                logger.debug("Redis set error: %s", e)
        else:
            self._mem[device_id] = value

    async def close(self):
        if self._redis:
            await self._redis.close()

state_store = StateStore()

# ---------------------------
# Helper functions
# ---------------------------
def safe_int(v) -> Optional[int]:
    try:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return int(v)
        return int(str(v))
    except Exception:
        return None

def parse_last_inform(device: Dict[str, Any]) -> Optional[float]:
    """
    GenieACS device object typically has lastInform as ISO timestamp string or epoch.
    We'll try to parse common formats. If missing, return None.
    """
    last = device.get("lastInform") or device.get("last_inform") or device.get("lastInformTime")
    if not last:
        return None
    # If it's numeric epoch
    try:
        if isinstance(last, (int, float)):
            return float(last)
        s = str(last)
        # Try ISO format
        try:
            # Python 3.11 has fromisoformat with Z handling; use fallback
            from datetime import datetime
            # strip Z
            s2 = s.rstrip("Z")
            dt = datetime.fromisoformat(s2)
            return dt.timestamp()
        except Exception:
            # try parse as float
            return float(s)
    except Exception:
        return None

def is_online(device: Dict[str, Any]) -> bool:
    ts = parse_last_inform(device)
    if ts is None:
        return False
    return (time.time() - ts) <= ONLINE_THRESHOLD_SECONDS

def extract_param_value(params: Dict[str, Any], path_pattern: str) -> Optional[int]:
    """
    Support wildcard '*' in path segments. GenieACS parameters endpoint returns a dict of parameter objects.
    We will search keys that match the pattern (simple wildcard matching).
    Example pattern: InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.Stats.BytesReceived
    """
    if not params:
        return None
    # Convert pattern to prefix and suffix segments for simple matching
    # We'll treat '*' as match-any-segment (no dots inside segment)
    pattern_segments = path_pattern.split(".")
    candidates = []
    for key, val in params.items():
        key_segments = key.split(".")
        if len(key_segments) != len(pattern_segments):
            # allow mismatch lengths by trying to match with '*' absorbing segments? Simpler: check if pattern segments with '*' can match variable segments
            # We'll do a simple approach: match from end for suffix segments that are not '*'
            pass
        match = True
        # We'll attempt to match by iterating pattern segments and key segments; '*' matches any single segment
        if len(key_segments) != len(pattern_segments):
            match = False
        else:
            for ps, ks in zip(pattern_segments, key_segments):
                if ps == "*":
                    continue
                if ps != ks:
                    match = False
                    break
        if match:
            # val may be dict with "value" or raw
            if isinstance(val, dict) and "value" in val:
                candidates.append(safe_int(val["value"]))
            else:
                candidates.append(safe_int(val))
    # If no exact-length matches, try suffix match: last N segments must match where pattern has no leading '*'
    if not candidates:
        # fallback: find keys that endwith the non-wildcard suffix
        suffix_parts = []
        for seg in reversed(pattern_segments):
            if seg == "*":
                break
            suffix_parts.insert(0, seg)
        if suffix_parts:
            suffix = ".".join(suffix_parts)
            for key, val in params.items():
                if key.endswith(suffix):
                    if isinstance(val, dict) and "value" in val:
                        candidates.append(safe_int(val["value"]))
                    else:
                        candidates.append(safe_int(val))
    if not candidates:
        return None
    # If multiple matches, pick the largest (likely aggregated) or the first non-null
    filtered = [c for c in candidates if c is not None]
    if not filtered:
        return None
    return max(filtered)

# ---------------------------
# GenieACS API client
# ---------------------------
class GenieACSClient:
    def __init__(self, base_url: str, session: aiohttp.ClientSession):
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.auth = None
        if GENIEACS_API_USER:
            self.auth = aiohttp.BasicAuth(GENIEACS_API_USER, GENIEACS_API_PASS)

    async def list_devices(self, page: int = 0, limit: int = 200, query_filter: str = "{}") -> Tuple[List[Dict[str, Any]], bool]:
        """
        Returns (devices, has_more)
        Uses GenieACS /devices endpoint with pagination parameters.
        The exact API shape may vary by GenieACS version; this function uses a generic approach:
        GET /devices?query=<json>&limit=<limit>&skip=<skip>
        """
        skip = page * limit
        url = f"{self.base_url}/devices?limit={limit}&skip={skip}"
        # attach query filter if provided
        if query_filter and query_filter != "{}":
            url += f"&query={aiohttp.helpers.quote(query_filter)}"
        try:
            async with self.session.get(url, auth=self.auth, timeout=ClientTimeout(total=GENIEACS_TIMEOUT)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("list_devices: non-200 %s: %s", resp.status, text[:200])
                    return [], False
                data = await resp.json()
                # GenieACS returns list and total? We'll assume list
                devices = data if isinstance(data, list) else data.get("data", data.get("devices", []))
                # Determine has_more by length
                has_more = len(devices) == limit
                return devices, has_more
        except Exception as e:
            logger.exception("Error listing devices: %s", e)
            return [], False

    async def get_device_parameters(self, device_id: str) -> Dict[str, Any]:
        """
        GET /devices/{id}/parameters
        """
        url = f"{self.base_url}/devices/{device_id}/parameters"
        try:
            async with self.session.get(url, auth=self.auth, timeout=ClientTimeout(total=GENIEACS_TIMEOUT)) as resp:
                if resp.status != 200:
                    logger.debug("get_device_parameters %s returned %s", device_id, resp.status)
                    return {}
                data = await resp.json()
                # GenieACS returns dict of parameter objects
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.debug("get_device_parameters error for %s: %s", device_id, e)
            return {}

# ---------------------------
# Polling and metric update
# ---------------------------
async def process_device(client: GenieACSClient, device: Dict[str, Any], now_ts: float):
    device_id = device.get("_id") or device.get("id") or device.get("deviceId") or device.get("serialNumber") or device.get("name")
    serial = device.get("serialNumber") or device.get("serial") or ""
    ip = device.get("ip") or device.get("wan_ip") or ""
    if not device_id:
        logger.debug("Skipping device with no id: %s", device)
        return

    if not is_online(device):
        # Clear metrics for offline devices to avoid stale graphs
        try:
            g_rx_bytes.remove(device_id, serial, ip)
            g_tx_bytes.remove(device_id, serial, ip)
            g_rx_rate.remove(device_id, serial, ip)
            g_tx_rate.remove(device_id, serial, ip)
        except KeyError:
            pass
        return

    params = await client.get_device_parameters(device_id)
    rx_val = extract_param_value(params, RX_PARAM_PATH) if RX_PARAM_PATH else None
    tx_val = extract_param_value(params, TX_PARAM_PATH) if TX_PARAM_PATH else None

    # Update raw counters (set to 0 if missing)
    if rx_val is not None:
        g_rx_bytes.labels(device_id=device_id, serial=serial, ip=ip).set(rx_val)
    else:
        # missing parameter: set nothing but log at debug
        logger.debug("Missing RX param for device %s", device_id)

    if tx_val is not None:
        g_tx_bytes.labels(device_id=device_id, serial=serial, ip=ip).set(tx_val)
    else:
        logger.debug("Missing TX param for device %s", device_id)

    # Rate calculation
    prev = await state_store.get(device_id) or {}
    prev_ts = prev.get("ts")
    prev_rx = prev.get("rx")
    prev_tx = prev.get("tx")

    # If no previous sample, store current and skip rate until next poll
    if prev_ts is None:
        await state_store.set(device_id, {"ts": now_ts, "rx": rx_val, "tx": tx_val})
        # set rates to 0 for initial sample
        g_rx_rate.labels(device_id=device_id, serial=serial, ip=ip).set(0.0)
        g_tx_rate.labels(device_id=device_id, serial=serial, ip=ip).set(0.0)
        return

    elapsed = now_ts - prev_ts if prev_ts else None
    if not elapsed or elapsed <= 0:
        # avoid division by zero
        return

    # RX rate
    rx_rate_bps = 0.0
    if rx_val is not None and prev_rx is not None:
        delta = rx_val - prev_rx
        if delta < 0:
            # counter reset or rollover; assume 32/64-bit counter unknown: treat delta as rx_val (best-effort)
            delta = rx_val
        # bytes/sec -> bits/sec
        rx_rate_bps = (delta / elapsed) * 8.0
        # sanitize NaN/inf
        if not math.isfinite(rx_rate_bps) or rx_rate_bps < 0:
            rx_rate_bps = 0.0
    else:
        rx_rate_bps = 0.0

    # TX rate
    tx_rate_bps = 0.0
    if tx_val is not None and prev_tx is not None:
        delta = tx_val - prev_tx
        if delta < 0:
            delta = tx_val
        tx_rate_bps = (delta / elapsed) * 8.0
        if not math.isfinite(tx_rate_bps) or tx_rate_bps < 0:
            tx_rate_bps = 0.0
    else:
        tx_rate_bps = 0.0

    g_rx_rate.labels(device_id=device_id, serial=serial, ip=ip).set(rx_rate_bps)
    g_tx_rate.labels(device_id=device_id, serial=serial, ip=ip).set(tx_rate_bps)

    # Persist current sample
    await state_store.set(device_id, {"ts": now_ts, "rx": rx_val, "tx": tx_val})

async def poll_loop():
    timeout = ClientTimeout(total=GENIEACS_TIMEOUT)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY, force_close=False)
    auth = None
    headers = {"Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        client = GenieACSClient(GENIEACS_API_URL, session)
        await state_store.init()
        g_up.set(1)
        while True:
            start = time.time()
            polled = 0
            try:
                page = 0
                tasks = []
                sem = asyncio.Semaphore(CONCURRENCY)
                devices_this_cycle = []

                # Pagination loop
                while True:
                    devices, has_more = await client.list_devices(page=page, limit=BATCH_SIZE, query_filter=DEVICE_QUERY_FILTER)
                    if not devices:
                        break
                    devices_this_cycle.extend(devices)
                    page += 1
                    if not has_more:
                        break

                polled = len(devices_this_cycle)
                g_polled_devices.set(polled)
                now_ts = time.time()

                # Process devices in parallel with concurrency control
                async def sem_task(dev):
                    async with sem:
                        await process_device(client, dev, now_ts)

                tasks = [asyncio.create_task(sem_task(d)) for d in devices_this_cycle]
                if tasks:
                    await asyncio.gather(*tasks)

                logger.info("Polled %d devices in %.2fs", polled, time.time() - start)
            except Exception as e:
                logger.exception("Error during poll loop: %s", e)
                g_up.set(0)
            # Sleep until next interval, accounting for time spent
            elapsed = time.time() - start
            to_sleep = max(0, POLL_INTERVAL - elapsed)
            await asyncio.sleep(to_sleep)

# ---------------------------
# Entrypoint
# ---------------------------
def main():
    logger.info("Starting GenieACS Prometheus exporter on :%d", EXPORTER_PORT)
    start_http_server(EXPORTER_PORT)
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(poll_loop())
    except KeyboardInterrupt:
        logger.info("Shutting down exporter")
    finally:
        loop.run_until_complete(state_store.close())

if __name__ == "__main__":
    main()