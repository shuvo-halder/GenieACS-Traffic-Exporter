#!/bin/bash
set -e
[ "$EUID" -ne 0 ] && echo "Run as root" && exit 1

APP_NAME="genieacs-exporter"
INSTALL_DIR="/opt/$APP_NAME"
WEB_SERVICE="/etc/systemd/system/$APP_NAME.service"
WORKER_SERVICE="/etc/systemd/system/$APP_NAME-worker.service"
SERVICE_USER="genieacs"

echo "===> Installing dependencies..."
apt update
apt install -y python3 python3-venv git

if id "$SERVICE_USER" &>/dev/null; then
    echo "User $SERVICE_USER already exists"
else
    useradd -r -s /bin/false $SERVICE_USER
fi

echo "===> Copying files..."
rm -rf $INSTALL_DIR
mkdir -p $INSTALL_DIR

cp -r app.py worker.py cache.py requirements.txt $INSTALL_DIR

echo "===> Creating virtual environment..."
python3 -m venv $INSTALL_DIR/venv
$INSTALL_DIR/venv/bin/pip install --upgrade pip
$INSTALL_DIR/venv/bin/pip install -r $INSTALL_DIR/requirements.txt

chown -R $SERVICE_USER:$SERVICE_USER $INSTALL_DIR

echo "===> Installing worker service..."
tee $WORKER_SERVICE > /dev/null <<EOF
[Unit]
Description=GenieACS Exporter Background Worker
After=network.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR

Environment=GENIEACS_URL=http://127.0.0.1:7557/devices
Environment=PAGE_LIMIT=1000
Environment=FETCH_INTERVAL=300

ExecStart=$INSTALL_DIR/venv/bin/python worker.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "===> Installing web exporter service..."
tee $WEB_SERVICE > /dev/null <<EOF
[Unit]
Description=GenieACS Prometheus Exporter
After=network.target genieacs-exporter-worker.service

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR

ExecStart=$INSTALL_DIR/venv/bin/gunicorn \\
  --workers 1 \\
  --timeout 60 \\
  --bind 0.0.0.0:9105 \\
  app:app

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

echo "==> Creating genieacs-exporter command..."
tee /usr/bin/genieacs-exporter > /dev/null <<'EOF'
#!/bin/bash
SERVICE="genieacs-exporter"

case "$1" in
  set-url)
    if [ -z "$2" ]; then
      echo "Usage: genieacs-exporter set-url <new-url>"
      exit 1
    fi
    sudo sed -i "s|^Environment=.*GENIEACS_URL=.*|Environment=GENIEACS_URL=$2|" /etc/systemd/system/$SERVICE.service
    sudo systemctl daemon-reload
    sudo systemctl restart $SERVICE
    echo "URL updated to $2"
    ;;
  start|stop|status|restart)
    systemctl $1 $SERVICE
    ;;
  logs)
    journalctl -u $SERVICE -f
    ;;
  *)
    echo "Usage: genieacs-exporter {start|stop|status|restart|logs|set-url <new-url>}"
    ;;
esac
EOF

sudo chmod +x /usr/bin/genieacs-exporter

echo "=== Installation complete ==="
echo "Use: genieacs-exporter start | stop | status | restart | logs | set-url <new-url>"