"""Print the webhook address to paste into the "HomeAssistant Game Events" Overwolf app.

    python ha_link.py                 # bot running on this PC
    python ha_link.py --host valbet   # bot on a home server reachable as valbet.local

The app only accepts http://<name>.local addresses. Windows answers to <PC name>.local by itself; a Linux server
does with avahi-daemon (installed by deploy/setup.sh). The address contains the bot's private key: don't share it.
The bot must listen on the network (DASHBOARD_HOST=0.0.0.0) for the app to reach it.
"""

import argparse
import os
import socket

from dotenv import load_dotenv

load_dotenv()

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--host", default=socket.gethostname(), help="the bot machine's name (default: this PC)")
ap.add_argument("--port", default=os.getenv("DASHBOARD_PORT", "8787"))
args = ap.parse_args()

token = os.getenv("DASHBOARD_TOKEN")
if not token:
    import db  # noqa: E402
    token = db.get_meta("dashboard_token")
if not token:
    raise SystemExit("No key yet: start the bot once (it creates one), then run this again.")

host = args.host.split(".")[0]
print("Paste this into HomeAssistant Game Events -> Webhook URL (keep it private):\n")
print(f"http://{host}.local:{args.port}/api/ha/{token}\n")
if os.getenv("DASHBOARD_HOST", "127.0.0.1") in ("127.0.0.1", "localhost"):
    print("Note: set DASHBOARD_HOST=0.0.0.0 in .env and restart the bot, so the app can reach it.")
