"""Overwatch 2 stats without Overwolf: a player's public Blizzard career profile, read through the OverFast API
(https://overfast-api.tekrop.fr, a free community service).

It gives wins/losses and win rate for Quick Play and Competitive. The bot uses that for the odds, and to tell
when a game finished and how it went: one more game played since betting opened, and whether it was a win or a
loss. Arcade and custom games aren't in the profile, so they can't be bet on. Profiles must be public
(Overwatch: Options → Social → Career Profile Visibility → Public) and OverFast caches each profile for about
10 minutes, so results arrive a few minutes after the game.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass

import aiohttp
import certifi

BASE = "https://overfast-api.tekrop.fr"
MODES = ("quickplay", "competitive")


class OverwatchError(Exception):
    pass


@dataclass
class Record:
    played: int
    won: int
    lost: int

    @property
    def win_pct(self) -> float:
        return self.won / self.played * 100 if self.played else 50.0


def total(records: dict[str, Record]) -> Record:
    return Record(sum(r.played for r in records.values()), sum(r.won for r in records.values()),
                  sum(r.lost for r in records.values()))


class OverwatchClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def _get(self, path: str):
        if self._session is None or self._session.closed:
            ctx = ssl.create_default_context(cafile=certifi.where())
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx),
                                                  timeout=aiohttp.ClientTimeout(total=40),
                                                  headers={"User-Agent": "valbet-discord-bot"})
        try:
            async with self._session.get(BASE + path) as r:
                body = await r.json(content_type=None)
                if r.status == 404:
                    raise OverwatchError("Overwatch profile not found or private (set Career Profile Visibility "
                                         "to Public in Overwatch, then finish a game)")
                if r.status >= 400:
                    raise OverwatchError(f"Overwatch stats service error (HTTP {r.status})")
                return body
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            raise OverwatchError(f"Couldn't reach the Overwatch stats service ({type(e).__name__})") from None

    async def find_player(self, battletag: str) -> str:
        """Blizzard's player ID for a BattleTag. Blizzard's search only knows the name part, so if several
        public players share the name, it's ambiguous."""
        name = battletag.split("#")[0]
        data = await self._get(f"/players?name={name}&limit=20")
        hits = [p for p in data.get("results", []) if (p.get("name") or "").casefold() == name.casefold()]
        public = [p for p in hits if p.get("is_public")]
        if not hits:
            raise OverwatchError(f"No Overwatch profile named {name}. Set Career Profile Visibility to Public in "
                                 "Overwatch, finish a game, then try again (it can take a while to show up).")
        if not public:
            raise OverwatchError(f"{name}'s Overwatch profile is private. Set Career Profile Visibility to Public.")
        if len(public) > 1:
            raise OverwatchError(f"Several public Overwatch players are named {name}, so the bot can't tell which "
                                 "one is you.")
        return public[0]["player_id"]

    async def records(self, player_id: str) -> dict[str, Record]:
        """Quick Play and Competitive totals for the current profile."""
        out = {}
        for mode in MODES:
            g = (await self._get(f"/players/{player_id}/stats/summary?gamemode={mode}") or {}).get("general") or {}
            out[mode] = Record(int(g.get("games_played") or 0), int(g.get("games_won") or 0),
                               int(g.get("games_lost") or 0))
        return out

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


def result_since(before: dict, after: dict[str, Record]) -> tuple[str, int] | None:
    """Compare a stored snapshot ({mode: [played, won, lost]}) with fresh records.
    None if no new game; otherwise ('win' | 'loss' | 'mixed' | 'draw', games finished since)."""
    new_games = new_wins = new_losses = 0
    for mode, rec in after.items():
        p, w, l = before.get(mode, [rec.played, rec.won, rec.lost])
        new_games += max(rec.played - p, 0)
        new_wins += max(rec.won - w, 0)
        new_losses += max(rec.lost - l, 0)
    if not new_games:
        return None
    if new_wins == new_games:
        return "win", new_games
    if new_losses == new_games:
        return "loss", new_games
    if new_wins == 0 and new_losses == 0:
        return "draw", new_games
    return "mixed", new_games


def snapshot(records: dict[str, Record]) -> dict:
    return {m: [r.played, r.won, r.lost] for m, r in records.items()}
