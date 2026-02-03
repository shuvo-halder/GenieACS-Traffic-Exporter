#!/bin/bash
set -e

APP_NAME="genieacs-exporter"
INSTALL_DIR="/opt/$APP_NAME"
SERVICE_FILE="/etc/systemd/system/$APP_NAME.service"

echo "===> Installing dependencies..."
sudo apt update
sudo apt install -y python3 python3-pip git

echo "===> Installing Python Package..."
pip3 install -r $INSTALL_DIR/requirements.txt
