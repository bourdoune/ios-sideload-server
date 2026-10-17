# ios sideload and auto-refresh server

self-hosted linux service for over-the-air ios app sideloading and automatic provisioning profile renewal across local wi-fi and vpn connections without requiring a mac or altserver on windows.

---

## features

- **automated background renewal**:
  renews provisioning profiles directly on the device using `misagent` over the lockdown protocol. renewals push updated certificates (~12 kb payload) in under 2 seconds without re-signing or re-transferring full ipa packages.

- **dynamic ip detection**:
  automatically detects and tracks the active device ip across local wi-fi subnets and vpn leases (e.g., sstp `172.16.3.x`) by probing apple lockdown port `62078`.

- **native apple portal client**:
  pure-python implementation (`apple_portal/` and `apple_client.py`) for apple grandslam (gsa) authentication and developer portal interaction, replacing unstable third-party binary cli shims.

- **secure session management**:
  developer session tokens are cached under `~/.local/share/altserver-apple` with restricted permissions (`0700`). authentication failures pause background renewal routines safely to avoid account lockouts.

- **interactive 2fa runner**:
  `login_2fa.py` handles two-factor authentication when apple verification is required, reading six-digit codes from `/tmp/2fa_code` without recursive retry loops.

- **web pwa dashboard & telegram bot**:
  responsive web dashboard with real-time expiration countdowns, drag-and-drop ipa signing via `zsign`, and an asynchronous telegram bot (`tg_bot.py`) for remote status queries (`/status`) and manual renewal triggers.

---

## architecture

```text
┌─────────────────┐       wi-fi / sstp vpn      ┌───────────────────────────────┐
│                 │ ◄─────────────────────────► │          ios device           │
│                 │   (tcp 62078 / netmuxd)     │       (ios 16+ / apps)        │
│                 │                             └───────────────────────────────┘
│                 │                                             ▲
│  linux server   │                                             │ profile payload (~12 kb)
│  (server_api)   │ ────► misagent over lockdown protocol ──────┘
│                 │
│                 │ ◄───► apple developer portal (apple_portal + anisette)
│                 │
│                 │ ◄───► telegram assistant bot (tg_bot)
└─────────────────┘
```

- **`server_api.py`**: fastapi daemon providing the web dashboard, rest api, ip detection, lockdown proxy pool, and background watcher loop.
- **`apple_client.py`**: bridge module for downloading fresh provisioning profiles from the apple developer portal using cached session tokens.
- **`apple_portal/`**: pure-python apple gsa authentication and developer session client.
- **`login_2fa.py`**: interactive two-factor authentication helper.
- **`tg_bot.py`**: asynchronous telegram bot polling for status commands, expiration alerts, and manual renewal requests.
- **`netmuxd`**: usbmuxd multiplexer connecting network-attached ios devices over mDNS and direct tcp.

---

## getting started

### 1. prerequisites

- linux host (ubuntu, debian, raspberry pi)
- python 3.10+
- local anisette server (e.g., docker on port `6970` or `6969`)
- paired ios device (`libimobiledevice` / `usbmuxd`)
- `zsign` binary for initial ipa signing

### 2. installation

```bash
git clone https://github.com/bourdoune/ios-sideload-server.git
cd ios-sideload-server

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. configuration

copy the environment template:

```bash
cp .env.example .env
chmod 600 .env
```

configure [.env](file:///home/bourdoune/misc/root/files/altserver/.env) with your credentials:

```ini
# apple developer credentials
DEVICE_UDID=00008000-0000000000000000
APPLE_ID=your_apple_id@example.com
APPLE_PASS=your_apple_password
TEAM_ID=XXXXXXXXXX
ALTSERVER_ANISETTE_SERVER=http://127.0.0.1:6970

# telegram bot (optional)
BOT_TOKEN=1234567890:abcdefghijklmnopqrstuvwxyz
TG_CHAT_ID=123456789
```

---

## apple authentication and session initialization

when first configuring the server, or whenever session credentials expire, run the interactive 2fa runner:

```bash
venv/bin/python login_2fa.py
```

the helper initiates grandslam authentication and waits up to 5 minutes for a 6-digit code in `/tmp/2fa_code`. upon verification, developer session tokens are written to `~/.local/share/altserver-apple/session/` and automatic profile renewals are enabled.

---

## systemd services

create systemd unit files for background operation.

### sideload server (`/etc/systemd/system/ios-sideload-server.service`):

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

### telegram bot (`/etc/systemd/system/ios-sideload-tgbot.service`):

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

enable and start services:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ios-sideload-server ios-sideload-tgbot
```

---

## tests

run the test suite using `pytest`:

```bash
PYTHONPATH=. venv/bin/pytest tests/
```

---

## security and data protection

- all secrets, private keys, device pair records (`*.plist`), provisioning profiles, and sessions are excluded via [.gitignore](file:///home/bourdoune/misc/root/files/altserver/.gitignore).
- apple session files are isolated under `~/.local/share/altserver-apple` and restricted to user-only read/write (`0700`).
- remote access can be routed through wireguard, sstp, or tailscale vpn interfaces without exposing internal endpoints to the public internet.

---

## license

licensed under the mit license.

the apple portal authentication implementation (`apple_portal/`) is adapted from [iPASide v1.2.8](https://github.com/pwnapplehat/iPASide/releases/tag/v1.2.8); see [apple_portal/LICENSE](file:///home/bourdoune/misc/root/files/altserver/apple_portal/LICENSE) and [apple_portal/README.md](file:///home/bourdoune/misc/root/files/altserver/apple_portal/README.md).
