#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IPA_TARGET="${1:-AltStore.ipa}"

if [ ! -f "$IPA_TARGET" ]; then
    echo "[-] File '$IPA_TARGET' not found."
    exit 1
fi

echo "=========================================="
echo "      Sideloader / AltStore Installer"
echo "=========================================="

# Ensure usbmuxd is active
if ! systemctl is-active --quiet usbmuxd 2>/dev/null; then
    sudo systemctl start usbmuxd
fi

# Detect device
UDID=$(idevice_id -l 2>/dev/null | head -n 1)
if [ -n "$UDID" ]; then
    echo "[+] Detected device UDID: $UDID"
    ./sideloader install -i --singlethread --udid "$UDID" "$IPA_TARGET"
else
    echo "[!] No USB device detected automatically. Running interactive install..."
    ./sideloader install -i --singlethread "$IPA_TARGET"
fi
