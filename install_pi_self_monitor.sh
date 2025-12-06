#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/cloudgy/Pi_Superbox_CSC_321"
REPO_DIR="/opt/Pi_Superbox_CSC_321"
APP_DIR="/opt/pi-self-monitor"
SERVICE_FILE="/etc/systemd/system/pi-self-monitor.service"

echo "=== Pi Self Monitor Installer ==="

# Must be root
if [[ "$EUID" -ne 0 ]]; then
  echo "Please run this script as root (sudo ./install_pi_self_monitor.sh)"
  exit 1
fi

echo "[1/6] Installing dependencies..."
apt-get update -y
apt-get install -y git python3 python3-pip
pip3 install --break-system-packages psutil || pip3 install psutil

echo "[2/6] Cloning or updating repository..."
if [[ -d "$REPO_DIR/.git" ]]; then
  echo "Repo already exists at $REPO_DIR, pulling latest..."
  git -C "$REPO_DIR" pull --ff-only
else
  echo "Cloning into $REPO_DIR..."
  git clone "$REPO_URL" "$REPO_DIR"
fi

echo "[3/6] Creating application directory..."
mkdir -p "$APP_DIR"

echo "[4/6] Copying files from repo..."

# NOTE: This assumes these files live at the root of the repo.
# If they are in a subdirectory, adjust the paths below.
cp "$REPO_DIR/pi_self_monitor.py" "$APP_DIR/pi_self_monitor.py"
cp "$REPO_DIR/pi-self-monitor.service" "$SERVICE_FILE"

chmod +x "$APP_DIR/pi_self_monitor.py"

echo "[5/6] Reloading systemd, enabling and starting service..."
systemctl daemon-reload
systemctl enable pi-self-monitor.service
systemctl restart pi-self-monitor.service

echo "[6/6] Checking service status..."
systemctl --no-pager --full status pi-self-monitor.service || true

echo
echo "=== Installation complete ==="
echo "Dashboard:  http://<your-pi-ip>:8081/"
echo "Health:     http://<your-pi-ip>:8081/health"
echo "Metrics:    http://<your-pi-ip>:8081/metrics"
