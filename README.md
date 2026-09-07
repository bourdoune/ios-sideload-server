<div align="center">

# ⚡ iOS Sideload & Auto-Refresh Server

### Seamless, Over-The-Air iOS App Sideloading & Automatic Wi-Fi Certificate Renewal

[![Platform](https://img.shields.io/badge/platform-iOS%2016%20%7C%2017%20%7C%2018-black.svg?style=for-the-badge&logo=apple)](https://apple.com)
[![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi)](https://fastapi.tiangolo.com)
[![Telegram Bot](https://img.shields.io/badge/Telegram-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://telegram.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge)](LICENSE)

<p align="center">
  A self-hosted, all-in-one Linux service for sideloading iOS apps wirelessly without a Mac or AltServer on Windows. Featuring a sleek iOS-style PWA dashboard, background Wi-Fi profile auto-renewal (even while locked), and a real-time Telegram assistant bot.
</p>

</div>

---

## ✨ Key Highlights

- 🔄 **Automatic Background Wi-Fi Renewal**
  No more 7-day expiration worries. The server automatically detects your iPhone when it joins your home Wi-Fi network and refreshes its provisioning profiles—**even when the device is locked in sleep mode**.

- ⚡ **Lightweight Payload (< 15 KB)**
  Instead of re-signing and downloading massive multi-hundred megabyte `.ipa` files over Wi-Fi, renewals push updated Apple certificates directly to the device through `misagent` in under 2 seconds.

- 📲 **Wireless IPA Sideloading**
  Drag & drop or upload `.ipa` files directly from your browser or iPhone to have them signed and installed over the air.

- 📱 **Modern iOS PWA Dashboard**
  A responsive web interface with live countdown timers, app battery/resource-friendly caching, one-tap manual refresh buttons, and standalone installation support (Add to Home Screen).

- 🤖 **Telegram Assistant Bot**
  Instant alerts before certificates expire, live status check command (`/status`), and inline buttons to trigger remote renewals from anywhere.

---

## 🏗️ Architecture

```
┌─────────────────┐       Wi-Fi / LAN       ┌───────────────────────────────┐
│                 │ ◄─────────────────────► │          iOS Device           │
│                 │      (netmuxd / mDNS)   │       (iOS 16+ / Apps)        │
│                 │                         └───────────────────────────────┘
│                 │                                  ▲
│  Linux Server   │                                  │ Profile install (~12KB)
│  (FastAPI API)  │ ────► misagent & Lockdown proxy ─┘
│                 │
│                 │ ◄───► Apple Developer Portal (via Sideloader & Anisette)
│                 │
│                 │ ◄───► Telegram Assistant Bot
└─────────────────┘
```

- **`server_api.py`**: High-performance FastAPI server providing the REST API, PWA interface, lockdown connection pool, and `zsign` signing pipeline.
- **`tg_bot.py`**: Telegram bot built with asynchronous HTTP polling for status queries, renewal triggers, and expiration warnings.
- **`netmuxd`**: Network multiplexer handling usbmuxd communication with iOS devices over Bonjour/mDNS (`_apple-mobdev2._tcp.local`).
- **`sideloader`**: Apple Developer Portal client for managing Developer certificates, registering device UDIDs, and downloading renewed provisioning profiles.

---

## 🚀 Getting Started

### 1. Prerequisites
- **Linux Machine** (Ubuntu / Debian / Raspberry Pi)
- **Python 3.10+**
- **Anisette v3 service** running locally (e.g., Docker container on port `6970` or `6969`)
- **iOS Device** paired with the host machine (`libimobiledevice` / `usbmuxd`)

### 2. Clone & Setup

```bash
git clone https://github.com/bourdoune/ios-sideload-server.git
cd ios-sideload-server

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configuration

Create your environment configuration from the template:

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your parameters:

```ini
# Apple Developer Credentials
DEVICE_UDID=00008000-0000000000000000
APPLE_ID=your_apple_id@example.com
APPLE_PASS=your_apple_account_password
TEAM_ID=XXXXXXXXXX
ALTSERVER_ANISETTE_SERVER=http://127.0.0.1:6970

# Telegram Bot (Optional but recommended)
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
TG_CHAT_ID=123456789
```

---

## 🖥️ Running as System Services

Create systemd services for continuous background execution.

#### Sideload Server (`/etc/systemd/system/ios-sideload-server.service`):
```ini
[Unit]
Description=iOS Sideload & Refresh Webhook Server
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/ios-sideload-server
ExecStart=/path/to/ios-sideload-server/venv/bin/python server_api.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

#### Telegram Bot (`/etc/systemd/system/ios-sideload-tgbot.service`):
```ini
[Unit]
Description=iOS Sideload Telegram Helper Bot
After=network.target ios-sideload-server.service
Wants=ios-sideload-server.service

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/ios-sideload-server
ExecStart=/path/to/ios-sideload-server/venv/bin/python tg_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start both services:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ios-sideload-server ios-sideload-tgbot
```

---

## ⏰ Automated 12-Hour Renewal (Cron)

To ensure your apps never expire even if you never open the dashboard, add an automated background renewal job:

```bash
crontab -e
```

Add the following rule:
```cron
0 */12 * * * curl -s -X POST http://127.0.0.1:8899/api/refresh > /dev/null 2>&1
```

Whenever your device is connected to your home network, the certificates will renew silently.

---

## 🔒 Security & Privacy

- **No Stored Credentials in Git**: All sensitive credentials, certificates, provisioning profiles, and pair records are ignored by [`.gitignore`](.gitignore).
- **Local Network Only**: By default, the service operates entirely within your local LAN. Remote access can be achieved securely through an encrypted VPN (WireGuard / SSTP / Tailscale).

---

## 📄 License

This project is licensed under the MIT License.
