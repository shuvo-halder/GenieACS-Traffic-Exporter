from flask import Flask, Response
import time
from cache import read_cache

app = Flask(__name__)

@app.route("/metrics")
def metrics():
    cache = read_cache()

    output = []
    output.append(cache["metrics"])

    output.append("# HELP genieacs_cache_last_update Cache update time")
    output.append("# TYPE genieacs_cache_last_update gauge")
    output.append(f"genieacs_cache_last_update {cache['last_update']}")

    output.append("# HELP genieacs_current_device Current processing device")
    output.append("# TYPE genieacs_device_info gauge")
    for dev_id in cache["device_id"]:
        output.append(f'genieacs_current_devices{{device="{dev_id}"}} 1')

    output.append("# HELP genieacs_cache_success Cache success")
    output.append("# TYPE genieacs_cache_success gauge")
    output.append(f"genieacs_cache_success {cache['success']}")

    return Response("\n".join(output), mimetype="text/plain")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9105)
