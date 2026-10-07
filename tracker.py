"""Thin async client for the tracker.gg Valorant API.

Docs: https://tracker.gg/developers  (header: TRN-Api-Key)
The response shapes below are parsed defensively because tracker.gg does not
publish a strict schema. Run `python check_tracker.py Name#TAG` to dump raw JSON
if a field ever comes back empty.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import aiohttp
from yarl import URL

BASE_URL = "https://public-api.tracker.gg/v2/valorant/standard"

VALORANT_TIERS = [
    f"{rank} {n}"
    for rank in ("Iron", "Bronze", "Silver", "Gold", "Platinum", "Diamond", "Ascendant", "Immortal")
    for n in (1, 2, 3)
] + ["Radiant"]


class TrackerError(Exception):
    pass


class PlayerNotFound(TrackerError):
    pass


class PrivateProfile(TrackerError):
    pass


def tier_index(name: str | None) -> int:
    """Iron 1 -> 1 ... Radiant -> 25, unknown/unranked -> 0."""
    if not name:
        return 0
    try:
        return VALORANT_TIERS.index(name.strip().title()) + 1
    except ValueError:
        return 0


def parse_riot_id(text: str) -> str | None:
    """Normalise 'Name #TAG' -> 'Name#TAG'; None if it isn't a Riot ID."""
    if not text or "#" not in text:
        return None
    name, _, tag = text.strip().rpartition("#")
    name, tag = name.strip(), tag.strip()
    if not (1 <= len(name) <= 16 and 2 <= len(tag) <= 5):
        return None
    if any(c in name for c in "@<>#`"):
        return None
    return f"{name}#{tag}"


def _stat(stats: dict, key: str, default=None):
    s = stats.get(key) or {}
    v = s.get("value")
    return default if v is None else v


def _rank_name(stats: dict) -> str | None:
    s = stats.get("rank") or {}
    return (s.get("metadata") or {}).get("tierName") or s.get("displayValue")


def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@dataclass
class PlayerStats:
    riot_id: str
    rank: str = "Unranked"
    tier: int = 0
    matches: int = 0
    win_pct: float = 50.0
    kd: float = 1.0
    kills_per_match: float = 15.0
    acs: float = 200.0
    hs_pct: float = 0.0
    top_agents: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PlayerStats":
        return cls(**d)


@dataclass
class MatchSummary:
    id: str
    timestamp: datetime | None
    map: str | None
    mode: str | None


@dataclass
class MatchPlayer:
    riot_id: str
    team: str
    agent: str = "?"
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    score: float = 0
    acs: float | None = None
    rounds: int | None = None
    rank: str = "Unranked"
    party: str | None = None  # players who queued together share this (HenrikDev data only)


@dataclass
class TeamResult:
    won: bool | None
    rounds_won: int | None


@dataclass
class MatchDetail:
    id: str
    map: str
    mode: str
    timestamp: datetime | None
    players: list[MatchPlayer] = field(default_factory=list)
    teams: dict[str, TeamResult] = field(default_factory=dict)

    @property
    def total_rounds(self) -> int | None:
        rounds = [t.rounds_won for t in self.teams.values() if t.rounds_won is not None]
        if len(rounds) >= 2:
            return sum(rounds)
        played = [p.rounds for p in self.players if p.rounds]
        return max(played) if played else None

    def find_player(self, riot_id: str) -> MatchPlayer | None:
        want = riot_id.casefold()
        return next((p for p in self.players if p.riot_id.casefold() == want), None)


