import os
import sys
import time
import json
import logging
import asyncio
import aiohttp

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
CONFIG_FILE = os.path.join(BASE_DIR, "tg_config.json")
LOCAL_API_BASE = os.getenv("LOCAL_API_BASE", "http://127.0.0.1:8899")
DEFAULT_CHAT_ID = int(os.getenv("TG_CHAT_ID", "0")) if os.getenv("TG_CHAT_ID") else None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("tg_bot")

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    subs = [DEFAULT_CHAT_ID] if DEFAULT_CHAT_ID else []
    return {"subscribers": subs, "last_notified_days": {}}

def save_config(config):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving config: {e}")

async def send_tg_message(session: aiohttp.ClientSession, chat_id: int, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with session.post(f"{API_URL}/sendMessage", json=payload, timeout=10) as resp:
            return await resp.json()
    except Exception as e:
        logger.error(f"Error sending TG message to {chat_id}: {e}")
        return None

async def answer_callback_query(session: aiohttp.ClientSession, callback_query_id: str, text: str = None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        async with session.post(f"{API_URL}/answerCallbackQuery", json=payload, timeout=5) as resp:
            return await resp.json()
    except Exception:
        pass

async def get_device_status(session: aiohttp.ClientSession):
    try:
        async with session.get(f"{LOCAL_API_BASE}/api/status", timeout=15) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        pass
    return None

async def get_apps_list(session: aiohttp.ClientSession):
    try:
        async with session.get(f"{LOCAL_API_BASE}/api/apps", timeout=15) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        pass
    return []

async def set_device_ip(session: aiohttp.ClientSession, ip: str):
    try:
        async with session.post(f"{LOCAL_API_BASE}/api/set-ip", json={"ip": ip}, timeout=10) as resp:
            return await resp.json()
    except Exception as e:
        return {"success": False, "message": str(e)}

async def trigger_refresh_all(session: aiohttp.ClientSession):
    try:
        async with session.post(f"{LOCAL_API_BASE}/api/refresh", timeout=40) as resp:
            return await resp.json()
    except Exception as e:
        return {"success": False, "message": str(e)}

async def trigger_refresh_app(session: aiohttp.ClientSession, bundle_id: str):
    try:
        data = aiohttp.FormData()
        data.add_field("bundle_id", bundle_id)
        async with session.post(f"{LOCAL_API_BASE}/api/refresh-app", data=data, timeout=35) as resp:
            return await resp.json()
    except Exception as e:
        return {"success": False, "message": str(e)}

def format_status_message(status, apps):
    if not status:
        return "⚠️ <b>SideStore Server is unreachable</b>"
    
    online_str = "🟢 Online" if status.get("online") else "🔴 Offline"
    text = (
        f"📱 <b>{status.get('device_name', 'iPhone')}</b>\n"
        f"• Status: {online_str} ({status.get('connection_type', 'LAN')})\n"
        f"• iOS: <code>{status.get('ios_version', 'N/A')}</code> | IP: <code>{status.get('ip', 'N/A')}</code>\n"
        f"• App IDs: <b>{status.get('app_ids_used', 0)} / {status.get('app_ids_max', 10)}</b> used\n\n"
        f"📦 <b>Installed Apps:</b>\n"
    )
    if not apps:
        if not status.get("online"):
            text += "<i>Device offline. If on SSTP VPN, unlock iPhone screen so iOS wakes the interface.</i>\n"
        else:
            text += "<i>No sideloaded apps detected.</i>\n"
    else:
        for app in apps:
            name = app.get("name", "Unknown")
            ver = app.get("version", "")
            time_left = app.get("time_left_str", "unknown")
            days = app.get("days_left", 0)
            if days <= 1:
                badge = "🔴"
            elif days <= 3:
                badge = "🟡"
            else:
                badge = "🟢"
            text += f"{badge} <b>{name}</b> (v{ver}): <b>{time_left}</b> (expires {app.get('expires_at', '')})\n"
            
    return text

def get_main_keyboard(apps):
    inline_keyboard = [
        [
            {"text": "🔄 Refresh All Apps", "callback_data": "refresh_all"},
            {"text": "📊 Check Status", "callback_data": "status"}
        ]
    ]
    app_buttons = []
    for app in apps:
        clean_name = app.get("name", "App")
        bid = app.get("bundle_id")
        app_buttons.append({"text": f"↻ {clean_name}", "callback_data": f"ref:{bid}"})
    if app_buttons:
        # Group in pairs
        grouped = [app_buttons[i:i+2] for i in range(0, len(app_buttons), 2)]
        inline_keyboard.extend(grouped)
        
    return {"inline_keyboard": inline_keyboard}

async def handle_update(session: aiohttp.ClientSession, update: dict, config: dict):
    if "message" in update:
        msg = update["message"]
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "")
        if not chat_id:
            return

        # Auto-subscribe
        if chat_id not in config["subscribers"]:
            config["subscribers"].append(chat_id)
            save_config(config)

        if text in ["/start", "/status", "/help"]:
            status = await get_device_status(session)
            apps = await get_apps_list(session)
            reply_text = format_status_message(status, apps)
            kb = get_main_keyboard(apps)
            await send_tg_message(session, chat_id, reply_text, reply_markup=kb)
            
        elif text in ["/refresh", "refresh"]:
            await send_tg_message(session, chat_id, "⏳ <i>Refreshing all apps over Wi-Fi / VPN...</i>")
            res = await trigger_refresh_all(session)
            if res.get("success"):
                await send_tg_message(session, chat_id, f"✅ {res.get('message', 'All apps refreshed!')}")
            else:
                msg = res.get('message', 'Refresh failed')
                await send_tg_message(session, chat_id, f"❌ {msg}\n\n💡 <i>Ensure iPhone screen is unlocked so iOS wakes the VPN interface.</i>")
            # Send updated status
            status = await get_device_status(session)
            apps = await get_apps_list(session)
            await send_tg_message(session, chat_id, format_status_message(status, apps), reply_markup=get_main_keyboard(apps))

        elif text.startswith("/ip"):
            parts = text.strip().split()
            if len(parts) > 1:
                target_ip = parts[1]
                res = await set_device_ip(session, target_ip)
                if res.get("success"):
                    st = "Online 🟢" if res.get("online") else "Offline 🔴"
                    await send_tg_message(session, chat_id, f"✅ Device target IP set to <code>{target_ip}</code> ({st})\n\n💡 <i>Try /status or tap Refresh.</i>")
                else:
                    await send_tg_message(session, chat_id, f"❌ Failed to set IP: {res.get('message')}")
            else:
                status = await get_device_status(session)
                cur_ip = status.get('ip', 'N/A') if status else 'N/A'
                await send_tg_message(session, chat_id, f"ℹ️ Current target IP: <code>{cur_ip}</code>\n\nTo update your VPN IP:\n<code>/ip &lt;ip_address&gt;</code> (e.g. <code>/ip 172.16.3.33</code>)")

    elif "callback_query" in update:
        cb = update["callback_query"]
        cb_id = cb["id"]
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        data = cb.get("data", "")

        if data == "status":
            await answer_callback_query(session, cb_id, "Updating status...")
            status = await get_device_status(session)
            apps = await get_apps_list(session)
            await send_tg_message(session, chat_id, format_status_message(status, apps), reply_markup=get_main_keyboard(apps))

        elif data == "refresh_all":
            await answer_callback_query(session, cb_id, "Refreshing apps...")
            await send_tg_message(session, chat_id, "⏳ <i>Refreshing all apps over Wi-Fi / VPN...</i>")
            res = await trigger_refresh_all(session)
            if res.get("success"):
                await send_tg_message(session, chat_id, f"✅ {res.get('message')}")
            else:
                msg = res.get('message', 'Refresh failed')
                await send_tg_message(session, chat_id, f"❌ {msg}\n\n💡 <i>Ensure iPhone screen is unlocked so iOS wakes the VPN interface.</i>")
            status = await get_device_status(session)
            apps = await get_apps_list(session)
            await send_tg_message(session, chat_id, format_status_message(status, apps), reply_markup=get_main_keyboard(apps))

        elif data.startswith("ref:"):
            bundle_id = data[4:]
            await answer_callback_query(session, cb_id, "Refreshing app...")
            await send_tg_message(session, chat_id, f"⏳ <i>Refreshing app ({bundle_id})...</i>")
            res = await trigger_refresh_app(session, bundle_id)
            if res.get("success"):
                await send_tg_message(session, chat_id, f"✅ {res.get('message')}")
            else:
                await send_tg_message(session, chat_id, f"❌ {res.get('message')}")
            status = await get_device_status(session)
            apps = await get_apps_list(session)
            await send_tg_message(session, chat_id, format_status_message(status, apps), reply_markup=get_main_keyboard(apps))

async def expiration_checker_loop(session: aiohttp.ClientSession, config: dict):
    """Checks every 30 minutes for expiring certificates (<= 2 days) and notifies subscribers."""
    while True:
        try:
            await asyncio.sleep(1800) # check every 30 mins
            apps = await get_apps_list(session)
            subscribers = config.get("subscribers", [])
            if not subscribers or not apps:
                continue

            last_notified = config.get("last_notified_days", {})
            alerts = []

            for app in apps:
                bid = app.get("bundle_id")
                name = app.get("name")
                days = app.get("days_left", 7)
                hours = app.get("hours_left", 168)

                # Trigger alert if 2 days or less left
                if days <= 2:
                    last_notified_day = last_notified.get(bid, 999)
                    # Only notify once per day threshold unless it drops further
                    if days < last_notified_day:
                        alerts.append(f"⚠️ <b>{name}</b> has only <b>{app.get('time_left_str')}</b> left before expiration!")
                        last_notified[bid] = days
                else:
                    # Reset tracker if refreshed
                    if bid in last_notified:
                        del last_notified[bid]

            config["last_notified_days"] = last_notified
            save_config(config)

            if alerts:
                alert_text = "🚨 <b>SideStore App Expiration Warning:</b>\n\n" + "\n".join(alerts) + "\n\nTap below to refresh immediately:"
                kb = get_main_keyboard(apps)
                for chat_id in subscribers:
                    await send_tg_message(session, chat_id, alert_text, reply_markup=kb)

        except Exception as e:
            logger.error(f"Error in expiration checker: {e}")
            await asyncio.sleep(60)

async def telegram_polling_loop(session: aiohttp.ClientSession, config: dict):
    offset = 0
    while True:
        try:
            url = f"{API_URL}/getUpdates?offset={offset}&timeout=30"
            async with session.get(url, timeout=35) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        for item in data.get("result", []):
                            offset = item["update_id"] + 1
                            await handle_update(session, item, config)
                elif resp.status in [409, 401]:
                    logger.error(f"Telegram polling error status: {resp.status}")
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Telegram polling exception: {e}")
            await asyncio.sleep(3)

async def main():
    config = load_config()
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Notify initial startup
        subscribers = config.get("subscribers", [])
        status = await get_device_status(session)
        apps = await get_apps_list(session)
        startup_msg = "🤖 <b>SideStore Helper Bot is online!</b>\n\n" + format_status_message(status, apps)
        for s in subscribers:
            await send_tg_message(session, s, startup_msg, reply_markup=get_main_keyboard(apps))

        await asyncio.gather(
            telegram_polling_loop(session, config),
            expiration_checker_loop(session, config)
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
