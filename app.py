from flask import Flask, Response
from cache import read_cache

app = Flask(__name__)


@app.route("/metrics")
def metrics():
    cache = read_cache()

    output = []
    output.append(cache["metrics"])

    output += [
        "# HELP genieacs_cache_last_update Cache update timestamp",
        "# TYPE genieacs_cache_last_update gauge",
        f"genieacs_cache_last_update {cache['last_update']}",
        "",
        "# HELP genieacs_cache_success Cache update success",
        "# TYPE genieacs_cache_success gauge",
        f"genieacs_cache_success {cache['success']}",
        "",
        "# HELP genieacs_cached_devices_total Cached device count",
        "# TYPE genieacs_cached_devices_total gauge",
        f"genieacs_cached_devices_total {cache['device_count']}",
        "",
        "# HELP genieacs_cached_devices_online Cached online devices",
        "# TYPE genieacs_cached_devices_online gauge",
        f"genieacs_cached_devices_online {cache['online_count']}",
        "",
        "# HELP genieacs_cached_devices_offline Cached offline devices",
        "# TYPE genieacs_cached_devices_offline gauge",
        f"genieacs_cached_devices_offline {cache['offline_count']}",
    ]

    if cache["device_ids"]:
        output += [
            "",
            "# HELP genieacs_cached_device_info Cached device info",
            "# TYPE genieacs_cached_device_info gauge",
        ]
        for dev_id in cache["device_ids"]:
            output.append(f'genieacs_cached_device_info{{device="{dev_id}"}} 1')

    return Response("\n".join(output), mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9105)