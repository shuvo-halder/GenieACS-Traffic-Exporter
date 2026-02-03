#!/bin/bash
set -e

APP_NAME="genieacs-exporter"
INSTALL_DIR="/opt/$APP_NAME"
SERVICE_FILE="/etc/systemd/system/$APP_NAME.service"
SERVICE_USER="genieacs"

echo "===> Installing dependencies..."
sudo apt update
sudo apt install -y python3 python3-venv git

if id "$SERVICE_USER" &>/dev/null;then
    echo "User $SERVICE_USER already exists, skipping creation."
else
    sudo useradd -r -s /bin/false $SERVICE_USER
fi

echo "===> Coppying files..."
sudo rm -rf $INSTALL_DIR
sudo mkdir -p $INSTALL_DIR
sudo cp -r exporter.py requirements.txt $INSTALL_DIR

echo "===> Creating virtual environment..."
python3 -m venv $INSTALL_DIR/venv
source $INSTALL_DIR/venv/bin/activate
pip install -r $INSTALL_DIR/requirements.txt
deactivate

sudo chown -R $SERVICE_USER:$SERVICE_USER $INSTALL_DIR

echo "===> Installing systemd service..."
sudo tee $SERVICE_FILE > /dev/null <<EOF
[Unit]
Description=GenieACS Prometheus Exporter
After=network.target

[Service]
Environment="GENIEACS_URL=http://127.0.0.1:7557/devices"
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/exporter.py
WorkingDirectory=$INSTALL_DIR
Restart=always
User=$SERVICE_USER
Group=$SERVICE_USER
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable $APP_NAME

echo "==> Creating exporter command..."
sudo tee /usr/bin/exporter > /dev/null <<'EOS'
#!/bin/bash
SERVICE="genieacs-exporter"

case "$1" in
  set-url)
    if [ -z "$2" ]; then
      echo "Usage: exporter set-url <new-url>"
      exit 1
    fi
    sudo sed -i "s|^Environment=.*GENIEACS_URL=.*|Environment=\"GENIEACS_URL=$2\"|" /etc/systemd/system/$SERVICE.service
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
    echo "Usage: exporter {start|stop|status|restart|logs|set-url <new-url>}"
    ;;
esac
EOS

sudo chmod +x /usr/bin/exporter

echo "===--- Installation complete ---==="
echo "Use: exporter start | stop | status | restart | logs | set-url <new-url>