#!/bin/bash
set -e

APP_NAME="genieacs-exporter"
INSTALL_DIR="/opt/$APP_NAME"
SERVICE_FILE="/etc/systemd/system/$APP_NAME.service"

echo "===> Installing dependencies..."
sudo apt update
sudo apt install -y python3 python3-pip git

echo "===> Coppying files..."
sudo rm -rf $INSTALL_DIR
sudo mkdir -p $INSTALL_DIR
sudo cp -r exporter.py requirements.txt $INSTALL_DIR

echo "===> Installing Python Package..."
pip3 install -r $INSTALL_DIR/requirements.txt

echo "===> Installing systemd service..."
sudo cp -r exporter.service $SERVICE_FILE
sudo systemctl daemon-reload
sudo systemctl enable $APP_NAME

echo "===> Creating exporter command..."
echo "#!/bin/bash
systemctl \$1 $APP_NAME" | sudo tee /usr/bin/exporter > /dev/null
sudo chmod +x /usr/bin/exporter

echo "=== Installation complete..."
echo "Use: exporter start | stop | status | logs"