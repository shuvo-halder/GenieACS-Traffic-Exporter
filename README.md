### Repository and purpose

This repo contains a production‑oriented Python exporter that polls **GenieACS** (TR‑069) for per‑device counters (`BytesReceived`, `BytesSent`), computes per‑device bandwidth rates, and exposes Prometheus metrics for real‑time and historical visualization in Grafana.

---

### Quick checklist (what you need)

- A running **GenieACS** HTTP API reachable from the exporter.
- A server with **Docker** (or Kubernetes) where you will run the exporter.
- An existing **Prometheus** instance (or the ability to add a scrape target).
- An existing **Grafana** instance (or ability to import the provided dashboard JSON).

---

### Build and run on a Docker server

#### 1. Clone the repo
```bash
git clone https://github.com/shuvo-halder/genieacs-traffic-exporter.git
cd genieacs-traffic-exporter
```

#### 2. Build the Docker image
You can build the image locally. No build‑time environment variables are required for normal operation; configuration is provided at runtime via environment variables.

```bash
docker compose build --no-cache genieacs-exporter
```

If you want to bake defaults into the image (not recommended for secrets), you can use `--build-arg` and modify the `Dockerfile` to accept build args. The recommended approach is runtime env vars (below).

#### 3. Run the exporter container (recommended)
**Important:** set `GENIEACS_API_URL` to the **docker-compose.yml** GenieACS API URL **without** `/devices`.  
Example: `http://10.13.14.18:7557`

```bash
docker compose up -d
```

#### View container logs
```bash
docker compose logs -f genieacs-exporter
```
#### Rebuild after code changes (optional)
```bash
docker compose up -d --build
```
**If port 9410 is already used on the host**, remap host port:
```bash
docker run -d --name genieacs-exporter -p 9510:9410 -e GENIEACS_API_URL=http://10.13.14.18:7557 genieacs-exporter:latest
```
Then Prometheus should scrape `localhost:9510`.

### Prometheus integration

Add a scrape job to your existing `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'genieacs_exporter'
    metrics_path: /metrics
    static_configs:
      - targets: ['<exporter-host>:<port>']   # e.g., ['localhost:9410'] or ['10.0.0.5:9410']
    scrape_interval: 15s
```

- Set `scrape_interval` ≤ `POLL_INTERVAL` to avoid missing samples.
- After editing `prometheus.yml`, reload Prometheus:
  ```bash
  curl -X POST http://<prometheus-host>:9090/-/reload
  ```
  or restart the Prometheus service.

---

### Grafana dashboard import

**UI import**
1. Open Grafana → Dashboards → Import.
2. Upload `grafana/dashboards/genieacs-bandwidth-dashboard.json` from this repo.
3. Select your Prometheus datasource and import.

**Automated provisioning**
If you manage Grafana provisioning, place the JSON in your provisioning dashboards folder and add a provider entry pointing to that path (see earlier `docker-compose` provisioning example).

---

### Metrics endpoint — how to check and expected output

**Check the exporter is running**
```bash
curl http://localhost:9410/metrics | head -n 60
```

**Expected metric names (examples)**

- `genieacs_device_rx_bytes{device_id="...",serial="...",ip="..."}`
- `genieacs_device_tx_bytes{device_id="...",serial="...",ip="..."}`
- `genieacs_device_rx_rate_bps{device_id="...",serial="...",ip="..."}`
- `genieacs_device_tx_rate_bps{device_id="...",serial="...",ip="..."}`
- `genieacs_exporter_up` (1 = running)
- `genieacs_polled_devices_total`

---

### Prometheus queries and Grafana usage (short)

- **Current RX rate for a device**
  ```promql
  genieacs_device_rx_rate_bps{device_id="$device"}
  ```
- **Current TX rate for a device**
  ```promql
  genieacs_device_tx_rate_bps{device_id="$device"}
  ```
- **Bytes transferred over a range (historical)**
  ```promql
  increase(genieacs_device_rx_bytes{device_id="$device"}[$__range])
  increase(genieacs_device_tx_bytes{device_id="$device"}[$__range])
  ```
- **Top N devices by current RX**
  ```promql
  topk($top_n, sum by(device_id) (genieacs_device_rx_rate_bps))
  ```

Use `increase()` on raw counters for historical totals and `avg_over_time()` or `rate()` for smoothing.

---

### Troubleshooting and common pitfalls

- **405 Method Not Allowed** when listing devices  
  Cause: `GENIEACS_API_URL` included `/devices`. Fix: remove `/devices` from the URL.

- **No device metrics appear**  
  - Check exporter logs: `docker logs genieacs-exporter` for `Polled N devices`.  
  - Ensure GenieACS API is reachable from the container: `docker exec -it genieacs-exporter sh` then `curl http://10.131.144.138:7557/devices?limit=1`.  
  - Verify `DEVICE_PARAM_PATHS` matches your vendor parameter names; use wildcards `*` if needed.  
  - Confirm `lastInform` timestamps are recent enough for `ONLINE_THRESHOLD_SECONDS`.

- **High API load / timeouts**  
  - Increase `POLL_INTERVAL` or reduce `CONCURRENCY` and `BATCH_SIZE`.  
  - Consider sharding exporters (each handles a subset of devices) for large fleets.

- **Prometheus target DOWN**  
  - Confirm exporter port is reachable from Prometheus host.  
  - If exporter runs in Docker, either map host port or use Docker network and use container name as target.

---

### Production recommendations (short)

- **Persistence:** enable Redis (`REDIS_URL`) to persist previous counters across restarts and avoid spikes.
- **Sharding:** for >10k devices, run multiple exporter instances and split device lists (hashing or query filters).
- **Long‑term storage:** use Prometheus `remote_write` to VictoriaMetrics/Thanos/Cortex for long retention.
- **Recording rules:** create recording rules for heavy aggregates (topk, 95th percentile) to reduce dashboard load.
- **Monitoring:** export exporter health metrics and alert on `genieacs_exporter_up == 0` or sudden drop in `genieacs_polled_devices_total`.

---

