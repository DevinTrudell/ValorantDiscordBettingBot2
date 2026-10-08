"""Valorant rank and agent icons as the bot's own emojis, plus map pictures, from valorant-api.com
(a free community service hosting Riot's game assets).

On startup the bot uploads any icons it doesn't have yet as *application emojis* (they belong to the bot, work
in every server it's in, and stay uploaded, so this only happens once). After that, rank("Gold 2") gives
"<:vr_gold_2:123…>" for use in messages; if an icon isn't available it falls back to plain text.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl

import aiohttp
import certifi
import discord

log = logging.getLogger("valbet.icons")
API = "https://valorant-api.com/v1"

RANKS: dict[str, str] = {}   # "gold 2" -> "<:vr_gold_2:id>"
AGENTS: dict[str, str] = {}  # "jett" -> "<:va_jett:id>"
MAPS: dict[str, str] = {}    # "ascent" -> picture URL
BLANK = ["\u2003"]          # transparent emoji the size of an icon (for lining things up); em space until loaded


def _transparent_png(size: int = 64) -> bytes:
    """A fully transparent square PNG."""
    import struct, zlib
    raw = b"".join(b"\x00" + b"\x00\x00\x00\x00" * size for _ in range(size))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def blank() -> str:
    return BLANK[0]


SURE: list = [None]  # the "are you sure?" confirm-button emoji, if are_you_sure.png was provided


def are_you_sure():
    return SURE[0]


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).strip()


def _emoji_name(prefix: str, name: str) -> str:
    return (prefix + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"))[:32]


def rank(name: str | None, fallback: bool = True) -> str:
    """Rank icon for 'Gold 2' / 'Unranked' (text if the icon isn't loaded). HenrikDev says 'Unrated'."""
    key = _key(name or "unranked")
    key = "unranked" if key in ("unrated", "", "none") else key
    return RANKS.get(key) or (name or "Unranked" if fallback else "")


def agent(name: str | None) -> str:
    return AGENTS.get(_key(name or "")) or ""


def map_picture(name: str | None) -> str | None:
    return MAPS.get(_key(name or ""))


async def _json(session, path):
    async with session.get(API + path) as r:
        r.raise_for_status()
        return (await r.json())["data"]


async def ensure(client: discord.Client):
    """Load (and upload any missing) icons. Safe to run every startup; failures just mean text instead of icons."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx),
                                     timeout=aiohttp.ClientTimeout(total=60)) as s:
        try:
            tiers = (await _json(s, "/competitivetiers"))[-1]["tiers"]
            agents = await _json(s, "/agents?isPlayableCharacter=true")
            maps = await _json(s, "/maps")
        except Exception as e:  # noqa: BLE001
            log.warning("Couldn't load Valorant icons (%s); using text instead", e)
            return
        for mp in maps:
            if mp.get("displayName") and (mp.get("listViewIconTall") or mp.get("splash")):
                MAPS[_key(mp["displayName"])] = mp.get("splash") or mp.get("listViewIconTall")

        wanted = {}  # emoji name -> (lookup dict, key, image url)
        for t in tiers:
            name, url = t.get("tierName") or "", t.get("smallIcon") or t.get("largeIcon")
            if url and not name.lower().startswith("unused"):
                wanted[_emoji_name("vr_", name)] = (RANKS, _key(name), url)
        for a in agents:
            name, url = a.get("displayName") or "", a.get("displayIconSmall") or a.get("displayIcon")
            if url:
                wanted[_emoji_name("va_", name)] = (AGENTS, _key(name), url)

        try:
            have = {e.name: e for e in await client.fetch_application_emojis()}
        except discord.HTTPException as e:
            log.warning("Couldn't list the bot's emojis (%s); using text instead", e)
            return
        made = 0
        if "v_blank" in have:
            BLANK[0] = str(have["v_blank"])
        else:
            try:
                BLANK[0] = str(await client.create_application_emoji(name="v_blank", image=_transparent_png()))
                made += 1
            except Exception as e:  # noqa: BLE001
                log.warning("Couldn't add the spacer emoji: %s", e)
        if "are_you_sure" in have:
            SURE[0] = have["are_you_sure"]
        elif os.path.exists("are_you_sure.png"):
            try:
                with open("are_you_sure.png", "rb") as f:
                    SURE[0] = await client.create_application_emoji(name="are_you_sure", image=f.read())
                made += 1
            except Exception as e:  # noqa: BLE001
                log.warning("Couldn't add the are-you-sure emoji: %s", e)
        for ename, (table, key, url) in wanted.items():
            emoji = have.get(ename)
            if emoji is None:
                try:
                    async with s.get(url) as r:
                        r.raise_for_status()
                        image = await r.read()
                    emoji = await client.create_application_emoji(name=ename, image=image)
                    made += 1
                    await asyncio.sleep(0.5)  # gentle on Discord's rate limits
                except Exception as e:  # noqa: BLE001
                    log.warning("Couldn't add emoji %s: %s", ename, e)
                    continue
            table[key] = str(emoji)
        log.info("Valorant icons ready: %d ranks, %d agents, %d maps (%d newly uploaded)",
                 len(RANKS), len(AGENTS), len(MAPS), made)