class TrackerClient:
    def __init__(self, api_key: str, profile_ttl: int = 600):
        self._key = api_key
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, dict]] = {}
        self._profile_ttl = profile_ttl
        self._gate = asyncio.Semaphore(2)  # stay well under tracker.gg's rate limit

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict | None = None, ttl: int = 0) -> dict:
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        cached = self._cache.get(url)
        if ttl and cached and cached[0] > time.monotonic():
            return cached[1]

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "TRN-Api-Key": self._key,
                    "Accept": "application/json",
                    "User-Agent": "valorant-betting-bot/1.0",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            )

        net_error: Exception | None = None
        for attempt in range(3):
            try:
                async with self._gate:
                    async with self._session.get(URL(url, encoded=True)) as r:
                        status = r.status
                        retry_after = r.headers.get("Retry-After")
                        body = await r.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                net_error = e
                await asyncio.sleep(3 * (attempt + 1))
                continue
            net_error = None
            if status == 200:
                try:
                    parsed = await asyncio.to_thread(_json_loads, body)
                except ValueError:
                    raise TrackerError("tracker.gg sent an unreadable reply.") from None
                if not isinstance(parsed, dict):
                    raise TrackerError("tracker.gg sent an unreadable reply.")
                data = parsed.get("data")
                if not isinstance(data, dict):
                    data = {}
                if ttl:
                    self._cache[url] = (time.monotonic() + ttl, data)
                return data
            if status == 404:
                raise PlayerNotFound("Player or match not found on tracker.gg.")
            if status == 451:
                raise PrivateProfile(
                    "That profile is private. The player must sign in at tracker.gg once to make it public."
                )
            if status in (401, 403):
                raise TrackerError(f"tracker.gg rejected the API key (HTTP {status}). Check TRACKER_API_KEY.")
            if status == 429 or status >= 500:
                try:
                    delay = float(retry_after)
                except (ValueError, TypeError):
                    delay = 3 * (attempt + 1)
                await asyncio.sleep(min(delay, 30))
                continue
            raise TrackerError(f"tracker.gg HTTP {status}: {body[:200]}")
        if net_error is not None:
            raise TrackerError(f"Couldn't reach tracker.gg ({net_error})")
        raise TrackerError("tracker.gg is rate limiting or unavailable; try again shortly.")

    async def raw(self, path: str, params: dict | None = None) -> dict:
        return await self._get(path, params)

    async def get_profile(self, riot_id: str) -> PlayerStats:
        data = await self._get(f"/profile/riot/{_q(riot_id)}", ttl=self._profile_ttl)
        segments = data.get("segments") or []
        seg = (
            next((s for s in segments if s.get("type") == "playlist"
                  and (s.get("attributes") or {}).get("key") == "competitive"), None)
            or next((s for s in segments if s.get("type") == "season"), None)
            or next((s for s in segments if s.get("type") == "overview"), None)
            or (segments[0] if segments else {})
        )
        stats = seg.get("stats") or {}
        rank = _rank_name(stats)
        if not rank:
            # Rank sometimes lives on a different segment than the playlist stats.
            rank = next((_rank_name(s.get("stats") or {}) for s in segments if _rank_name(s.get("stats") or {})), None)

        matches = int(_stat(stats, "matchesPlayed", 0) or 0)
        p = PlayerStats(riot_id=riot_id, rank=rank or "Unranked", tier=tier_index(rank), matches=matches)
        if matches:
            p.win_pct = float(_stat(stats, "matchesWinPct", 50.0))
            p.kd = float(_stat(stats, "kDRatio", 1.0))
            kills = _stat(stats, "kills")
            p.kills_per_match = float(_stat(stats, "killsPerMatch", (kills / matches) if kills else 15.0))
            p.acs = float(_stat(stats, "scorePerRound", 200.0))
            p.hs_pct = float(_stat(stats, "headshotsPercentage", 0.0))
        agents = [s for s in segments if s.get("type") == "agent"]
        agents.sort(key=lambda s: -float(_stat(s.get("stats") or {}, "matchesPlayed", 0)
                                        or _stat(s.get("stats") or {}, "timePlayed", 0) or 0))
        p.top_agents = [(s.get("metadata") or {}).get("name") for s in agents[:3]
                        if (s.get("metadata") or {}).get("name")]
        return p

    async def get_recent_matches(self, riot_id: str, mode: str = "competitive") -> list[MatchSummary]:
        params = {} if mode == "any" else {"type": mode}
        data = await self._get(f"/matches/riot/{_q(riot_id)}", params)  # never cached: used for polling
        out = []
        for m in data.get("matches") or []:
            attrs, meta = m.get("attributes") or {}, m.get("metadata") or {}
            if attrs.get("id"):
                out.append(MatchSummary(attrs["id"], _parse_time(meta.get("timestamp")),
                                        meta.get("mapName"), meta.get("modeName")))
        return out

    async def get_match(self, match_id: str) -> MatchDetail:
        data = await self._get(f"/matches/{_q(match_id)}", ttl=3600)
        meta = data.get("metadata") or {}
        detail = MatchDetail(
            id=match_id,
            map=meta.get("mapName") or "Unknown map",
            mode=meta.get("modeName") or "",
            timestamp=_parse_time(meta.get("timestamp")),
        )
        for seg in data.get("segments") or []:
            attrs, smeta, stats = seg.get("attributes") or {}, seg.get("metadata") or {}, seg.get("stats") or {}
            if seg.get("type") == "team-summary":
                team_id = str(attrs.get("teamId") or smeta.get("name") or len(detail.teams))
                rw = _stat(stats, "roundsWon")
                detail.teams[team_id] = TeamResult(smeta.get("hasWon"), int(rw) if rw is not None else None)
            elif seg.get("type") == "player-summary":
                riot = attrs.get("platformUserIdentifier") or (smeta.get("platformInfo") or {}).get("platformUserHandle")
                if not riot:
                    continue
                score = float(_stat(stats, "score", 0) or 0)
                rounds = _stat(stats, "roundsPlayed")
                acs = _stat(stats, "scorePerRound")
                detail.players.append(MatchPlayer(
                    riot_id=riot,
                    team=str(smeta.get("teamId") or attrs.get("teamId") or "?"),
                    agent=smeta.get("agentName") or "?",
                    kills=int(_stat(stats, "kills", 0) or 0),
                    deaths=int(_stat(stats, "deaths", 0) or 0),
                    assists=int(_stat(stats, "assists", 0) or 0),
                    score=score,
                    acs=float(acs) if acs is not None else None,
                    rounds=int(rounds) if rounds else None,
                    rank=_rank_name(stats) or "Unranked",
                ))
        # Fill in missing win flags from round counts.
        if len(detail.teams) == 2:
            (a_id, a), (b_id, b) = detail.teams.items()
            if a.won is None and a.rounds_won is not None and b.rounds_won is not None:
                a.won = a.rounds_won > b.rounds_won if a.rounds_won != b.rounds_won else None
                b.won = b.rounds_won > a.rounds_won if a.rounds_won != b.rounds_won else None
        total = detail.total_rounds
        for p in detail.players:
            if p.acs is None and total:
                p.acs = p.score / total
        return detail


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _json_loads(body: str) -> dict:
    import json
    return json.loads(body)
