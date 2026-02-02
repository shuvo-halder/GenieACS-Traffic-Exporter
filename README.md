
# GenieACS Traffic Exporter (Prometheus)

This is a lightweight **Flask-based Prometheus exporter** that collects **RX/TX traffic statistics** from **GenieACS** devices and exposes them in Prometheus format for visualization in **Grafana**.

It supports traffic collection from:

* **PPP interfaces**
* **IP interfaces**
* **WLAN interfaces**

---

## Architecture Overview

```
GenieACS  →  Flask Exporter  →  Prometheus  →  Grafana
```

* **GenieACS**: Source of device statistics (TR-069)
* **Exporter**: Fetches device data and converts it to Prometheus metrics
* **Prometheus**: Scrapes `/metrics`
* **Grafana**: Visualizes traffic rates and totals

---

## Features

* Collects **RX / TX bytes per device**
* Automatically detects available stats:

  * `WANPPPConnection`
  * `WANIPConnection`
  * `WLANConfiguration`
* Safe parsing (won’t crash if fields are missing)
* Prometheus-compatible metrics
* Simple & lightweight (Flask + Requests)

---

## Exported Metrics

### Metrics Name

```
genieacs_rx_bytes
genieacs_tx_bytes
```

### Labels

| Label  | Description                          |
| ------ | ------------------------------------ |
| device | GenieACS device `_id`                |
| iface  | Interface type (`ppp`, `ip`, `wlan`) |

### Example Output

```
genieacs_rx_bytes{device="ONU12345",iface="ppp"} 123456789
genieacs_tx_bytes{device="ONU12345",iface="ppp"} 987654321
```

---

## Requirements

* Python **3.7+**
* GenieACS REST API access
* Prometheus
* Grafana (optional but recommended)

### Python Dependencies

```bash
pip install flask requests
```

---

## Configuration

Edit the exporter file and update GenieACS URL:

```python
GENIEACS_URL = "http://192.168.10.20:7557/devices"
TIMEOUT = 5
```

> ⚠️ Ensure the GenieACS server is reachable from this exporter host.

---

## Running the Exporter

```bash
python3 exporter.py
```

The exporter will start on:

```
http://0.0.0.0:9105/metrics
```

Test it:

```bash
curl http://localhost:9105/metrics
```

---

## Prometheus Configuration

Add this job to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: "genieacs_exporter"
    static_configs:
      - targets:
          - "EXPORTER_IP:9105"
```

Reload Prometheus after updating config.

---

## Grafana Usage

### Recommended PromQL Queries

#### RX Traffic (bytes → bits/sec)

```promql
rate(genieacs_rx_bytes[5m]) * 8
```

#### TX Traffic (bytes → bits/sec)

```promql
rate(genieacs_tx_bytes[5m]) * 8
```

#### Per Device + Interface

```promql
rate(genieacs_rx_bytes{device="ONU12345"}[5m])
```

---

## Common Grafana Tips

* Use **Time series** panel
* Unit: `bps` or `Bps`
* Stack RX & TX if needed
* Apply legend:

  ```
  {{device}} - {{iface}}
  ```

---

## Notes & Limitations

* Only reads **device stats already available in GenieACS**
* If RX/TX is not visible:

  * Device may not report that interface
  * PPP/IP/WLAN stats path may differ by vendor
* This exporter is **read-only** and safe

---

## Future Improvements (Optional)

* Per-interface name instead of `ppp/ip/wlan`
* Device metadata labels (model, serial, OLT)
* Authentication support for GenieACS
* Caching for large device counts

---

## License

MIT License – free to use, modify, and distribute.


* Optimize for **large GenieACS deployments**
* Add **ONU/OLT labels**
* Convert this into a **systemd service**

Just tell me 🔥
