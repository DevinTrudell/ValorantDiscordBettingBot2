"""Valorant match data from the HenrikDev API (https://docs.henrikdev.xyz), a free unofficial service.

It runs on HenrikDev's servers: nobody's Riot account signs in to anything and nothing runs on anyone's PC.
Needs a free key in .env as HENRIK_API_KEY (get one from the HenrikDev Discord). Gives finished matches
with every player's name, team, party, agent, rank and combat score, so the bot can:
  - name your party members (exact party IDs from your recent games) as top-frag options,
  - settle top-frag and group bets from the real scoreboard right after the game,
  - give rank, season record and recent form for the odds (the bot's main data source; tracker.gg is a backup).
There's no live data: betting still opens from Discord status.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp
import certifi

from tracker import MatchDetail, MatchPlayer, TeamResult, TrackerError

log = logging.getLogger("valbet.henrik")
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
    # First bloods (first kill of each round), plants and defuses, per player
    name = lambda x: f"{(x or {}).get('name')}#{(x or {}).get('tag')}"
    first_kill: dict[int, dict] = {}
    for k in d.get("kills") or []:
        r = k.get("round")
        if r is not None and (r not in first_kill or k.get("time_in_round_in_ms", 0) < first_kill[r].get("time_in_round_in_ms", 0)):
            first_kill[r] = k
    fb, plants, defuses = {}, {}, {}
    for k in first_kill.values():
        fb[name(k.get("killer"))] = fb.get(name(k.get("killer")), 0) + 1
    for rd in d.get("rounds") or []:
        for key, table in (("plant", plants), ("defuse", defuses)):
            if (rd.get(key) or {}).get("player"):
                n = name(rd[key]["player"])
                table[n] = table.get(n, 0) + 1
    players = []
    for p in d.get("players") or []:
        st = p.get("stats") or {}
        score = float(st.get("score") or 0)
        rid = f"{p.get('name')}#{p.get('tag')}"
        perf = (p.get("performance") or {}).get("score")
        players.append(MatchPlayer(
            riot_id=f"{p.get('name')}#{p.get('tag')}", team=p.get("team_id") or "?",
            agent=(p.get("agent") or {}).get("name") or "?", kills=int(st.get("kills") or 0),
            deaths=int(st.get("deaths") or 0), assists=int(st.get("assists") or 0), score=score,
            acs=score / total_rounds if total_rounds else None, rounds=total_rounds,
            rank=(p.get("tier") or {}).get("name") or "Unranked", party=p.get("party_id"),
            perf=float(perf) if perf is not None else None, first_bloods=fb.get(rid, 0),
            plants=plants.get(rid, 0), defuses=defuses.get(rid, 0)))
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

    async def summary(self, riot_id: str, region: str | None = None) -> dict | None:
        """Their current Competitive rank plus this season's wins/games:
        {"rank": "Silver 1", "wins": 12, "games": 25}. Cached for an hour. None if unknown."""
        key = riot_id.casefold()
        if not hasattr(self, "_ranks"):
            self._ranks = {}
        hit = self._ranks.get(key)
        if hit and time.time() - hit[1] < 3600:
            return hit[0]
        name, _, tag = riot_id.partition("#")
        try:
            region = region or next(iter(self._regions.values()), None) or await self.region(riot_id)
            data = (await self._get(f"/valorant/v3/mmr/{region}/pc/{quote(name)}/{quote(tag)}")).get("data") or {}
        except HenrikError as e:
            log.info("No current rank for %s: %s", riot_id, e)
            if "rate limit" in str(e):
                raise  # let callers stop early instead of using up the minute
            self._ranks[key] = (None, time.time())
            return None
        # Recent record: the newest acts that have games (newest last in the list), until there are 20+ games.
        wins = games = 0
        for season in reversed(data.get("seasonal") or []):
            if games >= 20:
                break
            wins += int((season or {}).get("wins") or 0)
            games += int((season or {}).get("games") or 0)
        out = {"rank": ((data.get("current") or {}).get("tier") or {}).get("name"), "wins": wins, "games": games}
        self._ranks[key] = (out, time.time())
        return out

    async def current_rank(self, riot_id: str, region: str | None = None) -> str | None:
        """Their current Competitive rank ('Silver 1'), cached for an hour. None if unknown."""
        s = await self.summary(riot_id, region)
        return s["rank"] if s else None

    async def fill_ranks(self, detail: MatchDetail):
        """Unrated modes (Swiftplay, Unrated...) record everyone as 'Unrated': show their current
        Competitive rank instead."""
        missing = [p for p in detail.players if (p.rank or "").lower() in ("unrated", "unranked", "")]
        if not missing:
            return
        region = next(iter(self._regions.values()), None)  # everyone in a match shares a region
        ranks = []
        for i, p in enumerate(missing):  # one at a time, spaced out: kinder to the free key's rate limit
            try:
                ranks.append(await self.current_rank(p.riot_id, region))
            except HenrikError:  # rate limit reached: the rest just show unranked
                ranks += [None] * (len(missing) - len(ranks))
                break
            if i < len(missing) - 1:
                await asyncio.sleep(1.5)
        for p, r in zip(missing, ranks):
            if isinstance(r, str) and r and r.lower() not in ("unrated", "unranked"):
                p.rank = r

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
