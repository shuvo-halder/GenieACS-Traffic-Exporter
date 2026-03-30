#!/usr/bin/env python3
"""
GenieACS Bandwidth Exporter (async, production-ready)

Environment variables:
  GENIEACS_API_URL            - Base URL for GenieACS API (required)
  GENIEACS_USERNAME           - Basic auth username (optional)
  GENIEACS_PASSWORD           - Basic auth password (optional)
  GENIEACS_API_TOKEN          - API token (optional)
  GENIEACS_API_TOKEN_HEADER   - Header name for token (default: Authorization)
  POLL_INTERVAL               - Poll interval in seconds (default: 30)
  HTTP_PORT                   - Port to expose /metrics (default: 8000)
  DEVICE_QUERY_FILTER         - Optional query/filter to pass to device list endpoint
  PARAM_PATHS                 - Comma-separated parameter paths to read (default two WAN paths)
  PAGE_SIZE                   - Pagination page size (default: 500)
  CONCURRENCY                 - Max concurrent HTTP requests (default: 50)
  REQUEST_TIMEOUT             - HTTP request timeout seconds (default: 15)
  ENABLE_PER_DEVICE_METRICS   - "true" to enable per-device metrics (default: true)
  MAX_DEVICE_METRICS          - Max number of device metrics to expose (default: 20000)
  PERSIST_DB_PATH             - Path to sqlite DB for persistent counters (default: /data/genieacs_bw.db)
  USER_AGENT                  - HTTP user agent (default: genieacs-bw-exporter/1.0)
  ONLINE_THRESHOLD_SECONDS    - Seconds since lastInform to consider device online (default: 300)
"""

import os
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

import aiohttp
import aiosqlite
from prometheus_client import start_http_server, Gauge, Counter, Summary

# ---------------------------
# Configuration
# ---------------------------
GENIEACS_API_URL = os.getenv("GENIEACS_API_URL", "").rstrip("/")
if not GENIEACS_API_URL:
    raise SystemExit("GENIEACS_API_URL environment variable is required")

GENIEACS_USERNAME = os.getenv("GENIEACS_USERNAME")
GENIEACS_PASSWORD = os.getenv("GENIEACS_PASSWORD")
GENIEACS_API_TOKEN = os.getenv("GENIEACS_API_TOKEN")
GENIEACS_API_TOKEN_HEADER = os.getenv("GENIEACS_API_TOKEN_HEADER", "Authorization")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
HTTP_PORT = int(os.getenv("HTTP_PORT", "8000"))
DEVICE_QUERY_FILTER = os.getenv("DEVICE_QUERY_FILTER", "")  # passed as query param 'q' or similar
PARAM_PATHS = os.getenv(
    "PARAM_PATHS",
    "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.Stats.BytesReceived,"
    "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.Stats.BytesSent"
)
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "500"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "50"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
ENABLE_PER_DEVICE_METRICS = os.getenv("ENABLE_PER_DEVICE_METRICS", "true").lower() in ("1", "true", "yes")
MAX_DEVICE_METRICS = int(os.getenv("MAX_DEVICE_METRICS", "20000"))
PERSIST_DB_PATH = os.getenv("PERSIST_DB_PATH", "/data/genieacs_bw.db")
USER_AGENT = os.getenv("USER_AGENT", "genieacs-bw-exporter/1.0")
ONLINE_THRESHOLD_SECONDS = int(os.getenv("ONLINE_THRESHOLD_SECONDS", "300"))

# Derived
PARAM_PATH_LIST = [p.strip() for p in PARAM_PATHS.split(",") if p.strip()]

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("genieacs_bw_exporter")

# ---------------------------
# Prometheus metrics
# ---------------------------
# Per-device bytes (gauges) and rates (gauges). Labels: device_id
GENIEACS_DEVICE_RX_BYTES = Gauge(
    "genieacs_device_rx_bytes",
    "Latest BytesReceived counter per device",
    ["device_id"]
)
GENIEACS_DEVICE_TX_BYTES = Gauge(
    "genieacs_device_tx_bytes",
    "Latest BytesSent counter per device",
    ["device_id"]
)
GENIEACS_DEVICE_RX_RATE_BPS = Gauge(
    "genieacs_device_rx_rate_bps",
    "Calculated receive rate in bits per second per device",
    ["device_id"]
)
GENIEACS_DEVICE_TX_RATE_BPS = Gauge(
    "genieacs_device_tx_rate_bps",
    "Calculated transmit rate in bits per second per device",
    ["device_id"]
)

# Aggregates
GENIEACS_TOTAL_DEVICES = Gauge("genieacs_total_devices", "Total devices considered")
GENIEACS_MONITORED_DEVICES = Gauge("genieacs_monitored_devices", "Devices with metrics exported")
GENIEACS_EXPORTER_ERRORS = Counter("genieacs_exporter_errors_total", "Exporter errors total")
GENIEACS_POLL_DURATION = Summary("genieacs_exporter_poll_duration_seconds", "Poll cycle duration seconds")

