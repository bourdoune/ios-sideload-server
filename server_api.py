import os
import shutil
import asyncio
import subprocess
import tempfile
import zipfile
import plistlib
import socket
import struct
import time
import gc
from datetime import datetime, timezone
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional, List, Dict, Any

from pymobiledevice3.lockdown import create_using_usbmux, create_using_tcp
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.misagent import MisagentService

app = FastAPI(title="SideStore Remote Control")

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DEVICE_UDID = os.getenv("DEVICE_UDID", "")
APPLE_ID = os.getenv("APPLE_ID", "")
APPLE_PASS = os.getenv("APPLE_PASS", "")
TEAM_ID = os.getenv("TEAM_ID", "")
STATIC_DIR = os.path.join(BASE_DIR, "static")
PROFILES_DIR = os.path.join(BASE_DIR, "profiles")
SIDELOADER_BIN = os.path.join(BASE_DIR, "sideloader")
ZSIGN_BIN = os.path.join(BASE_DIR, "zsign")
KEY_PEM = os.getenv("KEY_PEM", os.path.expanduser("~/.config/Sideloader/keys/key.pem"))
CERT_PEM = os.path.join(BASE_DIR, "cert.pem")
DEFAULT_APP_IPA = os.path.join(BASE_DIR, "app.ipa")

