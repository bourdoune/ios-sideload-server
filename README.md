<div align="center">

# ⚡ iOS Sideload & Auto-Refresh Server

### Automated, Over-The-Air iOS Sideloading & Background Certificate Renewal

[![Platform](https://img.shields.io/badge/Platform-iOS%2016%20%7C%2017%20%7C%2018-000000?style=for-the-badge&logo=apple&logoColor=white)](https://apple.com)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://telegram.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge)](LICENSE)

<p align="center">
  A self-hosted Linux service for wireless iOS app sideloading and zero-touch provisioning profile renewal over Wi-Fi and VPN (SSTP, WireGuard) without needing a Mac or AltServer on Windows.
</p>

</div>

---

## ✨ Features

- 🔄 **Automated Background Renewal**  
  Refreshes provisioning profiles directly on device via `misagent` over Apple's lockdown protocol. Pushes lightweight certificate updates (~12 KB) in under 2 seconds without re-signing or redownloading entire IPA bundles.

- 🌐 **Dynamic IP & VPN Auto-Detection**  
  Probes Apple lockdown port `62078` across local Wi-Fi subnets and VPN leases (e.g. SSTP `172.16.3.x`) to seamlessly locate and sync your device even across roaming network interfaces.

- 🍏 **Pure-Python Apple Portal Client**  
  Standalone GrandSlam (GSA) and Developer Services engine (`apple_portal/` and `apple_client.py`), eliminating external binary CLI dependencies, memory segfaults, and headless TTY requirements.

- 🔐 **Isolated Session Management**  
  Encrypted token caches reside under `~/.local/share/altserver-apple/` with restricted user permissions (`0700`). Background sync routines pause automatically upon auth errors to prevent account lockouts.

- 🔑 **Interactive 2FA Runner**  
  `login_2fa.py` cleanly handles interactive Apple two-factor challenges, capturing 6-digit codes via `/tmp/2fa_code` without recursive polling loops.

- 📱 **iOS-Native PWA Dashboard**  
  Responsive web dashboard featuring live countdown timers, drag-and-drop IPA installation powered by `zsign`, and standalone home-screen support.

- 🤖 **Telegram Assistant Bot**  
  Asynchronous assistant bot (`tg_bot.py`) providing on-demand `/status` queries, expiration warnings, and inline buttons for one-tap remote renewals.

---

## 🏗️ Architecture

```text
┌─────────────────┐       Wi-Fi / SSTP VPN      ┌───────────────────────────────┐
│                 │ ◄─────────────────────────► │          iOS Device           │
│                 │   (TCP 62078 / netmuxd)     │       (iOS 16+ / Apps)        │
│                 │                             └───────────────────────────────┘
│                 │                                             ▲
│  Linux Server   │                                             │ Profile update (~12 KB)
│  (server_api)   │ ────► misagent over lockdown protocol ──────┘
│                 │
│                 │ ◄───► Apple Developer Portal (apple_portal + Anisette)
│                 │
│                 │ ◄───► Telegram Assistant Bot (tg_bot)
└─────────────────┘
```

- **`server_api.py`**: FastAPI core service managing web endpoints, device discovery, lockdown socket pools, and background sync loops.
- **`apple_client.py`**: Developer Portal renewal interface utilizing cached authentication tokens.
- **`apple_portal/`**: Native Python implementation for Apple GSA authentication and developer portal interaction.
- **`login_2fa.py`**: Dedicated two-factor authorization helper.
- **`tg_bot.py`**: Telegram bot handling status commands and manual renewal triggers.
- **`netmuxd`**: Network usbmuxd multiplexer connecting network-attached iOS devices over mDNS and direct TCP.

---

## 🚀 Getting Started

### 1. Prerequisites

- Linux host (Ubuntu, Debian, Raspberry Pi)
- Python 3.10+
- Local Anisette server running on port `6970` or `6969` (e.g. docker container)
- Paired iOS device (`usbmuxd` / `libimobiledevice`)
- `zsign` binary installed in system PATH or project root

### 2. Installation

```bash
git clone https://github.com/bourdoune/ios-sideload-server.git
cd ios-sideload-server

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configuration

Generate your environment file from the example template:

```bash
cp .env.example .env
chmod 600 .env
```

Edit [.env](file:///home/bourdoune/misc/root/files/altserver/.env) with your configuration:

```ini
# Apple Developer Credentials
DEVICE_UDID=00008000-0000000000000000
APPLE_ID=your_apple_id@example.com
APPLE_PASS=your_apple_password
TEAM_ID=XXXXXXXXXX
ALTSERVER_ANISETTE_SERVER=http://127.0.0.1:6970

# Telegram Bot (Optional)
BOT_TOKEN=1234567890:abcdefghijklmnopqrstuvwxyz
TG_CHAT_ID=123456789
```

---

## 🔑 Apple Session Initialization

When configuring a new deployment, or whenever Apple credentials expire, run the interactive 2FA helper:

```bash
venv/bin/python login_2fa.py
```

The script initiates Apple GrandSlam authentication and waits up to 5 minutes for your 6-digit verification code written to `/tmp/2fa_code`. Once verified, developer session tokens are saved to `~/.local/share/altserver-apple/` and automatic profile renewals are enabled.

---

## 🖥️ Systemd Service Setup

Configure systemd unit files for persistent background operation.

### Sideload Server (`/etc/systemd/system/ios-sideload-server.service`):

```ini
[Unit]
Description=iOS Sideload & Refresh Webhook Server
After=network.target

[Service]
Type=simple
User=bourdoune
WorkingDirectory=/home/bourdoune/misc/root/files/altserver
ExecStart=/home/bourdoune/misc/root/files/altserver/venv/bin/python server_api.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Telegram Bot (`/etc/systemd/system/ios-sideload-tgbot.service`):

```ini
[Unit]
Description=iOS Sideload Telegram Helper Bot
After=network.target ios-sideload-server.service
Wants=ios-sideload-server.service

[Service]
Type=simple
User=bourdoune
WorkingDirectory=/home/bourdoune/misc/root/files/altserver
ExecStart=/home/bourdoune/misc/root/files/altserver/venv/bin/python tg_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Reload systemd and enable both services:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ios-sideload-server ios-sideload-tgbot
```

---

## 🧪 Testing

Execute the test suite with `pytest`:

```bash
PYTHONPATH=. venv/bin/pytest tests/
```

---

## 🔒 Security & Data Protection

- All credentials, private keys, device pair records (`*.plist`), provisioning profiles, and active sessions are strictly ignored by [.gitignore](file:///home/bourdoune/misc/root/files/altserver/.gitignore).
- Apple session tokens are saved under `~/.local/share/altserver-apple` with restricted user access (`0700`).
- Remote connectivity can be routed securely through WireGuard, SSTP, or Tailscale without exposing web ports directly to the public internet.

---

## 📄 License & Acknowledgments

This project is licensed under the MIT License.

The Apple authentication and developer portal implementation (`apple_portal/`) is adapted from [iPASide v1.2.8](https://github.com/pwnapplehat/iPASide/releases/tag/v1.2.8); see [apple_portal/LICENSE](file:///home/bourdoune/misc/root/files/altserver/apple_portal/LICENSE) and [apple_portal/README.md](file:///home/bourdoune/misc/root/files/altserver/apple_portal/README.md).
