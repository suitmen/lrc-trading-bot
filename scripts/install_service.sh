#!/usr/bin/env bash
set -e

# Usage: sudo ./install_service.sh /opt/lrc-bot botuser
INSTALL_DIR=${1:-/opt/lrc-bot}
SERVICE_USER=${2:-botuser}
SERVICE_NAME=lrc-bot

echo "Installing ${SERVICE_NAME} to ${INSTALL_DIR} as ${SERVICE_USER}"

# Create user
if ! id -u ${SERVICE_USER} >/dev/null 2>&1; then
  sudo useradd -r -s /bin/false ${SERVICE_USER}
fi

sudo mkdir -p ${INSTALL_DIR}
sudo chown ${USER}:${USER} ${INSTALL_DIR}

# Copy files
cp -r . ${INSTALL_DIR}

# Create venv
python3 -m venv ${INSTALL_DIR}/venv
source ${INSTALL_DIR}/venv/bin/activate
pip install --upgrade pip
pip install -r ${INSTALL_DIR}/requirements.txt

# Set ownership
sudo chown -R ${SERVICE_USER}:${SERVICE_USER} ${INSTALL_DIR}

# Copy systemd unit
sudo cp config/lrc-bot.service /etc/systemd/system/${SERVICE_NAME}.service
sudo sed -i "s|WorkingDirectory=.*|WorkingDirectory=${INSTALL_DIR}|" /etc/systemd/system/${SERVICE_NAME}.service
sudo sed -i "s|EnvironmentFile=.*|EnvironmentFile=${INSTALL_DIR}/.env|" /etc/systemd/system/${SERVICE_NAME}.service
sudo sed -i "s|ExecStart=.*|ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/main.py|" /etc/systemd/system/${SERVICE_NAME}.service

# Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl start ${SERVICE_NAME}

echo "Service ${SERVICE_NAME} installed and started. Use 'sudo journalctl -u ${SERVICE_NAME} -f' to watch logs."