# ---------------------------
# Utilities
# ---------------------------
def parse_iso8601_to_epoch(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        try:
            # fallback common format
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            logger.debug("Failed to parse timestamp: %s", ts)
            return None

def is_online(last_inform_epoch: Optional[float], threshold: int = ONLINE_THRESHOLD_SECONDS) -> bool:
    if last_inform_epoch is None:
        return False
    return (time.time() - last_inform_epoch) <= threshold

# ---------------------------
# GenieACS API client
# ---------------------------
class GenieACSClient:
    def __init__(self, base_url: str, session: aiohttp.ClientSession, page_size: int = 500, concurrency: int = 50):
        self.base_url = base_url
        self.session = session
        self.page_size = page_size
        self.semaphore = asyncio.Semaphore(concurrency)

    async def _get(self, path: str, params: Dict[str, Any] = None) -> Any:
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if GENIEACS_API_TOKEN:
            # default header is Authorization: Bearer <token>
            if GENIEACS_API_TOKEN_HEADER.lower() == "authorization":
                headers["Authorization"] = f"Bearer {GENIEACS_API_TOKEN}"
            else:
                headers[GENIEACS_API_TOKEN_HEADER] = GENIEACS_API_TOKEN

        auth = None
        if GENIEACS_USERNAME and GENIEACS_PASSWORD:
            auth = aiohttp.BasicAuth(GENIEACS_USERNAME, GENIEACS_PASSWORD)

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with self.semaphore:
            async with self.session.get(url, params=params, headers=headers, auth=auth, timeout=timeout) as resp:
                resp.raise_for_status()
                ct = resp.headers.get("Content-Type", "")
                if "application/json" in ct:
                    return await resp.json()
                else:
                    return await resp.text()

    async def list_devices_paginated(self, device_filter: str = "", fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Try to fetch devices in pages. If the API supports fields selection, pass them.
        This function is resilient to different GenieACS shapes; adjust if your API differs.
        """
        devices: List[Dict[str, Any]] = []
        skip = 0
        while True:
            params = {"limit": self.page_size, "skip": skip}
            if device_filter:
                # common GenieACS uses 'q' or 'filter' depending on deployment; try 'q'
                params["q"] = device_filter
            if fields:
                # some APIs accept 'fields' as comma-separated
                params["fields"] = ",".join(fields)
            try:
                data = await self._get("/devices", params=params)
            except Exception:
                raise
            batch = self._extract_device_list(data)
            if not batch:
                break
            devices.extend(batch)
            if len(batch) < self.page_size:
                break
            skip += len(batch)
            # safety
            if skip > 10000000:
                logger.error("Pagination safety break")
                break
        return devices

    def _extract_device_list(self, resp: Any) -> List[Dict[str, Any]]:
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            # GenieACS sometimes returns {"data": [...]} or {"devices": [...]}
            if "data" in resp and isinstance(resp["data"], list):
                return resp["data"]
            if "devices" in resp and isinstance(resp["devices"], list):
                return resp["devices"]
            # fallback: find first list value
            for v in resp.values():
                if isinstance(v, list):
                    return v
        return []

    async def get_device_parameters(self, device_id: str) -> Dict[str, Any]:
        """
        Fallback per-device parameters fetch. Endpoint may vary; common path: /devices/{id}/parameters
        """
        try:
            data = await self._get(f"/devices/{device_id}/parameters")
            # return as dict param_path -> value
            if isinstance(data, dict):
                return data
            return {}
        except Exception:
            logger.debug("Failed to fetch parameters for device %s", device_id)
            return {}

# ---------------------------
# Persistent store for counters (sqlite)
# ---------------------------
class CounterStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def init(self):
        async with self._init_lock:
            if self._initialized:
                return
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
            self.conn = await aiosqlite.connect(self.db_path)
            await self.conn.execute(
                "CREATE TABLE IF NOT EXISTS device_counters (device_id TEXT PRIMARY KEY, rx_bytes INTEGER, tx_bytes INTEGER, ts REAL)"
            )
            await self.conn.commit()
            self._initialized = True

    async def get(self, device_id: str) -> Tuple[int, int, float]:
        await self.init()
        async with self.conn.execute("SELECT rx_bytes, tx_bytes, ts FROM device_counters WHERE device_id = ?", (device_id,)) as cur:
            row = await cur.fetchone()
            if row:
                return int(row[0] or 0), int(row[1] or 0), float(row[2] or 0.0)
            return 0, 0, 0.0

    async def upsert(self, device_id: str, rx_bytes: int, tx_bytes: int, ts: float):
        await self.init()
        await self.conn.execute(
            "INSERT INTO device_counters(device_id, rx_bytes, tx_bytes, ts) VALUES(?,?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET rx_bytes=excluded.rx_bytes, tx_bytes=excluded.tx_bytes, ts=excluded.ts",
            (device_id, rx_bytes, tx_bytes, ts)
        )
        await self.conn.commit()

    async def close(self):
        if getattr(self, "conn", None):
            await self.conn.close()

# ---------------------------
# Exporter logic
# ---------------------------
class BandwidthExporter:
    def __init__(self, client: GenieACSClient, store: CounterStore, poll_interval: int = 30):
        self.client = client
        self.store = store
        self.poll_interval = poll_interval
        self.lock = asyncio.Lock()
        self.running = True

    async def fetch_and_process(self):
        start = time.time()
        try:
            # Try to request device list with minimal fields to reduce payload.
            # We request lastInform and optionally parameter values if API supports fields selection.
            fields = ["_id", "lastInform"]
            # Some GenieACS allow nested parameter selection; we attempt to request parameter paths as fields to reduce per-device calls.
            # If that fails, we fallback to per-device parameter fetch.
            # Build a best-effort fields list
            # Note: many GenieACS deployments do not support fields param; the client will ignore it.
            devices = await self.client.list_devices_paginated(device_filter=DEVICE_QUERY_FILTER, fields=fields)
        except Exception as e:
            GENIEACS_EXPORTER_ERRORS.inc()
            logger.exception("Failed to list devices: %s", e)
            return

        total_devices = len(devices)
        GENIEACS_TOTAL_DEVICES.set(total_devices)
        monitored = 0

        # Process devices in batches concurrently
        sem = asyncio.Semaphore(CONCURRENCY)
        tasks = []
        for dev in devices:
            tasks.append(self._process_device(dev, sem))
        # run tasks with concurrency
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                GENIEACS_EXPORTER_ERRORS.inc()
                logger.debug("Device task error: %s", r)
            elif r:
                monitored += 1

        GENIEACS_MONITORED_DEVICES.set(monitored)
        duration = time.time() - start
        logger.info("Poll finished: total=%d monitored=%d duration=%.2fs", total_devices, monitored, duration)

    async def _process_device(self, dev: Dict[str, Any], sem: asyncio.Semaphore) -> bool:
        """
        Process a single device:
          - determine device_id
          - check lastInform (online)
          - fetch parameter counters (either from device object or per-device endpoint)
          - compute delta and rate, handle resets
          - persist counters
          - set Prometheus metrics (if enabled and under cap)
        Returns True if metrics exported for this device.
        """
        async with sem:
            device_id = str(dev.get("_id") or dev.get("id") or dev.get("serialNumber") or dev.get("name") or "unknown")
            last_inform = dev.get("lastInform") or dev.get("lastContact") or None
            last_inform_epoch = parse_iso8601_to_epoch(last_inform) if isinstance(last_inform, str) else None

            if not is_online(last_inform_epoch):
                # skip offline devices
                return False

            # Attempt to extract counters from device object if present
            rx_val = None
            tx_val = None

            # Some APIs include parameters in device object under 'parameters' or 'data'
            params = {}
            if isinstance(dev, dict):
                if "parameters" in dev and isinstance(dev["parameters"], dict):
                    params = dev["parameters"]
                elif "data" in dev and isinstance(dev["data"], dict):
                    params = dev["data"]

            # Try to find counters in params by matching suffixes of parameter paths
            for p in PARAM_PATH_LIST:
                # exact match
                if p in params:
                    val = params.get(p)
                    if p.lower().endswith("bytesreceived"):
                        rx_val = _safe_int(val)
                    elif p.lower().endswith("bytessent"):
                        tx_val = _safe_int(val)
                else:
                    # try to find by last token
                    last_token = p.split(".")[-1].lower()
                    for k, v in params.items():
                        if k.lower().endswith(last_token):
                            if last_token == "bytesreceived":
                                rx_val = _safe_int(v)
                            elif last_token == "bytessent":
                                tx_val = _safe_int(v)

            # If missing counters, fallback to per-device parameter fetch
            if rx_val is None or tx_val is None:
                # fetch per-device parameters (best-effort)
                try:
                    param_map = await self.client.get_device_parameters(device_id)
                    for p in PARAM_PATH_LIST:
                        if p in param_map:
                            if p.lower().endswith("bytesreceived"):
                                rx_val = _safe_int(param_map.get(p))
                            elif p.lower().endswith("bytessent"):
                                tx_val = _safe_int(param_map.get(p))
                    # also try last token matching
                    for k, v in param_map.items():
                        kt = k.lower()
                        if kt.endswith("bytesreceived") and rx_val is None:
                            rx_val = _safe_int(v)
                        if kt.endswith("bytessent") and tx_val is None:
                            tx_val = _safe_int(v)
                except Exception:
                    logger.debug("Per-device parameter fetch failed for %s", device_id)

            # If still missing, skip gracefully
            if rx_val is None and tx_val is None:
                logger.debug("No counters for device %s; skipping", device_id)
                return False

            # Persisted previous counters
            try:
                prev_rx, prev_tx, prev_ts = await self.store.get(device_id)
            except Exception:
                GENIEACS_EXPORTER_ERRORS.inc()
                logger.exception("Failed to read store for %s", device_id)
                prev_rx, prev_tx, prev_ts = 0, 0, 0.0

            now_ts = time.time()
            # compute deltas and rates
            rx_delta = _compute_delta(prev_rx, rx_val) if rx_val is not None else 0
            tx_delta = _compute_delta(prev_tx, tx_val) if tx_val is not None else 0
            time_delta = now_ts - prev_ts if prev_ts and now_ts > prev_ts else POLL_INTERVAL

            # bytes per second
            rx_bps = (rx_delta / time_delta) if time_delta > 0 else 0.0
            tx_bps = (tx_delta / time_delta) if time_delta > 0 else 0.0

            # convert to bits per second
            rx_bps_bits = rx_bps * 8.0
            tx_bps_bits = tx_bps * 8.0

            # Persist current counters
            try:
                await self.store.upsert(device_id, rx_val or 0, tx_val or 0, now_ts)
            except Exception:
                GENIEACS_EXPORTER_ERRORS.inc()
                logger.exception("Failed to persist counters for %s", device_id)

            # Export metrics if under cap
            # To avoid exploding cardinality, enforce MAX_DEVICE_METRICS
            # We count current number of monitored devices via sqlite row count (cheapish)
            export_allowed = True
            try:
                # quick check: if MAX_DEVICE_METRICS reached, skip new devices
                async with self.store.conn.execute("SELECT COUNT(*) FROM device_counters") as cur:
                    row = await cur.fetchone()
                    count = int(row[0] or 0)
                    if count > MAX_DEVICE_METRICS:
                        export_allowed = False
                        logger.debug("Device metrics cap reached (%d > %d); skipping export for %s", count, MAX_DEVICE_METRICS, device_id)
            except Exception:
                # if store not ready, allow export
                export_allowed = True

            if ENABLE_PER_DEVICE_METRICS and export_allowed:
                try:
                    if rx_val is not None:
                        GENIEACS_DEVICE_RX_BYTES.labels(device_id=device_id).set(rx_val)
                        GENIEACS_DEVICE_RX_RATE_BPS.labels(device_id=device_id).set(rx_bps_bits)
                    if tx_val is not None:
                        GENIEACS_DEVICE_TX_BYTES.labels(device_id=device_id).set(tx_val)
                        GENIEACS_DEVICE_TX_RATE_BPS.labels(device_id=device_id).set(tx_bps_bits)
                except Exception:
                    GENIEACS_EXPORTER_ERRORS.inc()
                    logger.exception("Failed to set Prometheus metrics for %s", device_id)
                    return False

                return True
            else:
                # Not exporting per-device metrics (either disabled or cap reached)
                return False

    async def run(self):
        logger.info("Starting poll loop (interval=%ds)", self.poll_interval)
        while self.running:
            try:
                with GENIEACS_POLL_DURATION.time():
                    await self.fetch_and_process()
            except Exception:
                GENIEACS_EXPORTER_ERRORS.inc()
                logger.exception("Unhandled error in poll loop")
            await asyncio.sleep(self.poll_interval)

    async def stop(self):
        self.running = False

# ---------------------------
# Helpers
# ---------------------------
def _safe_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return None

def _compute_delta(prev: int, current: Optional[int]) -> int:
    if current is None:
        return 0
    if prev is None:
        return current
    # handle counter reset (wrap)
    if current >= prev:
        return current - prev
    # reset detected: assume current is new counter since reset
    return current

# ---------------------------
# Entrypoint
# ---------------------------
async def main():
    logger.info("GenieACS Bandwidth Exporter starting")
    conn = aiohttp.TCPConnector(limit=CONCURRENCY * 2)
    async with aiohttp.ClientSession(connector=conn) as session:
        client = GenieACSClient(GENIEACS_API_URL, session, page_size=PAGE_SIZE, concurrency=CONCURRENCY)
        store = CounterStore(PERSIST_DB_PATH)
        await store.init()

        exporter = BandwidthExporter(client, store, poll_interval=POLL_INTERVAL)

        # Start Prometheus HTTP server
        start_http_server(HTTP_PORT)
        logger.info("Prometheus metrics exposed on :%s/metrics", HTTP_PORT)

        # Run exporter
        try:
            await exporter.run()
        finally:
            await store.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Exporter stopped by user")
    except Exception:
        logger.exception("Exporter crashed")
        raise
