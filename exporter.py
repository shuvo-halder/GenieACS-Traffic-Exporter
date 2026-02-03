from flask import Flask, Response
import requests

# GENIEACS_URL = "GenieACS ip or server: port"
GENIEACS_URL = "http://192.168.30.40:7557/devices"
TIMEOUT = 5

app = Flask(__name__)

def safe_get(d, path):
    try:
        for p in path:
            d = d[p]
        if isinstance(d, dict) and "_value" in d:
            return d["_value"]
        return d
    except Exception:
        return 0

def get_ppp_stats(d):
    return d.get("InternetGatewayDevice", {}) \
            .get("WANDevice", {}) \
            .get("1", {}) \
            .get("WANConnectionDevice", {}) \
            .get("1", {}) \
            .get("WANPPPConnection", {}) \
            .get("1", {}) \
            .get("Stats", {}) \
            .get("1", {})

def get_ip_stats(d):
    return d.get("InternetGatewayDevice", {}) \
            .get("WANDevice", {}) \
            .get("1", {}) \
            .get("WANConnectionDevice", {}) \
            .get("1", {}) \
            .get("WANIPConnection", {}) \
            .get("1", {}) \
            .get("Stats", {}) \
            .get("1", {})

def get_wlan_stats(d):
    return d.get("InternetGatewayDevice", {}) \
            .get("LANDevice", {}) \
            .get("1", {}) \
            .get("WLANConfiguration", {}) \
            .get("1", {}) \
            .get("Stats", {})

@app.route("/metrics")
def metrics():
    r = requests.get(GENIEACS_URL, timeout=TIMEOUT)
    devices = r.json()

    lines = []
    lines.append("# HELP genieacs_rx_bytes RX bytes")
    lines.append("# TYPE genieacs_rx_bytes counter")
    lines.append("# HELP genieacs_tx_bytes TX bytes")
    lines.append("# TYPE genieacs_tx_bytes counter")

    for d in devices:
        device_id = d.get("_id", "unknown")

        # Try PPP, IP, WLAN stats
        for label, base in [
            ("ppp", get_ppp_stats(d)),
            ("ip", get_ip_stats(d)),
            ("wlan", get_wlan_stats(d))
        ]:
            if base:
                rx = safe_get(base, ["EthernetBytesReceived"]) or safe_get(base, ["TotalBytesReceived"])
                tx = safe_get(base, ["EthernetBytesSent"]) or safe_get(base, ["TotalBytesSent"])

                lines.append(
                    f'genieacs_rx_bytes{{device="{device_id}",iface="{label}",ip="{d.get("IPAddress","unknown")}",name="{d.get("DeviceName","router")}"}} {rx}'
                )
                lines.append(
                    f'genieacs_tx_bytes{{device="{device_id}",iface="{label}",ip="{d.get("IPAddress","unknown")}",name="{d.get("DeviceName","router")}"}} {tx}'
                )

    return Response("\n".join(lines), mimetype="text/plain")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9105)
