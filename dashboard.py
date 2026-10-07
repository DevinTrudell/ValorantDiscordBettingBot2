"""Receives live game data from the Overwolf app (POST /api/overwolf). There is no web page or any other
control: betting only opens, closes and pays out automatically.

Served on 127.0.0.1 (this machine) by default. On a home server, set DASHBOARD_HOST=0.0.0.0 so the
Overwolf app on the gaming PC can reach it over the home network (never port-forward it to the internet).
Every call needs a key (in the X-Token header, or in the address from /overwolf-link), so other websites
and devices can't send fake game results.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import socket

from aiohttp import web

import db
import ha_bridge

log = logging.getLogger("valbet.overwolf")
HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("DASHBOARD_PORT", "8787"))
TOKEN = os.getenv("DASHBOARD_TOKEN") or db.get_or_create_meta("dashboard_token", lambda: secrets.token_urlsafe(18))


async def start(bot) -> None:
    app = web.Application(middlewares=[_auth])
    app["bot"] = bot
    app["bridge"] = ha_bridge.Bridge(bot.overwolf_event)
    app.add_routes([
        web.post("/api/overwolf", _overwolf),
        web.options("/api/overwolf", _preflight),
        # The "HomeAssistant Game Events" Overwolf app can't send headers, so its key is in the address.
        web.post("/api/ha/{key}", _ha),
        web.options("/api/ha/{key}", _preflight),
    ])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        # "0.0.0.0" = the whole home network, over IPv4 and IPv6 (<PC name>.local resolves to both); requests from
        # outside the home network are refused below.
        await web.TCPSite(runner, None if HOST in ("0.0.0.0", "::") else HOST, PORT).start()
    except OSError as e:
        log.error("Couldn't listen for the Overwolf app on %s:%s: %s", HOST, PORT, e)
        return
    log.info("Listening for the Overwolf app on %s:%s", HOST, PORT)


def _cors(request) -> dict:
    """The Overwolf app's page lives at overwolf-extension://…; only that origin may call the bot cross-site."""
    origin = request.headers.get("Origin", "")
    if origin.startswith("overwolf-extension://"):
        return {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Headers": "Content-Type, X-Token",
                "Access-Control-Allow-Methods": "POST, OPTIONS", "Access-Control-Allow-Private-Network": "true",
                "Vary": "Origin"}
    return {}


def _own_addresses() -> set[str]:
    try:
        return {a[4][0].split("%")[0] for a in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError:
        return set()


OWN = _own_addresses()


TAILSCALE = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def _from_home(remote: str | None) -> bool:
    """This machine, the home network, or a Tailscale private network (friends the bot's machine is shared
    with) only: never anything from the open internet."""
    try:
        ip = ipaddress.ip_address((remote or "").split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return (ip.is_loopback or ip.is_private or ip.is_link_local or str(ip) in OWN
            or any(ip in net for net in TAILSCALE if ip.version == net.version))


@web.middleware
async def _auth(request, handler):
    if not _from_home(request.remote):
        return web.Response(status=403)
    if request.method == "OPTIONS":
        return await handler(request)
    key = request.match_info.get("key") if request.path.startswith("/api/ha/") else None
    if key is not None and not secrets.compare_digest(key.encode(), TOKEN.encode()):
        # a person's own key (from /overwolf-link): only accepted for reports about their own games
        owner = db.overwolf_key_owner(key)
        if owner is None:
            return web.json_response({"error": "Unknown key. Get your address with /overwolf-link in Discord."},
                                     status=401, headers=_cors(request))
        request["owner"] = owner
        return await handler(request)
    given = key if key is not None else request.headers.get("X-Token", "")
    if not secrets.compare_digest(given.encode(), TOKEN.encode()):
        return web.json_response({"error": "Wrong or missing key. Get your address with /overwolf-link in Discord."}, status=401,
                                 headers=_cors(request))
    try:
        return await handler(request)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400, headers=_cors(request))


async def _preflight(request):
    return web.Response(status=204, headers=_cors(request))


async def _ha(request):
    """A batch of live game data from the HomeAssistant Game Events Overwolf app."""
    try:
        batch = await request.json()
    except json.JSONDecodeError:
        raise ValueError("Bad request.") from None
    owner = request.get("owner")  # None = the bot owner's master key: any linked player
    used = request.app["bridge"].feed(f"{request.remote}|{owner}", batch, owner)
    return web.json_response({"ok": True, "used": used}, headers=_cors(request))


async def _overwolf(request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise ValueError("Bad request.") from None
    if not isinstance(body, dict):
        raise ValueError("Bad request.")
    result = await request.app["bot"].overwolf_event(body)
    return web.json_response(result, headers=_cors(request))
