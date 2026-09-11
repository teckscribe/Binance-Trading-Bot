"""
ngrok_runner.py - Ngrok Tunnel Management for CSB Quad-Core Bot (Port 8102)
"""

import os
import json
import time
import urllib.request
import subprocess
import logging
import asyncio
import aiohttp
from typing import Optional, Tuple
from dotenv import load_dotenv

# Load .env explicitly
_PROJECT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_PROJECT, ".env"))

NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN", "")
NGROK_ENABLED = os.getenv("NGROK_ENABLED", "true").lower() == "true"
ACTIVE_WEB_PORT = 8102

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

log = logging.getLogger("CSBNgrok")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")

ACTIVE_PUBLIC_URL: Optional[str] = None
ACTIVE_NGROK_PROC: Optional[subprocess.Popen] = None

def get_active_ngrok_url(target_port: int = ACTIVE_WEB_PORT, force_refresh: bool = False) -> Optional[str]:
    """Queries active Ngrok public URL matching target_port across local API ports."""
    global ACTIVE_PUBLIC_URL
    if ACTIVE_PUBLIC_URL and not force_refresh:
        return ACTIVE_PUBLIC_URL

    api_ports = [4042, 4040, 4041, 4043, 4044, 4045]
    for api_port in api_ports:
        try:
            req = urllib.request.urlopen(f"http://127.0.0.1:{api_port}/api/tunnels", timeout=0.5)
            res = json.loads(req.read().decode("utf-8"))
            for t in res.get("tunnels", []):
                config_addr = str(t.get("config", {}).get("addr", ""))
                url = t.get("public_url", "")
                if (str(target_port) in config_addr or not config_addr) and url:
                    if url.startswith("http://"):
                        url = url.replace("http://", "https://")
                    ACTIVE_PUBLIC_URL = url
                    return url
        except Exception:
            continue

    try:
        from pyngrok import ngrok
        for t in ngrok.get_tunnels():
            config_addr = str(getattr(t, "config", {}).get("addr", ""))
            url = getattr(t, "public_url", "")
            if (str(target_port) in config_addr or not config_addr) and url:
                if url.startswith("http://"):
                    url = url.replace("http://", "https://")
                ACTIVE_PUBLIC_URL = url
                return url
    except Exception:
        pass

    return None


def start_ngrok_tunnel(port: int = ACTIVE_WEB_PORT) -> Tuple[bool, str]:
    """Establishes Ngrok tunnel targeting specified web port (8102)."""
    global ACTIVE_PUBLIC_URL, ACTIVE_NGROK_PROC

    existing_url = get_active_ngrok_url(port)
    if existing_url:
        ACTIVE_PUBLIC_URL = existing_url
        return True, existing_url

    if not NGROK_ENABLED:
        return False, "Ngrok is disabled in configuration (NGROK_ENABLED=FALSE)."

    if not NGROK_AUTHTOKEN:
        return False, "NGROK_AUTHTOKEN is missing in .env configuration."

    # Pre-configure CLI with authtoken to ensure CLI fallback / yml works without token in yml
    try:
        subprocess.run(["ngrok", "config", "add-authtoken", NGROK_AUTHTOKEN], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    # 1. Try config file ngrok.yml if present
    yml_path = os.path.join(_PROJECT, "ngrok.yml")
    if os.path.exists(yml_path):
        try:
            ACTIVE_NGROK_PROC = subprocess.Popen(
                ["ngrok", "start", "--config", yml_path, "csb_dashboard"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            time.sleep(3)
            url = get_active_ngrok_url(port, True)
            if url:
                ACTIVE_PUBLIC_URL = url
                log.info(f"🚀 Ngrok Tunnel established via ngrok.yml: {url} -> http://localhost:{port}")
                return True, url
        except Exception as yml_err:
            log.warning(f"ngrok.yml launch failed: {yml_err}. Falling back...")

    # 2. Try pyngrok
    try:
        from pyngrok import ngrok
        ngrok.set_auth_token(NGROK_AUTHTOKEN)
        tunnel = ngrok.connect(port, "http")
        url = tunnel.public_url
        if url.startswith("http://"):
            url = url.replace("http://", "https://")
        ACTIVE_PUBLIC_URL = url
        log.info(f"🚀 Ngrok pyngrok Tunnel established: {url} -> http://localhost:{port}")
        return True, url
    except Exception as e:
        log.warning(f"pyngrok connection attempt failed: {e}. Trying CLI fallback...")

    # 3. Try CLI subprocess fallback (no yml)
    try:
        ACTIVE_NGROK_PROC = subprocess.Popen(
            ["ngrok", "http", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        time.sleep(3)
        url = get_active_ngrok_url(port, True)
        if url:
            ACTIVE_PUBLIC_URL = url
            log.info(f"🚀 Ngrok CLI Tunnel established: {url} -> http://localhost:{port}")
            return True, url
        else:
            return False, "Ngrok process started but failed to fetch public tunnel URL."
    except Exception as ex:
        log.error(f"Ngrok CLI fallback error: {ex}")
        return False, f"Failed to start Ngrok tunnel: {str(ex)}"


def stop_ngrok_tunnel():
    """Kills active Ngrok subprocess or Pyngrok tunnels."""
    global ACTIVE_PUBLIC_URL, ACTIVE_NGROK_PROC
    try:
        from pyngrok import ngrok
        ngrok.kill()
    except Exception:
        pass

    if ACTIVE_NGROK_PROC:
        try:
            ACTIVE_NGROK_PROC.terminate()
            ACTIVE_NGROK_PROC = None
        except Exception:
            pass

    if os.name == "posix":
        subprocess.run(["/usr/bin/pkill", "-f", "ngrok"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif os.name == "nt":
        subprocess.run(["taskkill", "/F", "/IM", "ngrok.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ACTIVE_PUBLIC_URL = None
    log.info("⏹️ Ngrok tunnels terminated.")


async def send_notifications(public_url: str):
    """Sends active Ngrok public URL alert to Telegram and Discord."""
    
    msg = (
        f"🌐 **CSB Quad-Core Dashboard Live**\n\n"
        f"🔗 Public URL: {public_url}\n"
        f"⚡ Web UI Port: `{ACTIVE_WEB_PORT}`\n"
        f"🚀 Status: `ACTIVE`"
    )

    async with aiohttp.ClientSession() as session:
        # Telegram
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False
            }
            try:
                await session.post(api_url, json=payload, timeout=aiohttp.ClientTimeout(total=5))
                log.info("Telegram notification sent.")
            except Exception as e:
                log.error(f"Telegram notification dispatch error: {e}")

        # Discord
        if DISCORD_WEBHOOK_URL:
            payload = {
                "content": msg
            }
            try:
                await session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5))
                log.info("Discord notification sent.")
            except Exception as e:
                log.error(f"Discord webhook dispatch error: {e}")

if __name__ == "__main__":
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    
    if action == "start":
        ok, res = start_ngrok_tunnel()
        if ok:
            print(f"Success! URL: {res}")
            asyncio.run(send_notifications(res))
            
            # Keep alive so the subprocess doesn't exit if we used Popen
            try:
                while True:
                    time.sleep(60)
            except KeyboardInterrupt:
                stop_ngrok_tunnel()
        else:
            print(f"Error: {res}")
    
    elif action == "stop":
        stop_ngrok_tunnel()
        print("Ngrok stopped.")

