"""Turn what the Overwolf app sends (Valorant live game data) into the bot's own types.

Overwolf docs: https://dev.overwolf.com/ow-native/live-game-data-gep/supported-games/valorant
Names, agents and maps arrive as Valorant's internal codes, so they're translated here.
"""

from __future__ import annotations

import json

from tracker import VALORANT_TIERS, MatchDetail, MatchPlayer, TeamResult, parse_riot_id

# Valorant's internal agent codes -> display names (unknown codes are shown as-is).
AGENTS = {
    "Wushu": "Jett", "Clay": "Raze", "Vampire": "Reyna", "Phoenix": "Phoenix", "Sprinter": "Neon", "Sequoia": "Iso",
    "Stealth": "Yoru", "Terra": "Waylay", "Hunter": "Sova", "BountyHunter": "Fade", "Guide": "Skye", "Breach": "Breach",
    "Grenadier": "KAY/O", "AggroBot": "Gekko", "Cashew": "Tejo", "Wraith": "Omen", "Sarge": "Brimstone",
    "Rift": "Astra", "Pandemic": "Viper", "Mage": "Harbor", "Smonk": "Clove", "Thorne": "Sage", "Killjoy": "Killjoy",
    "Gumshoe": "Cypher", "Deadeye": "Chamber", "Cable": "Deadlock", "Nox": "Vyse",
}
MAPS = {
    "Triad": "Haven", "Duality": "Bind", "Bonsai": "Split", "Ascent": "Ascent", "Port": "Icebox", "Foxtrot": "Breeze",
    "Canyon": "Fracture", "Pitt": "Pearl", "Jam": "Lotus", "Juliett": "Sunset", "Infinity": "Abyss", "Range": "the Range",
}


def riot_id(name: str | None) -> str | None:
    """Overwolf writes names as 'Name #TAG'. Returns 'Name#TAG', or None for hidden/unknown names."""
    return parse_riot_id((name or "").replace(" #", "#").replace("# ", "#"))


def rank_name(tier) -> str:
    """Valorant's competitive tier number (3 = Iron 1 ... 27 = Radiant, 0 = unranked) -> rank name."""
    try:
        t = int(tier)
    except (TypeError, ValueError):
        return "Unranked"
    return VALORANT_TIERS[t - 3] if 3 <= t <= 27 else "Unranked"


def agent_name(code: str | None) -> str:
    return AGENTS.get(code or "", code or "?")


HIDDEN_TAG = "AGENT"  # stands in for the tag of a teammate whose name is hidden: "Reyna#AGENT"


def hidden_id(agent: str) -> str:
    return f"{agent}#{HIDDEN_TAG}"


def is_hidden(riot: str | None) -> bool:
    return bool(riot) and riot.endswith("#" + HIDDEN_TAG)


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value or {}


def parse_mode(raw) -> tuple[str | None, bool]:
    """(tracker.gg mode to watch, should_skip). Customs and non-5v5 modes are skipped."""
    gm = _json(raw)
    mode = str(gm.get("mode", "")).lower()
    # "hurm" is believed to be Team Deathmatch's internal code (unconfirmed).
    if str(gm.get("custom")).lower() in ("true", "1") or any(w in mode for w in ("deathmatch", "ggteam", "oneforall",
                                                                                   "snowball", "range", "practice",
                                                                                   "hurm")):
        return None, True
    return ("competitive" if str(gm.get("ranked")).lower() in ("1", "true") else "any"), False


def lineup(payload: dict) -> tuple[str | None, list[str], list[str], dict[str, str]]:
    """(local player, teammates, opponents, {riot: rank}) from the roster. Hidden names are left out."""
    me = riot_id(payload.get("me"))
    mates, opps, ranks = [], [], {}
    for r in payload.get("roster") or []:
        rid = riot_id(r.get("name"))
        if r.get("local") and not me:
            me = rid
        if not rid:
            # A teammate with a hidden name: list them by their agent ("Reyna#AGENT") once it's picked
            locked = r.get("locked")
            if (r.get("teammate") and not r.get("local") and r.get("character")
                    and (locked is None or str(locked).lower() in ("true", "1") or payload.get("event") == "match_start")):
                mates.append(hidden_id(agent_name(r["character"])))
            continue
        ranks[rid] = rank_name(r.get("rank"))
        if r.get("local") or (me and rid.casefold() == me.casefold()):
            continue
        (mates if r.get("teammate") else opps).append(rid)
    return me, mates, opps, ranks


def final_detail(payload: dict, me: str, ranks: dict[str, str]) -> MatchDetail | None:
    """A finished game, built from the end-of-match scoreboard, in the same shape tracker.gg gives."""
    players = []
    for s in payload.get("scoreboard") or []:
        # Hidden names still count: shown by their agent ("Reyna"), so both teams have all 5
        rid = me if s.get("is_local") else (riot_id(s.get("name")) or hidden_id(agent_name(s.get("character"))))
        players.append(MatchPlayer(
            riot_id=rid, team="A" if s.get("teammate") or s.get("is_local") else "B",
            agent=agent_name(s.get("character")), kills=int(s.get("kills") or 0),
            deaths=int(s.get("deaths") or 0), assists=int(s.get("assists") or 0), score=0, acs=None,
            rank=ranks.get(rid, "Unranked")))
    if not any(p.riot_id.casefold() == me.casefold() for p in players):
        return None
    outcome = str(payload.get("outcome") or "").lower()
    if outcome not in ("victory", "defeat", "draw"):
        return None  # incomplete data: let the bot fall back to tracker.gg instead of refunding everyone
    won = True if outcome == "victory" else False if outcome == "defeat" else None  # draw = a genuine push
    score = _json(payload.get("score"))
    rw, rl = score.get("won"), score.get("lost")
    teams = {"A": TeamResult(won, int(rw) if rw is not None else None),
             "B": TeamResult(None if won is None else not won, int(rl) if rl is not None else None)}
    return MatchDetail(id=payload.get("match_id") or "overwolf", map=MAPS.get(payload.get("map") or "", payload.get("map") or "Unknown map"),
                       mode="", timestamp=None, players=players, teams=teams)
