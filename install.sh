#!/bin/bash
set -euo pipefail

[ "$EUID" -ne 0 ] && echo "Run as root" && exit 1

APP_NAME="genieacs-exporter"
INSTALL_DIR="/opt/$APP_NAME"
WEB_SERVICE="/etc/systemd/system/$APP_NAME.service"
WORKER_SERVICE="/etc/systemd/system/$APP_NAME-worker.service"
SERVICE_USER="genieacs"

echo "===> Installing dependencies..."
apt update || true
apt install -y python3 python3-venv git

if command -v redis-server >/dev/null 2>&1; then
    echo "Redis already installed!"
else
    apt install -y redis-server
    systemctl enable --now redis
fi

if id "$SERVICE_USER" &>/dev/null; then
    echo "User $SERVICE_USER already exists"
else
    useradd -r -s /usr/sbin/nologin $SERVICE_USER
fi

echo "===> Copying files..."
rm -rf "$INSTALL_DIR"
rm -rf "$WEB_SERVICE"
rm -rf "$WORKER_SERVICE"
systemctl daemon-reload
mkdir -p "$INSTALL_DIR"
cp app.py worker.py cache.py requirements.txt "$INSTALL_DIR"

echo "===> Creating virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

chown -R $SERVICE_USER:$SERVICE_USER "$INSTALL_DIR"


# Worker service
echo "===> Installing worker service..."
tee "$WORKER_SERVICE" > /dev/null <<EOF
[Unit]
Description=GenieACS Exporter Background Worker
After=network.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR

Environment=GENIEACS_URL=http://127.0.0.1:7557/devices
Environment=PAGE_LIMIT=5000
Environment=FETCH_INTERVAL=600

LimitNOFILE=65536

StandardOutput=journal
StandardError=journal

ExecStart=$INSTALL_DIR/venv/bin/python worker.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Web exporter service
echo "===> Installing web exporter service..."
tee "$WEB_SERVICE" > /dev/null <<EOF
[Unit]
Description=GenieACS Prometheus Exporter
After=network.target genieacs-exporter-worker.service
Requires=genieacs-exporter-worker.service

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR

ExecStart=$INSTALL_DIR/venv/bin/gunicorn \\
  --workers 1 \\
  --timeout 180 \\
  --bind 0.0.0.0:9105 \\
  app:app

LimitNOFILE=65536
Restart=always
RestartSec=5

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "===> Reloading systemd..."
systemctl daemon-reload
systemctl enable genieacs-exporter-worker
systemctl enable genieacs-exporter


echo "===> Creating genieacs-exporter command..."
tee /usr/bin/genieacs-exporter > /dev/null <<'EOF'
#!/bin/bash
WORKER_SERVICE="genieacs-exporter-worker"
WEB_SERVICE="genieacs-exporter"

case "$1" in
  set-url)
    if [ -z "$2" ]; then
      echo "Usage: genieacs-exporter set-url <GENIEACS_URL>"
      exit 1
    fi
    sed -i "s|^Environment=GENIEACS_URL=.*|Environment=GENIEACS_URL=$2|" \
      /etc/systemd/system/$WORKER_SERVICE.service
    systemctl daemon-reload
    systemctl restart $WORKER_SERVICE
    echo "GENIEACS_URL updated to $2"
    ;;
  start|stop|restart|status)
    systemctl $1 $WORKER_SERVICE
    systemctl $1 $WEB_SERVICE
    ;;
  logs)
    journalctl -u $WORKER_SERVICE -u $WEB_SERVICE -f
    ;;
  *)
    echo "Usage: genieacs-exporter {start|stop|restart|status|logs|set-url <url>}"
    ;;
esac
EOF

chmod +x /usr/bin/genieacs-exporter

echo "======================================="
echo " Installation complete "
echo "======================================="
echo "Start services:"
echo "  genieacs-exporter start"
echo ""
echo "Set GenieACS URL:"
echo "  genieacs-exporter set-url http://IP:7557/devices"
echo ""
echo "View logs:"
echo "  genieacs-exporter logs"
