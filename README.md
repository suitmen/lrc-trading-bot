# LRC Trading Bot

This repository contains a trading bot (`main.py`) designed to run continuously and trade on Bybit.

## ⚙️ Run as a service (recommended)
You have two recommended ways to run the bot as a server service:

1) Systemd (Linux) — install with the provided script
2) Docker — run in a container (cross-platform)

---

## 🔧 Systemd install (Linux)
Notes:
- The bot expects a `.env` file placed in the project root with your `BYBIT_API_KEY` and `BYBIT_API_SECRET`.
- TA-Lib requires system-level libraries (`libta-lib-dev`), see Dockerfile for an example.

Steps:

1. Copy repository to server, e.g. `/opt/lrc-bot`.
2. Create `.env` from `.env.example` and fill your keys.
3. Run the installer (as root or with sudo):

   sudo ./scripts/install_service.sh /opt/lrc-bot botuser

4. Check logs:

   sudo systemctl status lrc-bot
   sudo journalctl -u lrc-bot -f

---

## 🐳 Docker (cross-platform)
Build and run:

    docker build -t lrc-bot:latest .
    docker run --restart unless-stopped --env-file .env lrc-bot:latest

Or with docker-compose:

    docker compose up -d --build

Notes: Dockerfile installs `libta-lib-dev` before pip installing dependencies.

---

## 🪟 Windows
If you need to run as a Windows service, consider using NSSM or sc.exe to register the Python interpreter that runs `main.py`, or use Docker on Windows.

---

## 📌 Example .env
See `.env.example` in the repo.

---

If you want, I can also add a small health-check endpoint, Docker healthcheck, or optional systemd watchdog support — tell me which you prefer.