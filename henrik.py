"""Valorant match data from the HenrikDev API (https://docs.henrikdev.xyz), a free unofficial service.

It runs on HenrikDev's servers: nobody's Riot account signs in to anything and nothing runs on anyone's PC.
Needs a free key in .env as HENRIK_API_KEY (get one from the HenrikDev Discord). Gives finished matches
with every player's name, team, party, agent, rank and combat score, so the bot can:
  - name your party members (exact party IDs from your recent games) as top-frag options,
  - settle top-frag and group bets from the real scoreboard right after the game,
  - use recent form for the odds while tracker.gg isn't available.
There's no live data: betting still opens from Discord status.
"""

from __future__ import annotations

import asyncio
import os
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp
import certifi

from tracker import MatchDetail, MatchPlayer, TeamResult, TrackerError

BASE = "https://api.henrikdev.xyz"
KEY = os.getenv("HENRIK_API_KEY", "").strip()


class HenrikError(TrackerError):
    pass


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_detail(d: dict) -> MatchDetail | None:
    """A HenrikDev v4 match -> the bot's MatchDetail (same shape tracker.gg matches use)."""
    meta = d.get("metadata") or {}
    if meta.get("is_completed") is False:
        return None
    teams = {}
    for t in d.get("teams") or []:
        r = t.get("rounds") or {}
        teams[t.get("team_id")] = TeamResult(won=t.get("won"), rounds_won=r.get("won"))
    total_rounds = sum((t.rounds_won or 0) for t in teams.values()) or len(d.get("rounds") or []) or None
    players = []
    for p in d.get("players") or []:
        st = p.get("stats") or {}
        score = float(st.get("score") or 0)
        players.append(MatchPlayer(
            riot_id=f"{p.get('name')}#{p.get('tag')}", team=p.get("team_id") or "?",
            agent=(p.get("agent") or {}).get("name") or "?", kills=int(st.get("kills") or 0),
            deaths=int(st.get("deaths") or 0), assists=int(st.get("assists") or 0), score=score,
            acs=score / total_rounds if total_rounds else None, rounds=total_rounds,
            rank=(p.get("tier") or {}).get("name") or "Unranked", party=p.get("party_id")))
    queue = meta.get("queue") or {}
    return MatchDetail(id=meta.get("match_id") or "?", map=(meta.get("map") or {}).get("name") or "Unknown map",
                       mode=queue.get("name") or queue.get("id") or "?", timestamp=_ts(meta.get("started_at")),
                       players=players, teams=teams)


def party_of(riot_id: str, details: list[MatchDetail], within_hours: float = 6) -> list[str]:
    """Who queued with this player in their most recent game (exact party ID), if that game was recent."""
    now = datetime.now(timezone.utc)
    for d in details:  # newest first
        me = d.find_player(riot_id)
        if not me or not me.party:
            continue
        if d.timestamp and (now - d.timestamp).total_seconds() > within_hours * 3600:
            return []  # their last game was a while ago: the party may have changed
        return [p.riot_id for p in d.players if p.party == me.party and p.riot_id.casefold() != riot_id.casefold()]
    return []


class HenrikClient:
    def __init__(self, key: str = KEY):
        self.key = key
        self._session: aiohttp.ClientSession | None = None
        self._regions: dict[str, str] = {}
        self._gate = asyncio.Semaphore(2)

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    async def _get(self, path: str) -> dict:
        if not self.enabled:
            raise HenrikError("HENRIK_API_KEY isn't set")
        if self._session is None or self._session.closed:
            ctx = ssl.create_default_context(cafile=certifi.where())
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx),
                                                  timeout=aiohttp.ClientTimeout(total=40),
                                                  headers={"Authorization": self.key})
        async with self._gate:
            try:
                async with self._session.get(BASE + path) as r:
                    body = await r.json(content_type=None)
                    if r.status == 401 or r.status == 403:
                        raise HenrikError("HenrikDev rejected the API key")
                    if r.status == 404:
                        raise HenrikError("player or match not found on HenrikDev")
                    if r.status == 429:
                        raise HenrikError("HenrikDev rate limit reached; try again in a minute")
                    if r.status >= 400:
                        raise HenrikError(f"HenrikDev error (HTTP {r.status})")
                    return body
            except (aiohttp.ClientError, TimeoutError, ValueError) as e:
                raise HenrikError(f"couldn't reach HenrikDev ({type(e).__name__})") from None

    async def region(self, riot_id: str) -> str:
        key = riot_id.casefold()
        if key not in self._regions:
            name, _, tag = riot_id.partition("#")
            data = (await self._get(f"/valorant/v2/account/{quote(name)}/{quote(tag)}")).get("data") or {}
            self._regions[key] = (data.get("region") or "na").lower()
        return self._regions[key]

    async def recent_details(self, riot_id: str, size: int = 5) -> list[MatchDetail]:
        """Their latest finished matches, newest first, with full scoreboards."""
        name, _, tag = riot_id.partition("#")
        region = await self.region(riot_id)
        data = await self._get(f"/valorant/v4/matches/{region}/pc/{quote(name)}/{quote(tag)}?size={size}")
        out = [to_detail(m) for m in data.get("data") or []]
        return [d for d in out if d]

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
