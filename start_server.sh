#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo "    AltServer Linux - Wi-Fi Server Daemon"
echo "=========================================="

# Ensure netmuxd is running
if systemctl is-active --quiet usbmuxd 2>/dev/null; then
    echo "[!] Stopping system usbmuxd to allow netmuxd to handle Wi-Fi multiplexing..."
    sudo systemctl stop usbmuxd
fi

if ! pgrep -x "netmuxd" >/dev/null; then
    echo "[+] Starting netmuxd daemon in background..."
    sudo ./netmuxd >/dev/null 2>&1 &
    sleep 2
fi

echo "[+] Starting AltServer daemon (announcing via Avahi/mDNS)..."
echo "[+] Your iPhone can now refresh apps over Wi-Fi automatically."
echo "[!] Press Ctrl+C to stop the server."

./AltServer