os.makedirs(PROFILES_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
ICONS_DIR = os.path.join(STATIC_DIR, "icons")
os.makedirs(ICONS_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

DEVICE_IP_FILE = os.path.join(BASE_DIR, ".device_ip")

def is_local_or_docker(ip: Optional[str]) -> bool:
    if not ip:
        return True
    return (
        ip.startswith("127.") or 
        ip.startswith("172.17.") or 
        ip.startswith("172.18.") or 
        ip == "::1" or 
        ip == "localhost"
    )

def load_last_known_ip() -> str:
    if os.path.exists(DEVICE_IP_FILE):
        try:
            with open(DEVICE_IP_FILE, "r") as f:
                ip = f.read().strip()
                if ip and not is_local_or_docker(ip):
                    return ip
        except Exception:
            pass
    return os.getenv("DEVICE_IP", "127.0.0.1")

def save_last_known_ip(ip: str):
    if not ip or is_local_or_docker(ip):
        return
    try:
        with open(DEVICE_IP_FILE, "w") as f:
            f.write(ip.strip())
    except Exception:
        pass

LAST_KNOWN_IP = load_last_known_ip()
CURRENT_REGISTERED_NETMUXD_IP = None
CACHED_APPS_LIST = []
CACHED_APPS_TIMESTAMP = 0.0
CACHED_DEVICE_PROPS = {}

def extract_and_save_app_icon(ipa_path: str, bundle_id: str) -> Optional[str]:
    """Extract app icon from IPA and save to static/icons/<bundle_id>.png."""
    icon_filename = f"{bundle_id}.png"
    icon_dest = os.path.join(ICONS_DIR, icon_filename)
    if os.path.exists(icon_dest) and os.path.getsize(icon_dest) > 0:
        return f"/static/icons/{icon_filename}"

    try:
        with zipfile.ZipFile(ipa_path, "r") as z:
            plist_path = None
            for n in z.namelist():
                if n.startswith("Payload/") and n.endswith(".app/Info.plist") and n.count("/") == 2:
                    plist_path = n
                    break
            if not plist_path:
                return None
            app_prefix = plist_path[:-len("Info.plist")]
            p = plistlib.loads(z.read(plist_path))

            icon_names = []
            icons_dict = p.get("CFBundleIcons", {})
            if isinstance(icons_dict, dict):
                pri = icons_dict.get("CFBundlePrimaryIcon", {})
                if isinstance(pri, dict):
                    icon_names.extend(pri.get("CFBundleIconFiles", []))
            if "CFBundleIconFiles" in p:
                icon_names.extend(p["CFBundleIconFiles"])
            if "CFBundleIconFile" in p:
                icon_names.append(p["CFBundleIconFile"])

            found_icon_member = None
            for base in reversed(icon_names):
                clean = base.replace(".png", "")
                for candidate in [f"{app_prefix}{clean}@3x.png", f"{app_prefix}{clean}@2x.png", f"{app_prefix}{clean}.png"]:
                    if candidate in z.namelist():
                        found_icon_member = candidate
                        break
                if found_icon_member:
                    break

            if not found_icon_member:
                for n in z.namelist():
                    if n.startswith(app_prefix) and "icon" in n.lower() and n.endswith(".png"):
                        found_icon_member = n
                        break

            if found_icon_member:
                data = z.read(found_icon_member)
                with open(icon_dest, "wb") as f:
                    f.write(data)
                return f"/static/icons/{icon_filename}"
    except Exception as e:
        print(f"Error extracting icon for {bundle_id}: {e}")
    return None
APPS_CACHE_TTL = 30.0  # 30 seconds cache for snappy updates

def is_device_alive(ip: str) -> bool:
    """Check if device is reachable via TCP 62078 (Apple lockdown service) or ICMP ping."""
    if not ip or is_local_or_docker(ip):
        return False
    # 1. Direct TCP probe to Apple lockdown port 62078 (works across Wi-Fi and VPN)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.8)
        ret = s.connect_ex((ip, 62078))
        s.close()
        if ret == 0:
            return True
    except Exception:
        pass
    # 2. Fallback to ICMP ping (works on home Wi-Fi)
    try:
        res = subprocess.run(["ping", "-c", "1", "-W", "1", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False

def is_device_in_netmuxd() -> bool:
    """Check if our device is already registered as an attached Network device."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect("/var/run/usbmuxd")
        req = {"ClientVersionString": "check", "MessageType": "ListDevices", "ProgName": "check"}
        data = plistlib.dumps(req)
        hdr = struct.pack("<IIII", len(data) + 16, 1, 8, 1)
        s.sendall(hdr + data)
        resp_hdr = s.recv(16)
        if len(resp_hdr) == 16:
            length, _, _, _ = struct.unpack("<IIII", resp_hdr)
            resp_data = s.recv(length - 16)
            s.close()
            devices = plistlib.loads(resp_data).get("DeviceList", [])
            for d in devices:
                props = d.get("Properties", {})
                if props.get("SerialNumber") == DEVICE_UDID and props.get("ConnectionType") == "Network":
                    return True
        s.close()
    except Exception:
        pass
    return False

def register_device_to_netmuxd(ip: str):
    """Tell netmuxd to attach the device by its IP directly, updating if IP changes."""
    global CURRENT_REGISTERED_NETMUXD_IP
    if not ip or is_local_or_docker(ip):
        return
    if CURRENT_REGISTERED_NETMUXD_IP == ip and is_device_in_netmuxd():
        return
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect("/var/run/usbmuxd")
        req = {
            "MessageType": "AddDevice",
            "ConnectionType": "Network",
            "ServiceName": "_apple-mobdev2._tcp.local",
            "IPAddress": ip,
            "DeviceID": DEVICE_UDID
        }
        data = plistlib.dumps(req)
        hdr = struct.pack("<IIII", len(data) + 16, 1, 8, 69)
        s.sendall(hdr + data)
        s.close()
        CURRENT_REGISTERED_NETMUXD_IP = ip
    except Exception as e:
        print(f"Error registering device to netmuxd: {e}")

async def get_lockdown_client():
    """Connect to iPhone directly over TCP or via netmuxd with auto-retry."""
    # 1. Try direct TCP connection first if IP is known (works great on SSTP VPN and LAN)
    if LAST_KNOWN_IP and not is_local_or_docker(LAST_KNOWN_IP):
        try:
            return await asyncio.wait_for(
                create_using_tcp(hostname=LAST_KNOWN_IP, identifier=DEVICE_UDID or None),
                timeout=3.5
            )
        except Exception:
            pass

    # 2. Try netmuxd
    if LAST_KNOWN_IP and not is_local_or_docker(LAST_KNOWN_IP):
        register_device_to_netmuxd(LAST_KNOWN_IP)
        await asyncio.sleep(0.2)

    try:
        return await asyncio.wait_for(
            create_using_usbmux(serial=DEVICE_UDID, connection_type="Network"),
            timeout=3.5
        )
    except Exception:
        # Re-register and retry once
        if LAST_KNOWN_IP and not is_local_or_docker(LAST_KNOWN_IP):
            register_device_to_netmuxd(LAST_KNOWN_IP)
            await asyncio.sleep(0.4)
        try:
            return await asyncio.wait_for(
                create_using_usbmux(serial=DEVICE_UDID, connection_type="Network"),
                timeout=3.5
            )
        except Exception:
            return None

def extract_ipa_metadata(ipa_path: str):
    try:
        with zipfile.ZipFile(ipa_path, "r") as z:
            for name in z.namelist():
                if name.startswith("Payload/") and name.endswith(".app/Info.plist") and name.count("/") == 2:
                    data = z.read(name)
                    p = plistlib.loads(data)
                    bundle_id = p.get("CFBundleIdentifier", "com.sideload.app")
                    name = p.get("CFBundleDisplayName") or p.get("CFBundleName") or "App"
                    version = p.get("CFBundleShortVersionString") or p.get("CFBundleVersion") or "1.0"
                    return str(bundle_id), str(name), str(version)
    except Exception as e:
        print("Metadata extraction error:", e)
    return "com.sideload.app", "App", "1.0"

def extract_profile_expiration(prov_path: str) -> Optional[datetime]:
    try:
        if not os.path.exists(prov_path):
            return None
        with open(prov_path, "rb") as fp:
            data = fp.read()
        start = data.find(b"<?xml")
        end = data.find(b"</plist>")
        if start != -1 and end != -1:
            p = plistlib.loads(data[start:end + 8])
            exp = p.get("ExpirationDate")
            if exp:
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                return exp
    except Exception as e:
        print(f"Error reading expiration date from {prov_path}: {e}")
    return None

def fetch_fresh_provisioning_profile_from_apple(bundle_id: str, app_name: str = "App") -> tuple[Optional[str], bool, str]:
    """
    Downloads provisioning profile from Apple Developer portal.
    Returns: (prov_path, is_extended, message)
    - If Apple issued a new extended profile: returns (prov_path, True, "Renewed until <date>")
    - If profile exists and is still active: returns (prov_path, False, "Profile is already active until <date>")
    - If download failed: returns (None, False, "Error message")
    """
    if not bundle_id.endswith(f".{TEAM_ID}"):
        registered_id = f"{bundle_id}.{TEAM_ID}"
    else:
        registered_id = bundle_id

    prov_path = os.path.join(PROFILES_DIR, f"{registered_id}.mobileprovision")
    tmp_dl_path = os.path.join(PROFILES_DIR, f"{registered_id}.download.tmp")
    if os.path.exists(tmp_dl_path):
        try:
            os.remove(tmp_dl_path)
        except Exception:
            pass

    existing_exp = extract_profile_expiration(prov_path)

    env = os.environ.copy()
    env["HOME"] = os.path.expanduser("~")
    env["ALTSERVER_ANISETTE_SERVER"] = os.getenv("ALTSERVER_ANISETTE_SERVER", "http://127.0.0.1:6970")
    env["APPLE_ID"] = APPLE_ID
    env["APPLE_PASSWORD"] = APPLE_PASS

    cmd_dl = [SIDELOADER_BIN, "app-id", "download", "-i", "--team", TEAM_ID, "-o", tmp_dl_path, registered_id]
    res_dl = subprocess.run(cmd_dl, cwd=BASE_DIR, env=env, capture_output=True, text=True)

    if res_dl.returncode != 0 or not os.path.exists(tmp_dl_path):
        cmd_add = [SIDELOADER_BIN, "app-id", "add", "-i", "--team", TEAM_ID, app_name, registered_id]
        subprocess.run(cmd_add, cwd=BASE_DIR, env=env, capture_output=True, text=True)
        res_dl = subprocess.run(cmd_dl, cwd=BASE_DIR, env=env, capture_output=True, text=True)

    if res_dl.returncode == 0 and os.path.exists(tmp_dl_path) and os.path.getsize(tmp_dl_path) > 0:
        new_exp = extract_profile_expiration(tmp_dl_path)
        if new_exp:
            if not existing_exp or new_exp > existing_exp:
                shutil.move(tmp_dl_path, prov_path)
                exp_str = new_exp.strftime("%b %d, %H:%M")
                return prov_path, True, f"Renewed until {exp_str} UTC (7 days renewed)!"
            else:
                try:
                    os.remove(tmp_dl_path)
                except Exception:
                    pass
                now = datetime.now(timezone.utc)
                delta = existing_exp - now
                days = max(0, delta.days)
                hours = max(0, delta.seconds // 3600)
                exp_str = existing_exp.strftime("%b %d, %H:%M")
                return prov_path, False, f"Profile is already active until {exp_str} UTC ({days}d {hours}h left). Apple renews closer to expiration."

    if os.path.exists(tmp_dl_path):
        try:
            os.remove(tmp_dl_path)
        except Exception:
            pass

    out_err = (res_dl.stdout or "") + (res_dl.stderr or "")
    if "503" in out_err or res_dl.returncode == -11:
        err_reason = "Apple Developer service temporarily busy (HTTP 503 / rate limit)."
    else:
        err_reason = "Could not download fresh profile from Apple."

    if existing_exp and os.path.exists(prov_path):
        now = datetime.now(timezone.utc)
        delta = existing_exp - now
        days = max(0, delta.days)
        hours = max(0, delta.seconds // 3600)
        exp_str = existing_exp.strftime("%b %d, %H:%M")
        return prov_path, False, f"{err_reason} Existing profile is valid until {exp_str} UTC ({days}d {hours}h left)."

    return None, False, f"Could not obtain profile for {registered_id}: {err_reason}"

async def push_certificate_profile_only(bundle_id: str) -> tuple[bool, str, bool]:
    clean_name = bundle_id.split(".")[-2] if "." in bundle_id else "App"
    prov_path, extended, message = fetch_fresh_provisioning_profile_from_apple(bundle_id, clean_name)
    if not prov_path or not os.path.exists(prov_path):
        return False, f"Could not obtain profile for {bundle_id}: {message}", False

    if not extended:
        ld = await get_lockdown_client()
        if not ld:
            return False, f"{message} (iPhone is offline over Wi-Fi/VPN)", False
        return True, message, False

    try:
        ld = await get_lockdown_client()
        if not ld:
            return False, "Device unreachable over Wi-Fi / VPN", False
        async with MisagentService(ld) as mis:
            with open(prov_path, "rb") as f:
                res = await mis.install(f)
                if res.get("Status") == 0:
                    return True, message, True
                else:
                    return False, f"misagent error: {res}", False
    except Exception as e:
        return False, str(e), False

async def wireless_sign_and_install(ipa_path: str, custom_bundle_id: Optional[str] = None):
    orig_bundle_id, app_name, version = extract_ipa_metadata(ipa_path)
    target_bundle_id = custom_bundle_id or (f"{orig_bundle_id}.{TEAM_ID}" if not orig_bundle_id.endswith(f".{TEAM_ID}") else orig_bundle_id)

    prov_path, extended, message = fetch_fresh_provisioning_profile_from_apple(orig_bundle_id, app_name)
    if not prov_path or not os.path.exists(prov_path):
        return False, f"Could not obtain Apple provisioning profile for {target_bundle_id}: {message}"

    with tempfile.NamedTemporaryFile(suffix=".ipa", delete=False) as tmp_signed:
        signed_ipa = tmp_signed.name

    try:
        cmd = [
            ZSIGN_BIN,
            "-k", KEY_PEM,
            "-c", CERT_PEM,
            "-m", prov_path,
            "-b", target_bundle_id,
            "-o", signed_ipa,
            ipa_path
        ]

        res = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
        if res.returncode != 0:
            return False, f"Signing error:\n{res.stdout}\n{res.stderr}"

        ld = await get_lockdown_client()
        if not ld:
            return False, "Device unreachable over Wi-Fi"
        async with InstallationProxyService(ld) as ips:
            await ips.install_from_local(signed_ipa)

        # Extract app icon to static/icons
        extract_and_save_app_icon(ipa_path, target_bundle_id)

        return True, f"{app_name} installed successfully!"
    except Exception as e:
        return False, str(e)
    finally:
        if os.path.exists(signed_ipa):
            os.remove(signed_ipa)

@app.get("/manifest.json")
def get_manifest():
    return FileResponse(os.path.join(STATIC_DIR, "manifest.json"), media_type="application/manifest+json")

@app.get("/sw.js")
def get_sw():
    return FileResponse(os.path.join(STATIC_DIR, "sw.js"), media_type="application/javascript")

@app.get("/api/status")
async def get_status(request: Request):
    global LAST_KNOWN_IP, CACHED_DEVICE_PROPS
    client_ip = request.client.host if request.client else None
    query_ip = request.query_params.get("ip")
    if query_ip and not is_local_or_docker(query_ip):
        LAST_KNOWN_IP = query_ip.strip()
        save_last_known_ip(LAST_KNOWN_IP)
    elif client_ip and not is_local_or_docker(client_ip):
        LAST_KNOWN_IP = client_ip.strip()
        save_last_known_ip(LAST_KNOWN_IP)

    alive = is_device_alive(LAST_KNOWN_IP)
    
    conn_type = "Disconnected"
    if alive:
        if LAST_KNOWN_IP.startswith("192.168."):
            conn_type = "Wi-Fi LAN"
        elif LAST_KNOWN_IP.startswith("172.16.") or LAST_KNOWN_IP.startswith("10.") or LAST_KNOWN_IP.startswith("100."):
            conn_type = "SSTP / VPN"
        else:
            conn_type = "Remote IP"
    
    device_info = {
        "device_name": CACHED_DEVICE_PROPS.get("DeviceName", os.getenv("DEVICE_NAME", "iPhone")),
        "model": CACHED_DEVICE_PROPS.get("ProductType", os.getenv("DEVICE_MODEL", "iOS Device")),
        "ios_version": CACHED_DEVICE_PROPS.get("ProductVersion", os.getenv("DEVICE_IOS_VERSION", "iOS")),
        "device_udid": DEVICE_UDID,
        "ip": LAST_KNOWN_IP,
        "online": alive,
        "connection_type": conn_type,
        "app_ids_used": len(CACHED_APPS_LIST) if CACHED_APPS_LIST else 0,
        "app_ids_max": 10
    }

    # Only fetch full device info if not already cached
    if alive and not CACHED_DEVICE_PROPS:
        try:
            ld = await get_lockdown_client()
            if ld:
                vals = ld.all_values
                CACHED_DEVICE_PROPS["DeviceName"] = vals.get("DeviceName", "iPhone")
                CACHED_DEVICE_PROPS["ProductVersion"] = vals.get("ProductVersion", "iOS")
                device_info["device_name"] = CACHED_DEVICE_PROPS["DeviceName"]
                device_info["ios_version"] = CACHED_DEVICE_PROPS["ProductVersion"]
        except Exception:
            pass

    return device_info

@app.get("/api/apps")
async def get_apps(force: bool = False):
    global CACHED_APPS_LIST, CACHED_APPS_TIMESTAMP
    now_ts = time.time()
    
    # Return fast cached response if within TTL and not explicitly forced
    if not force and CACHED_APPS_LIST and (now_ts - CACHED_APPS_TIMESTAMP < APPS_CACHE_TTL):
        return CACHED_APPS_LIST

    sideloaded_apps = []
    
    try:
        ld = await get_lockdown_client()
        if not ld:
            return CACHED_APPS_LIST or fallback_apps()

        profile_expirations = {}
        async with MisagentService(ld) as mis:
            profiles = await mis.copy_all()
            for p in profiles:
                pl = p.plist
                appid = pl.get("Entitlements", {}).get("application-identifier", "")
                exp = pl.get("ExpirationDate")
                if exp and TEAM_ID in appid:
                    clean_id = appid.replace(f"{TEAM_ID}.", "")
                    if clean_id not in profile_expirations or exp > profile_expirations[clean_id]:
                        profile_expirations[clean_id] = exp
                    profile_expirations[appid] = exp

        async with InstallationProxyService(ld) as ips:
            user_apps = await ips.get_apps(application_type="User")
            now = datetime.now(timezone.utc)

            for bid, info in user_apps.items():
                is_sideloaded = False
                if (TEAM_ID and TEAM_ID in bid) or (bid in profile_expirations):
                    is_sideloaded = True
                elif info.get("ProfileValidated") or "Developer" in str(info.get("SignerIdentity", "")):
                    is_sideloaded = True

                if is_sideloaded:
                    app_name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or bid
                    version = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "1.0"
                    
                    exp_date = profile_expirations.get(bid)
                    if not exp_date and TEAM_ID:
                        exp_date = profile_expirations.get(f"{bid}.{TEAM_ID}") or profile_expirations.get(f"{TEAM_ID}.{bid}")
                    
                    if exp_date:
                        if exp_date.tzinfo is None:
                            exp_date = exp_date.replace(tzinfo=timezone.utc)
                        delta = exp_date - now
                        total_hours = max(0, int(delta.total_seconds() // 3600))
                        days = total_hours // 24
                        hours = total_hours % 24
                        
                        if days > 0:
                            time_left_str = f"{days}d {hours}h left"
                        else:
                            time_left_str = f"{hours}h left"
                            
                        pct = max(0, min(100, int((delta.total_seconds() / (7 * 86400)) * 100)))
                        exp_str = exp_date.strftime("%b %d, %H:%M")
                    else:
                        days = 7
                        total_hours = 168
                        time_left_str = "7 days left"
                        pct = 100
                        exp_str = "Valid (7 Days)"

                    icon_url = None
                    icon_file = os.path.join(ICONS_DIR, f"{bid}.png")
                    if os.path.exists(icon_file) and os.path.getsize(icon_file) > 0:
                        icon_url = f"/static/icons/{bid}.png"
                    else:
                        local_ipa = os.path.join(BASE_DIR, "installed_apps", f"{bid}.ipa")
                        if os.path.exists(local_ipa):
                            icon_url = extract_and_save_app_icon(local_ipa, bid)

                    sideloaded_apps.append({
                        "bundle_id": bid,
                        "name": app_name,
                        "version": version,
                        "icon_url": icon_url,
                        "days_left": days,
                        "hours_left": total_hours,
                        "time_left_str": time_left_str,
                        "percent_remaining": pct,
                        "expires_at": exp_str
                    })

        if sideloaded_apps:
            CACHED_APPS_LIST = sideloaded_apps
            CACHED_APPS_TIMESTAMP = now_ts
        gc.collect()
    except Exception as e:
        print("Error getting apps:", e)
        if CACHED_APPS_LIST:
            return CACHED_APPS_LIST
        return fallback_apps()

    return sideloaded_apps

def fallback_apps():
    return []

@app.post("/api/set-ip")
async def set_device_ip(request: Request):
    global LAST_KNOWN_IP
    ip = None
    try:
        data = await request.json()
        ip = data.get("ip")
    except Exception:
        pass
    if not ip:
        ip = request.query_params.get("ip")
    if not ip and request.client:
        cand = request.client.host
        if not is_local_or_docker(cand):
            ip = cand
    if ip and not is_local_or_docker(ip):
        LAST_KNOWN_IP = ip.strip()
        save_last_known_ip(LAST_KNOWN_IP)
        register_device_to_netmuxd(LAST_KNOWN_IP)
        alive = is_device_alive(LAST_KNOWN_IP)
        return {"success": True, "ip": LAST_KNOWN_IP, "online": alive}
    return JSONResponse(status_code=400, content={"success": False, "message": "Invalid IP address"})

@app.api_route("/refresh", methods=["GET", "POST"])
@app.api_route("/api/refresh", methods=["GET", "POST"])
async def refresh_all(request: Request):
    global LAST_KNOWN_IP, CACHED_APPS_TIMESTAMP
    client_ip = request.client.host if request.client else None
    query_ip = request.query_params.get("ip")
    if query_ip and not is_local_or_docker(query_ip):
        LAST_KNOWN_IP = query_ip.strip()
        save_last_known_ip(LAST_KNOWN_IP)
    elif client_ip and not is_local_or_docker(client_ip):
        LAST_KNOWN_IP = client_ip.strip()
        save_last_known_ip(LAST_KNOWN_IP)

    bundle_ids_to_refresh = set()
    if os.path.exists(PROFILES_DIR):
        for fname in os.listdir(PROFILES_DIR):
            if fname.endswith(".mobileprovision"):
                bid = fname[:-len(".mobileprovision")]
                bundle_ids_to_refresh.add(bid)
                
    try:
        ld = await get_lockdown_client()
        if ld:
            async with InstallationProxyService(ld) as ips:
                apps = await ips.get_apps(application_type="User")
                for bid, info in apps.items():
                    if (TEAM_ID and TEAM_ID in bid) or info.get("ProfileValidated"):
                        bundle_ids_to_refresh.add(bid)
    except Exception:
        pass

    renewed_apps = []
    already_active_apps = []
    failed_apps = []

    for bid in bundle_ids_to_refresh:
        parts = [p for p in bid.split(".") if p != TEAM_ID]
        clean_name = parts[-1].capitalize() if parts else "App"
        success, message, renewed = await push_certificate_profile_only(bid)
        if success and renewed:
            renewed_apps.append(clean_name)
        elif success and not renewed:
            already_active_apps.append(clean_name)
        else:
            failed_apps.append(f"{clean_name} ({message})")

    CACHED_APPS_TIMESTAMP = 0.0
    gc.collect()

    if renewed_apps:
        msg = f"Renewed (7 days): {', '.join(sorted(set(renewed_apps)))}!"
        if already_active_apps:
            msg += f" (Already valid: {', '.join(sorted(set(already_active_apps)))})"
        return {"success": True, "message": msg}
    elif already_active_apps:
        min_days = 7
        now = datetime.now(timezone.utc)
        for fname in os.listdir(PROFILES_DIR):
            if fname.endswith(".mobileprovision"):
                exp = extract_profile_expiration(os.path.join(PROFILES_DIR, fname))
                if exp:
                    d = max(0, (exp - now).days)
                    if d < min_days:
                        min_days = d
        msg = f"Apps are already up to date ({min_days}d left). Apple free accounts only renew certificates closer to expiration (<= 2 days)."
        return {"success": True, "message": msg}
    else:
        err_detail = "; ".join(failed_apps) if failed_apps else "Ensure iPhone screen is awake and connected to home Wi-Fi or SSTP VPN."
        return JSONResponse(
            status_code=500, 
            content={
                "success": False, 
                "message": f"Refresh failed: {err_detail}"
            }
        )

@app.post("/api/refresh-app")
async def refresh_single_app(request: Request, bundle_id: str = Form(...)):
    global LAST_KNOWN_IP, CACHED_APPS_TIMESTAMP
    client_ip = request.client.host if request.client else None
    query_ip = request.query_params.get("ip")
    if query_ip and not is_local_or_docker(query_ip):
        LAST_KNOWN_IP = query_ip.strip()
        save_last_known_ip(LAST_KNOWN_IP)
    elif client_ip and not is_local_or_docker(client_ip):
        LAST_KNOWN_IP = client_ip.strip()
        save_last_known_ip(LAST_KNOWN_IP)

    success, message, renewed = await push_certificate_profile_only(bundle_id)
    CACHED_APPS_TIMESTAMP = 0.0
    gc.collect()
    if success:
        return {"success": True, "message": message}
    else:
        return JSONResponse(
            status_code=500, 
            content={
                "success": False, 
                "message": f"{message}. Ensure screen is awake on Wi-Fi or SSTP VPN."
            }
        )

LAST_AUTO_REFRESH_TIME = 0.0

async def device_network_watcher_loop():
    global LAST_AUTO_REFRESH_TIME
    print("[Device Watcher] Background network watcher started.")
    was_online = False
    
    while True:
        try:
            await asyncio.sleep(45)
            if not LAST_KNOWN_IP:
                continue
                
            is_online = is_device_alive(LAST_KNOWN_IP)
            now_ts = time.time()
            
            # If device just came online, or hasn't had auto-refresh checked in 12 hours while online
            just_connected = (not was_online and is_online)
            routine_check_due = (is_online and (now_ts - LAST_AUTO_REFRESH_TIME > 12 * 3600))
            
            if just_connected or routine_check_due:
                print(f"[Device Watcher] iPhone detected online at {LAST_KNOWN_IP} (just_connected={just_connected}, routine_check_due={routine_check_due}).")
                register_device_to_netmuxd(LAST_KNOWN_IP)
                await asyncio.sleep(5)  # Let connection stabilize
                
                ld = await get_lockdown_client()
                if ld:
                    print("[Device Watcher] Running automatic background refresh...")
                    LAST_AUTO_REFRESH_TIME = now_ts
                    bundle_ids = set()
                    if os.path.exists(PROFILES_DIR):
                        for fname in os.listdir(PROFILES_DIR):
                            if fname.endswith(".mobileprovision"):
                                bundle_ids.add(fname[:-len(".mobileprovision")])
                    for bid in bundle_ids:
                        try:
                            s, m, renewed = await push_certificate_profile_only(bid)
                            print(f"[Device Watcher] Auto-refresh {bid}: {m}")
                        except Exception as ex:
                            print(f"[Device Watcher] Auto-refresh {bid} error: {ex}")
            
            was_online = is_online
        except Exception as e:
            print("[Device Watcher] Error in watcher loop:", e)
            await asyncio.sleep(60)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(device_network_watcher_loop())

@app.get("/api/logs")
def get_debug_logs():
    try:
        res = subprocess.run(
            ["journalctl", "-u", "ios-sideload-server.service", "-u", "netmuxd.service", "-n", "80", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=3
        )
        return {"logs": res.stdout or res.stderr or "No logs available"}
    except Exception as e:
        return {"logs": f"Error reading logs: {e}"}

@app.post("/install")
@app.post("/api/install")
async def install_file(file: UploadFile = File(None), url: Optional[str] = Form(None)):
    if file:
        with tempfile.NamedTemporaryFile(suffix=".ipa", delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name
    elif url:
        tmp_path = tempfile.mktemp(suffix=".ipa")
        urllib.request.urlretrieve(url, tmp_path)
    else:
        return JSONResponse(status_code=400, content={"error": "Must provide either file or url"})

    try:
        success, message = await wireless_sign_and_install(tmp_path)
        if success:
            return {"success": True, "message": message}
        else:
            return JSONResponse(status_code=500, content={"success": False, "message": "Install failed", "details": message})
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@app.get("/", response_class=HTMLResponse)
def pwa_index(request: Request):
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, user-scalable=no">
        <meta name="apple-mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
        <meta name="apple-mobile-web-app-title" content="SideStore">
        <meta name="theme-color" content="#0b0f19">
        <link rel="manifest" href="/manifest.json">
        <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
        <link rel="icon" type="image/png" href="/static/icon-192.png">
        <title>SideStore Remote</title>
        <style>
            :root {
                --bg: #0b0f19;
                --card-bg: rgba(22, 31, 48, 0.75);
                --card-border: rgba(255, 255, 255, 0.08);
                --accent: #38bdf8;
                --accent-gradient: linear-gradient(135deg, #38bdf8 0%, #3b82f6 100%);
                --success: #10b981;
                --warning: #f59e0b;
                --danger: #ef4444;
                --text: #f8fafc;
                --text-muted: #94a3b8;
            }
            * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                background: var(--bg);
                color: var(--text);
                margin: 0;
                padding: env(safe-area-inset-top, 20px) 16px env(safe-area-inset-bottom, 30px) 16px;
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                align-items: center;
            }
            .app-container {
                width: 100%;
                max-width: 520px;
                padding-bottom: 60px;
            }
            .header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-top: 10px;
                margin-bottom: 20px;
            }
            .header-title {
                font-size: 1.75rem;
                font-weight: 800;
                background: linear-gradient(135deg, #ffffff 0%, #cbd5e1 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                letter-spacing: -0.5px;
            }
            .header-actions { display: flex; gap: 10px; }
            .icon-btn {
                background: var(--card-bg);
                border: 1px solid var(--card-border);
                color: var(--accent);
                width: 42px;
                height: 42px;
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 1.25rem;
                cursor: pointer;
                backdrop-filter: blur(12px);
                transition: transform 0.15s, background 0.15s;
            }
            .icon-btn:active { transform: scale(0.92); }
            
            .status-banner {
                background: var(--card-bg);
                border: 1px solid var(--card-border);
                border-radius: 18px;
                padding: 16px;
                margin-bottom: 20px;
                backdrop-filter: blur(16px);
                display: flex;
                justify-content: space-between;
                align-items: center;
            }
            .device-info { display: flex; flex-direction: column; gap: 4px; }
            .device-name { font-size: 1.1rem; font-weight: 700; }
            .device-sub { font-size: 0.82rem; color: var(--text-muted); }
            .status-badge {
                padding: 6px 12px;
                border-radius: 20px;
                font-size: 0.8rem;
                font-weight: 600;
                display: flex;
                align-items: center;
                gap: 6px;
            }
            .status-online { background: rgba(16, 185, 129, 0.15); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.3); }
            .status-offline { background: rgba(239, 68, 68, 0.15); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.3); }
            .pulse-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; animation: pulse 2s infinite; }
            @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

            .stats-grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 12px;
                margin-bottom: 24px;
            }
            .stat-card {
                background: var(--card-bg);
                border: 1px solid var(--card-border);
                border-radius: 16px;
                padding: 14px;
                backdrop-filter: blur(16px);
            }
            .stat-label { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 6px; }
            .stat-value { font-size: 1.25rem; font-weight: 700; color: #fff; }
            .progress-bar-bg { width: 100%; height: 6px; background: rgba(255,255,255,0.1); border-radius: 3px; margin-top: 8px; overflow: hidden; }
            .progress-bar-fill { height: 100%; border-radius: 3px; background: var(--accent-gradient); transition: width 0.4s ease; }

            .section-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 12px;
            }
            .section-title { font-size: 1.15rem; font-weight: 700; color: #e2e8f0; }
            .refresh-all-btn {
                background: rgba(56, 189, 248, 0.15);
                border: 1px solid rgba(56, 189, 248, 0.3);
                color: var(--accent);
                padding: 6px 14px;
                border-radius: 12px;
                font-size: 0.85rem;
                font-weight: 600;
                cursor: pointer;
                transition: transform 0.15s;
            }
            .refresh-all-btn:active { transform: scale(0.95); }

            .app-list { display: flex; flex-direction: column; gap: 12px; }
            .app-card {
                background: var(--card-bg);
                border: 1px solid var(--card-border);
                border-radius: 18px;
                padding: 16px;
                backdrop-filter: blur(16px);
                display: flex;
                align-items: center;
                justify-content: space-between;
                transition: transform 0.15s;
            }
            .app-main { display: flex; align-items: center; gap: 14px; }
            .app-icon {
                width: 50px;
                height: 50px;
                border-radius: 14px;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 1.4rem;
                font-weight: 800;
                color: #fff;
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                flex-shrink: 0;
                overflow: hidden;
            }
            .app-icon-img {
                width: 100%;
                height: 100%;
                object-fit: cover;
                border-radius: 14px;
                display: block;
            }
            .app-details { display: flex; flex-direction: column; gap: 4px; }
            .app-name { font-size: 1.05rem; font-weight: 700; color: #fff; }
            .app-bid { font-size: 0.76rem; color: var(--text-muted); word-break: break-all; max-width: 200px; }
            .app-pill {
                display: inline-flex;
                align-items: center;
                gap: 4px;
                font-size: 0.75rem;
                font-weight: 600;
                padding: 3px 8px;
                border-radius: 8px;
                margin-top: 2px;
                width: fit-content;
            }
            .pill-green { background: rgba(16, 185, 129, 0.2); color: #34d399; }
            .pill-orange { background: rgba(245, 158, 11, 0.2); color: #fbbf24; }
            .pill-red { background: rgba(239, 68, 68, 0.2); color: #f87171; }

            .app-action-btn {
                background: rgba(16, 185, 129, 0.2);
                border: 1px solid rgba(16, 185, 129, 0.4);
                color: #34d399;
                padding: 7px 14px;
                border-radius: 12px;
                font-size: 0.82rem;
                font-weight: 700;
                cursor: pointer;
                display: inline-flex;
                align-items: center;
                gap: 6px;
                transition: transform 0.15s, background 0.2s;
                white-space: nowrap;
            }
            .app-action-btn:active { transform: scale(0.95); }
            .app-action-btn.btn-days-green {
                background: rgba(16, 185, 129, 0.2);
                border-color: rgba(16, 185, 129, 0.4);
                color: #34d399;
            }
            .app-action-btn.btn-days-yellow {
                background: rgba(245, 158, 11, 0.2);
                border-color: rgba(245, 158, 11, 0.4);
                color: #fbbf24;
            }
            .app-action-btn.btn-days-red {
                background: rgba(239, 68, 68, 0.2);
                border-color: rgba(239, 68, 68, 0.4);
                color: #f87171;
            }
            .app-action-btn:disabled {
                opacity: 0.7;
                cursor: not-allowed;
            }
            .spinner-icon {
                width: 13px;
                height: 13px;
                border: 2px solid currentColor;
                border-right-color: transparent;
                border-radius: 50%;
                display: inline-block;
                animation: spin 0.75s linear infinite;
            }
            @keyframes spin { 100% { transform: rotate(360deg); } }

            .app-ids-footer {
                margin-top: 14px;
                padding: 10px 14px;
                border-radius: 12px;
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid rgba(255, 255, 255, 0.05);
                font-size: 0.8rem;
                color: var(--text-muted);
                display: flex;
                align-items: center;
                justify-content: space-between;
            }

            .modal-overlay {
                position: fixed;
                top: 0; left: 0; right: 0; bottom: 0;
                background: rgba(0, 0, 0, 0.7);
                backdrop-filter: blur(10px);
                display: none;
                justify-content: center;
                align-items: flex-end;
                z-index: 100;
                padding: 16px;
            }
            .modal-card {
                background: #1e293b;
                border: 1px solid var(--card-border);
                border-radius: 24px;
                width: 100%;
                max-width: 520px;
                padding: 22px;
                margin-bottom: env(safe-area-inset-bottom, 10px);
                animation: slideUp 0.3s ease;
            }
            @keyframes slideUp { from { transform: translateY(100%); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
            .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
            .modal-title { font-size: 1.15rem; font-weight: 700; }
            .close-btn { background: none; border: none; font-size: 1.5rem; color: var(--text-muted); cursor: pointer; }
            
            .console-box {
                background: #090d16;
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 14px;
                padding: 12px;
                font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
                font-size: 0.74rem;
                line-height: 1.4;
                color: #38bdf8;
                max-height: 350px;
                overflow-y: auto;
                white-space: pre-wrap;
                word-break: break-all;
            }
            .console-actions {
                display: flex;
                justify-content: flex-end;
                gap: 8px;
                margin-top: 12px;
            }
            .btn-secondary {
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.12);
                color: #fff;
                padding: 7px 14px;
                border-radius: 10px;
                font-size: 0.8rem;
                font-weight: 600;
                cursor: pointer;
            }
            .btn-secondary:active { transform: scale(0.96); }

            .drop-zone {
                border: 2px dashed rgba(56, 189, 248, 0.4);
                border-radius: 16px;
                padding: 28px 16px;
                text-align: center;
                background: rgba(15, 23, 42, 0.6);
                cursor: pointer;
                margin-bottom: 16px;
            }
            .drop-zone:hover { border-color: var(--accent); }
            .btn-install {
                background: var(--accent-gradient);
                color: #fff;
                border: none;
                width: 100%;
                padding: 14px;
                border-radius: 14px;
                font-size: 1rem;
                font-weight: 700;
                cursor: pointer;
                transition: transform 0.15s;
            }
            .btn-install:active { transform: scale(0.98); }
            
            .toast {
                position: fixed;
                bottom: calc(env(safe-area-inset-bottom, 20px) + 20px);
                left: 50%;
                transform: translateX(-50%) translateY(140px);
                background: rgba(30, 41, 59, 0.95);
                border: 1px solid rgba(255, 255, 255, 0.15);
                backdrop-filter: blur(20px);
                box-shadow: 0 12px 35px rgba(0, 0, 0, 0.6);
                color: #fff;
                padding: 14px 22px;
                border-radius: 28px;
                font-size: 0.92rem;
                font-weight: 600;
                display: flex;
                align-items: center;
                gap: 10px;
                z-index: 1000;
                max-width: 90%;
                width: max-content;
                transition: transform 0.35s cubic-bezier(0.18, 0.89, 0.32, 1.28);
            }
            .toast.show { transform: translateX(-50%) translateY(0); }
        </style>
    </head>
    <body>
        <div id="toast" class="toast"></div>

        <div class="app-container">
            <div class="header">
                <div class="header-title">SideStore</div>
                <div class="header-actions">
                    <button class="icon-btn" onclick="openConsoleModal()" title="Debug Console">⚙️</button>
                    <button class="icon-btn" onclick="openInstallModal()" title="Install IPA">＋</button>
                    <button class="icon-btn" id="headerRefreshBtn" onclick="triggerRefreshAll()" title="Refresh All">↻</button>
                </div>
            </div>

            <div class="status-banner">
                <div class="device-info">
                    <div class="device-name" id="devName">iOS Device</div>
                    <div class="device-sub" id="devSub">Connecting...</div>
                </div>
                <div class="status-badge status-online" id="statusBadge">
                    <div class="pulse-dot"></div>
                    <span id="statusText">Connected</span>
                </div>
            </div>

            <div class="section-header">
                <div class="section-title">My Apps</div>
                <button class="refresh-all-btn" id="refreshAllBtn" onclick="triggerRefreshAll()">Refresh All</button>
            </div>

            <div class="app-list" id="appList">
                <div style="text-align: center; color: var(--text-muted); padding: 20px;">Loading installed apps...</div>
            </div>

            <div class="app-ids-footer" id="appIdsFooter">
                <span>Active App IDs</span>
                <span style="font-weight: 700; color: #f8fafc;" id="appIdsText">-- / 10 used</span>
            </div>
        </div>

        <!-- Debug Console Modal -->
        <div class="modal-overlay" id="consoleModal">
            <div class="modal-card">
                <div class="modal-header">
                    <div style="display:flex; align-items:center; gap:8px;">
                        <div class="modal-title">Debug Console</div>
                        <span id="consoleLiveBadge" style="display:inline-flex; align-items:center; gap:5px; font-size:0.7rem; font-weight:700; color:#10b981; background:rgba(16,185,129,0.15); border:1px solid rgba(16,185,129,0.3); padding:2px 8px; border-radius:12px;">
                            <span class="pulse-dot" style="width:6px; height:6px;"></span> LIVE
                        </span>
                    </div>
                    <button class="close-btn" onclick="closeConsoleModal()">✕</button>
                </div>
                <div class="console-box" id="consoleOutput">Connecting to log stream...</div>
                <div class="console-actions">
                    <button class="btn-secondary" onclick="fetchLogs()">Clear & Refresh</button>
                    <button class="btn-secondary" onclick="closeConsoleModal()">Close</button>
                </div>
            </div>
        </div>

        <!-- Sideload Modal -->
        <div class="modal-overlay" id="installModal">
            <div class="modal-card">
                <div class="modal-header">
                    <div class="modal-title">Sideload New App</div>
                    <button class="close-btn" onclick="closeInstallModal()">✕</button>
                </div>
                <div class="drop-zone" onclick="document.getElementById('fileInput').click()">
                    <div style="font-size: 2rem; margin-bottom: 8px;">📦</div>
                    <div style="font-weight: 600; margin-bottom: 4px;" id="dropText">Choose .IPA file</div>
                    <div style="font-size: 0.8rem; color: var(--text-muted);">Tap to select from Files app</div>
                    <input type="file" id="fileInput" accept=".ipa" style="display: none;" onchange="onFileSelected(event)">
                </div>
                <button class="btn-install" id="installBtn" onclick="uploadAndInstall()">Install App Over The Air</button>
            </div>
        </div>

        <script>
            let swRegistration = null;

            if ('serviceWorker' in navigator) {
                navigator.serviceWorker.register('/sw.js').then(reg => {
                    swRegistration = reg;
                    console.log('SW active');
                });
            }

            function showToast(msg, isSuccess = true) {
                const t = document.getElementById('toast');
                t.innerHTML = (isSuccess ? '✅ ' : '❌ ') + msg;
                t.classList.add('show');
                setTimeout(() => t.classList.remove('show'), 3500);
            }

            async function loadStatus() {
                try {
                    const res = await fetch('/api/status');
                    const d = await res.json();
                    document.getElementById('devName').innerText = d.device_name;
                    document.getElementById('devSub').innerText = `${d.model || 'iPhone'} • ${d.connection_type} (${d.ip})`;
                    const badge = document.getElementById('statusBadge');
                    const st = document.getElementById('statusText');
                    if (d.online) {
                        badge.className = 'status-badge status-online';
                        st.innerText = 'Connected';
                    } else {
                        badge.className = 'status-badge status-offline';
                        st.innerText = 'Offline';
                    }
                    if (document.getElementById('appIdsText')) {
                        document.getElementById('appIdsText').innerText = `${d.app_ids_used} / ${d.app_ids_max} used`;
                    }
                } catch(e) { console.error(e); }
            }

            async function loadApps() {
                try {
                    const res = await fetch('/api/apps');
                    const apps = await res.json();
                    const container = document.getElementById('appList');
                    if (!apps || apps.length === 0) {
                        container.innerHTML = '<div style="text-align:center; color: var(--text-muted); padding: 20px;">No sideloaded apps found.</div>';
                        return;
                    }
                    container.innerHTML = apps.map(app => {
                        let btnColorClass = 'btn-days-green';
                        if (app.days_left <= 1) btnColorClass = 'btn-days-red';
                        else if (app.days_left <= 3) btnColorClass = 'btn-days-yellow';

                        const daysLabel = (app.days_left === 1) ? '1 day' : `${app.days_left} days`;

                        const iconLetter = app.name ? app.name.charAt(0).toUpperCase() : 'A';
                        const gradients = [
                            'linear-gradient(135deg, #0284c7 0%, #6366f1 100%)',
                            'linear-gradient(135deg, #10b981 0%, #059669 100%)',
                            'linear-gradient(135deg, #8b5cf6 0%, #ec4899 100%)',
                            'linear-gradient(135deg, #f59e0b 0%, #ef4444 100%)',
                            'linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%)'
                        ];
                        let hash = 0;
                        for (let i = 0; i < (app.name || '').length; i++) hash = (app.name.charCodeAt(i) + ((hash << 5) - hash)) | 0;
                        const gradient = gradients[Math.abs(hash) % gradients.length];

                        const iconHtml = app.icon_url 
                            ? `<img class="app-icon-img" src="${app.icon_url}" alt="${app.name}" onerror="this.parentElement.innerHTML='${iconLetter}'">`
                            : iconLetter;

                        return `
                        <div class="app-card">
                            <div class="app-main">
                                <div class="app-icon" style="background: ${gradient}">${iconHtml}</div>
                                <div class="app-details">
                                    <div class="app-name">${app.name} <span style="font-size:0.75rem; color:var(--text-muted)">v${app.version}</span></div>
                                    <div class="app-bid">${app.bundle_id}</div>
                                </div>
                            </div>
                            <button class="app-action-btn ${btnColorClass}" id="btn-${app.bundle_id}" data-days="${daysLabel}" onclick="refreshApp('${app.bundle_id}')">${daysLabel}</button>
                        </div>
                        `;
                    }).join('');
                } catch(e) { console.error(e); }
            }

            async function triggerRefreshAll() {
                const btn = document.getElementById('refreshAllBtn');
                const headerBtn = document.getElementById('headerRefreshBtn');
                if (btn) {
                    btn.innerHTML = '<span class="spinner-icon"></span> Refreshing...';
                    btn.disabled = true;
                }
                if (headerBtn) {
                    headerBtn.disabled = true;
                }
                showToast('Refreshing certificates for all apps...', true);
                try {
                    const res = await fetch('/api/refresh', { method: 'POST' });
                    const d = await res.json();
                    if (d.success) {
                        showToast(d.message, true);
                        await loadApps();
                    } else {
                        showToast(d.details || d.message, false);
                    }
                } catch(e) {
                    showToast('Refresh failed: ' + e, false);
                } finally {
                    if (btn) {
                        btn.innerHTML = 'Refresh All';
                        btn.disabled = false;
                    }
                    if (headerBtn) {
                        headerBtn.disabled = false;
                    }
                }
            }

            async function refreshApp(bundleId) {
                const btn = document.getElementById(`btn-${bundleId}`);
                const originalLabel = btn ? (btn.getAttribute('data-days') || btn.innerText) : '';
                if (btn) {
                    btn.innerHTML = '<span class="spinner-icon"></span> Refreshing...';
                    btn.disabled = true;
                }
                showToast('Refreshing certificate from Apple...', true);
                const form = new FormData();
                form.append('bundle_id', bundleId);
                try {
                    const res = await fetch('/api/refresh-app', { method: 'POST', body: form });
                    const d = await res.json();
                    if (d.success) {
                        showToast(d.message, true);
                        await loadApps();
                    } else {
                        showToast(d.details || d.message, false);
                        if (btn) {
                            btn.innerHTML = originalLabel;
                            btn.disabled = false;
                        }
                    }
                } catch(e) {
                    showToast('Error: ' + e, false);
                    if (btn) {
                        btn.innerHTML = originalLabel;
                        btn.disabled = false;
                    }
                }
            }

            let consoleTimer = null;

            function openConsoleModal() {
                document.getElementById('consoleModal').style.display = 'flex';
                fetchLogs();
                // Live streaming: update logs every 2 seconds while console is open
                if (consoleTimer) clearInterval(consoleTimer);
                consoleTimer = setInterval(fetchLogs, 2000);
            }

            function closeConsoleModal() {
                document.getElementById('consoleModal').style.display = 'none';
                if (consoleTimer) {
                    clearInterval(consoleTimer);
                    consoleTimer = null;
                }
            }

            async function fetchLogs() {
                const out = document.getElementById('consoleOutput');
                try {
                    const res = await fetch('/api/logs');
                    const d = await res.json();
                    const newLogs = d.logs || 'No logs available';
                    if (out.innerText !== newLogs) {
                        const isScrolledToBottom = out.scrollHeight - out.clientHeight <= out.scrollTop + 40;
                        out.innerText = newLogs;
                        if (isScrolledToBottom) {
                            out.scrollTop = out.scrollHeight;
                        }
                    }
                } catch(e) {
                    console.error('Error fetching logs:', e);
                }
            }

            function openInstallModal() {
                document.getElementById('installModal').style.display = 'flex';
            }
            function closeInstallModal() {
                document.getElementById('installModal').style.display = 'none';
            }
            function onFileSelected(e) {
                if (e.target.files[0]) {
                    document.getElementById('dropText').innerText = e.target.files[0].name;
                }
            }
            async function uploadAndInstall() {
                const fileInput = document.getElementById('fileInput');
                if (!fileInput.files[0]) {
                    alert('Please select an IPA file first');
                    return;
                }
                const btn = document.getElementById('installBtn');
                btn.innerText = '⏳ Provisioning & Installing over the air...';
                btn.disabled = true;

                const form = new FormData();
                form.append('file', fileInput.files[0]);

                try {
                    const res = await fetch('/api/install', { method: 'POST', body: form });
                    const d = await res.json();
                    if (d.success) {
                        showToast(d.message, true);
                        closeInstallModal();
                        await loadApps();
                    } else {
                        showToast(d.details || d.message, false);
                    }
                } catch(e) {
                    showToast('Installation error: ' + e, false);
                } finally {
                    btn.innerText = 'Install App Over The Air';
                    btn.disabled = false;
                }
            }

            loadStatus();
            loadApps();

            // Smart polling: only poll when tab is active
            setInterval(() => {
                if (!document.hidden) loadStatus();
            }, 30000);

            setInterval(() => {
                if (!document.hidden) loadApps();
            }, 120000);

            // Trigger immediate refresh when user returns to tab
            document.addEventListener('visibilitychange', () => {
                if (!document.hidden) {
                    loadStatus();
                }
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8899)
