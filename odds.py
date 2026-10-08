"""Betting markets: Win/Loss and top frag on the host's team. Pricing before a game, settling after."""

from __future__ import annotations

import math
import os
import random
from dataclasses import replace

from tracker import MatchDetail, MatchPlayer, PlayerStats, tier_index

HOUSE_EDGE = 0.05
FORM_GAMES = 10       # how many recent games make up "recent form"
FORM_PRIOR = 5        # recent form outweighs season stats once there are more than this many games
OTHER = "other"  # top-frag option covering teammates nobody listed
TEAM_SIZE = 5

WR_PRIOR = 10         # a win rate counts as if it also had this many 50% games (few games -> close to 50%)
MAP_PRIOR = 8         # same for the win rate on one map, pulled towards the player's overall win rate
MAP_MAX = 0.06        # the map can move the win chance by at most ±6%
MODE_ROUNDS = {"swiftplay": 5, "spikerush": 4}  # rounds needed to win; everything else plays to 13
CAL_MIN_GAMES = 30    # self-tuning starts once this many predicted games have a result
# Tuned on 230 of the group's past games and 70 scoreboards (replayed, each predicted from earlier games only):
# win rate, streak and map record didn't predict results at all (matchmaking evens them out), the group as a
# whole wins ~46% (pulled a little towards 50%), and top scores swing more, with randoms scoring above 200.
GROUP_WIN_RATE = float(os.getenv("GROUP_WIN_RATE", "0.47"))  # the starting win chance for any game
OPP_MAX = 0.08        # known opponents' ranks / K/D can move the win chance by at most ±8%
RANDOM_ACS = 213.0    # average ACS of the random teammates Valorant adds (measured in the group's games)
ACS_SD = 0.50         # typical game-to-game spread of a player's ACS, as a share of their average
SD_PRIOR = 4          # a measured ACS spread counts as if it also had this many "typical" games
SIMS = 20000          # top-frag simulations
CROWD_WEIGHT = float(os.getenv("CROWD_WEIGHT", "4000"))  # coins of betting that move the odds halfway
CROWD_MAX = 0.05      # bets can move a chance by at most ±5% from the model


def price(p: float) -> float:
    """Decimal odds for an outcome with probability p, minus the house edge."""
    p = min(max(p, 0.04), 0.96)
    return round(max(1.05, (1 - HOUSE_EDGE) / p), 2)


def _avg(values):
    values = [v for v in values if v]
    return sum(values) / len(values) if values else None


def recent_form(riot: str, details: list[MatchDetail]) -> dict | None:
    """How one player has done in the given finished games (newest first): record, streak, K/D, ACS,
    and the average rank of their teammates and opponents in those lobbies."""
    games = []
    for d in details:
        me = d.find_player(riot)
        if not me:
            continue
        team = d.teams.get(me.team)
        games.append({
            "won": team.won if team else None, "kills": me.kills, "deaths": me.deaths, "acs": me.acs, "rank": me.rank,
            "map": d.map, "team_tier": _avg(tier_index(p.rank) for p in d.players if p.team == me.team),
            "opp_tier": _avg(tier_index(p.rank) for p in d.players if p.team != me.team),
        })
    if not games:
        return None
    decided = [g["won"] for g in games if g["won"] is not None]
    acs = [g["acs"] for g in games if g["acs"]]
    mean = _avg(acs)
    return {
        **_record(decided), "games": len(games),
        "kd": sum(g["kills"] for g in games) / max(sum(g["deaths"] for g in games), 1),
        "acs": mean, "kills": sum(g["kills"] for g in games) / len(games),
        "acs_sd": math.sqrt(sum((a - mean) ** 2 for a in acs) / (len(acs) - 1)) if len(acs) >= 3 else None,
        "acs_n": len(acs),
        "team_tier": _avg(g["team_tier"] for g in games), "opp_tier": _avg(g["opp_tier"] for g in games),
        "rank": next((g["rank"] for g in games if tier_index(g["rank"])), None),  # latest known rank
        "maps": _map_records((g["map"], g["won"]) for g in games),
        "source": "scoreboards",
    }


def _record(decided: list[bool]) -> dict:
    """Wins, losses, win % and current streak from results listed newest first."""
    wins = sum(decided)
    streak = 0
    for w in decided:  # newest first: count how many in a row match the latest result
        if streak == 0 or (w and streak > 0) or (not w and streak < 0):
            streak += 1 if w else -1
        else:
            break
    return {"wins": wins, "losses": len(decided) - wins,
            "win_pct": wins / len(decided) * 100 if decided else 50.0, "streak": streak}


def _map_records(pairs) -> dict:
    """{map: [wins, games]} from (map, won) pairs."""
    out: dict[str, list[int]] = {}
    for name, won in pairs:
        if name and won is not None:
            rec = out.setdefault(name, [0, 0])
            rec[0] += bool(won)
            rec[1] += 1
    return out


def history_form(games: list[dict]) -> dict | None:
    """Recent form from the games the bot itself watched through Discord status (newest first), for
    when no scoreboards load: record, streak and per-map record, but no K/D or ACS."""
    games = [g for g in games if g["outcome"] in ("win", "loss")][:FORM_GAMES * 2]
    if not games:
        return None
    decided = [g["outcome"] == "win" for g in games]
    return {**_record(decided), "games": len(games), "kd": None, "acs": None, "kills": None, "acs_sd": None,
            "acs_n": 0, "team_tier": None, "opp_tier": None, "rank": None,
            "maps": _map_records((g["map"], g["outcome"] == "win") for g in games), "source": "bot history"}


def blend(stats: PlayerStats, form: dict | None) -> PlayerStats:
    """Mix season stats with recent form; the more recent games, the more they count."""
    if not form:
        return stats
    n = form["games"]
    w = 1.0 if stats.matches == 0 else n / (n + FORM_PRIOR)  # no season stats loaded: use form only
    mix = lambda season, recent: season if recent is None else season * (1 - w) + recent * w
    rank = stats.rank if stats.tier or not form.get("rank") else form["rank"]
    return replace(stats, win_pct=mix(stats.win_pct, form["win_pct"]), kd=mix(stats.kd, form["kd"]),
                   acs=mix(stats.acs, form["acs"]), kills_per_match=mix(stats.kills_per_match, form["kills"]),
                   matches=max(stats.matches, n), rank=rank, tier=tier_index(rank))


def shrunk_win_pct(win_pct: float, games: int, toward: float = 50.0, prior: int = WR_PRIOR) -> float:
    """A win rate pulled towards `toward` the fewer games it comes from (3-0 isn't really 100%)."""
    return (win_pct * games + toward * prior) / (games + prior)


def map_edge(map_record: list[int] | None, overall_pct: float) -> float:
    """How much better (or worse) than usual the player does on this map, as a change in win chance."""
    if not map_record or not map_record[1]:
        return 0.0
    wins, games = map_record
    on_map = shrunk_win_pct(wins / games * 100, games, overall_pct, MAP_PRIOR)
    return min(max((on_map - overall_pct) / 100 * 0.5, -MAP_MAX), MAP_MAX)


def win_probability(host: PlayerStats, team: list[PlayerStats], opps: list[PlayerStats],
                    form: dict | None = None, map_record: list[int] | None = None) -> float:
    """Chance the host's team wins a full game (first to 13), before the group's own win rate is applied.
    Only the opponents (when the Overwolf app names them) move it: replaying the group's past games showed the
    player's win rate, streak and map record predicted nothing, so `form` and `map_record` are no longer used."""
    p = 0.5
    if opps:
        edge = 0.0
        t_tier, o_tier = _avg(s.tier for s in team), _avg(s.tier for s in opps)
        if t_tier and o_tier:
            edge += (t_tier - o_tier) * 0.035
        edge += ((_avg(s.kd for s in team) or 1.0) - (_avg(s.kd for s in opps) or 1.0)) * 0.25
        p += min(max(edge, -OPP_MAX), OPP_MAX)
    return p


def first_to(q: float, n: int) -> float:
    """Chance of winning n rounds before the other team does, winning each round with chance q
    (overtime ignored)."""
    return sum(math.comb(n - 1 + k, k) * q ** n * (1 - q) ** k for k in range(n))


def for_mode(p: float, mode: str | None) -> float:
    """Turn a full-game (first to 13) win chance into one for this mode. Short modes like Swiftplay
    (first to 5) leave more to luck, so the same edge gives a chance closer to 50%."""
    n = MODE_ROUNDS.get((mode or "").replace(" ", "").lower())
    if not n:
        return p
    lo, hi = 0.0, 1.0  # find the per-round chance that gives p over 13 rounds
    for _ in range(50):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if first_to(mid, 13) < p else (lo, mid)
    return first_to((lo + hi) / 2, n)


def fit_calibration(pairs: list[tuple[float, bool]]) -> dict | None:
    """Learn from finished games how far off the predictions were: fits p' = sigmoid(a·logit(p) + b)
    by logistic regression, held near a=1, b=0 (no change) until there's plenty of data."""
    pairs = [(p, y) for p, y in pairs if 0 < p < 1]
    if len(pairs) < CAL_MIN_GAMES:
        return None
    a, b, ridge = 1.0, 0.0, 5.0
    xs = [(math.log(p / (1 - p)), 1.0 if y else 0.0) for p, y in pairs]
    for _ in range(25):  # Newton steps on the penalised log-likelihood
        ga = gb = haa = hab = hbb = 0.0
        for x, y in xs:
            s = 1 / (1 + math.exp(-(a * x + b)))
            ga += (s - y) * x
            gb += s - y
            w = s * (1 - s)
            haa, hab, hbb = haa + w * x * x, hab + w * x, hbb + w
        ga, gb = ga + ridge * (a - 1), gb + ridge * b
        haa, hbb = haa + ridge, hbb + ridge
        det = haa * hbb - hab * hab
        if det <= 0:
            break
        a -= (hbb * ga - hab * gb) / det
        b -= (haa * gb - hab * ga) / det
    return {"a": round(min(max(a, 0.3), 2.0), 3), "b": round(min(max(b, -0.6), 0.6), 3), "games": len(pairs)}


def calibrate(p: float, cal: dict | None) -> float:
    if not cal:
        return p
    p = min(max(p, 0.01), 0.99)
    return 1 / (1 + math.exp(-(cal["a"] * math.log(p / (1 - p)) + cal["b"])))


def acs_spread(mean: float, form: dict | None) -> float:
    """How much this player's ACS swings from game to game: measured from recent games when there are
    some, pulled towards the typical spread."""
    typical = ACS_SD * mean
    if not form or not form.get("acs_sd"):
        return typical
    n = form.get("acs_n") or 0
    return math.sqrt((n * form["acs_sd"] ** 2 + SD_PRIOR * typical ** 2) / (n + SD_PRIOR))


def topfrag_chances(players: list[tuple[float, float]], unknown: int, seed: int = 7) -> list[float]:
    """Chance each player (ACS average, spread) has the team's highest ACS, by simulating the game many
    times. The last entry is the chance it's one of the `unknown` average teammates."""
    rng = random.Random(seed)  # fixed seed: the same lineup always gets the same odds
    field = players + [(RANDOM_ACS, ACS_SD * RANDOM_ACS)] * unknown
    wins = [0] * len(field)
    for _ in range(SIMS):
        draws = [rng.gauss(m, s) for m, s in field]
        wins[draws.index(max(draws))] += 1
    known = [w / SIMS for w in wins[:len(players)]]
    return known + ([sum(wins[len(players):]) / SIMS] if unknown else [])


def build_markets(host: PlayerStats, team: list[PlayerStats], opps: list[PlayerStats],
                  form: dict | None = None, *, mode: str | None = None, map_record: list[int] | None = None,
                  cal: dict | None = None, forms: dict[str, dict] | None = None) -> dict:
    """team = host first, then any teammates given. Unlisted teammates share one 'someone else' option.
    Pass stats already blended with recent form, plus the host's form for streak/lobby adjustments,
    the mode (short modes are closer to 50/50), their record on this map, the self-tuning fit, and each
    player's recent form by Riot ID (how much their ACS swings)."""
    p_raw = for_mode(win_probability(host, team, opps, form, map_record), mode) + (GROUP_WIN_RATE - 0.5)
    p_raw = min(max(p_raw, 0.15), 0.85)
    p_win = calibrate(p_raw, cal)
    known, seen = [], set()
    forms = {k.casefold(): v for k, v in (forms or {}).items()}
    for p in team:
        if p.riot_id.casefold() in seen:
            continue
        seen.add(p.riot_id.casefold())
        mean = max(p.acs or 200.0, 50.0)
        known.append((p.riot_id, (mean, acs_spread(mean, forms.get(p.riot_id.casefold())))))
    known = known[:TEAM_SIZE]
    unknown = TEAM_SIZE - len(known)
    chances = topfrag_chances([ms for _, ms in known], unknown)
    names = [r for r, _ in known] + ([OTHER] if unknown else [])
    options = [{"riot": r, "p": c, "p_model": c, "odds": price(c)} for r, c in zip(names, chances)]
    if unknown:
        options[-1]["count"] = unknown  # how many randoms Valorant adds to fill the team
    return {"win": {"p": p_win, "p_model": p_win, "p_raw": p_raw, "win": price(p_win), "loss": price(1 - p_win)},
            "topfrag": options}


def _pull(model: float, share: float, staked: float) -> float:
    """Move a model chance towards the share of money bet on it, by at most CROWD_MAX."""
    p = (model * CROWD_WEIGHT + share * staked) / (CROWD_WEIGHT + staked)
    return min(max(p, model - CROWD_MAX), model + CROWD_MAX)


def reprice(markets: dict, bets: list[dict]) -> dict:
    """Odds for the next bets, nudged by what everyone has bet so far (like a sportsbook: if most of the
    money is on Win, Win pays a bit less and Loss a bit more). Bets already placed keep their odds.
    `bets` should leave out the host's own bets (they can only ever bet on themselves)."""
    out = {**markets, "win": dict(markets["win"]), "topfrag": [dict(o) for o in markets["topfrag"]]}
    win = out["win"]
    model = win.get("p_model", win["p"])
    on = {"win": 0, "loss": 0}
    for b in bets:
        if b["market"] == "win" and b["side"] in on:
            on[b["side"]] += b["amount"]
    staked = on["win"] + on["loss"]
    win["p"] = _pull(model, on["win"] / staked, staked) if staked else model  # no bets (left): model odds
    win["win"], win["loss"] = price(win["p"]), price(1 - win["p"])
    tf_on = {o["riot"]: 0 for o in out["topfrag"]}
    for b in bets:
        if b["market"] == "topfrag" and b["side"] in tf_on:
            tf_on[b["side"]] += b["amount"]
    tf_staked = sum(tf_on.values())
    if not tf_staked:
        for o in out["topfrag"]:
            o["p"] = o.get("p_model", o["p"])
            o["odds"] = price(o["p"])
    elif out["topfrag"]:
        pulled = [_pull(o.get("p_model", o["p"]), tf_on[o["riot"]] / tf_staked, tf_staked) for o in out["topfrag"]]
        total = sum(pulled) or 1
        for o, p in zip(out["topfrag"], pulled):
            o["p"] = p / total
            o["odds"] = price(o["p"])
    return out


def option_name(riot: str, markets: dict | None = None) -> str:
    """'Sam', or for the randoms Valorant adds to fill the team: 'Other teammate' / 'Other teammates'."""
    if riot != OTHER:
        return riot.split("#")[0]
    n = next((o.get("count") for o in (markets or {}).get("topfrag", []) if o["riot"] == OTHER), None)
    return "Other teammate" if n == 1 else "Other teammates"


def option_label(o: dict, markets: dict | None = None) -> str:
    """Name plus their usual agent when known: 'Sam (Jett main)'."""
    name = option_name(o["riot"], markets)
    return f"{name} ({o['agent']} main)" if o.get("agent") and o["riot"] != OTHER else name


def combo_side(result: str, riot: str) -> str:
    """A group (combo) bet: a match result AND a top fragger, both must be right. Stored as 'win|Name#TAG'."""
    return f"{result}|{riot}"


def split_combo(side: str) -> tuple[str, str]:
    result, _, riot = side.partition("|")
    return result, riot


def combo_price(markets: dict, side: str) -> float | None:
    """Odds for a combo: the result's odds times the top fragger's odds."""
    result, riot = split_combo(side)
    tf = next((o["odds"] for o in markets.get("topfrag", []) if o["riot"] == riot), None)
    if result not in ("win", "loss") or tf is None:
        return None
    return round(markets["win"][result] * tf, 2)


def bet_label(markets: dict, host_riot: str, market: str, side: str) -> str:
    if market == "win":
        return "Win" if side == "win" else "Loss"
    if market == "combo":
        result, riot = split_combo(side)
        return f"{'Win' if result == 'win' else 'Loss'} + {option_name(riot, markets)} team top frag"
    return f"{option_name(side, markets)} team top frag"


def judge(results: dict, market: str, side: str) -> str | None:
    """'won', 'lost' or 'push' for one bet, given settle() results; None = can't tell yet (leave it pending)."""
    if market == "win":
        return "push" if results["win"] == "push" else ("won" if side == results["win"] else "lost")
    if market == "topfrag":
        if results.get("topfrag") in (None, "push"):  # undecided (no ACS) or exact tie: never a loss
            return "push"
        if side in results.get("absent", ()):  # that player wasn't in the game: refund
            return "push"
        return "won" if side == results["topfrag"] else "lost"
    if market == "combo":
        result, riot = split_combo(side)
        if results["win"] == "push":
            return "push"
        if result != results["win"]:
            return "lost"  # the result part is wrong: lost, no need to wait for the top frag
        if "topfrag" not in results:
            return None    # result right, top frag not known yet: wait for the scoreboard
        if results["topfrag"] in (None, "push") or riot in results.get("absent", ()):
            return "push"  # top frag tied, or the picked player wasn't in the game: refund
        return "won" if riot == results["topfrag"] else "lost"
    return "push"


def has_acs(detail: MatchDetail, host: MatchPlayer) -> bool:
    """Top frag needs a score: Valorant's performance score, or combat score (ACS). Overwolf's scoreboard has neither."""
    return any(p.score or p.perf for p in detail.players if p.team == host.team)


def compute_outcome(detail: MatchDetail, host: MatchPlayer) -> dict:
    """Result, plus the top frag: the highest ACS on the host's team (same rounds for everyone, so total
    combat score ranks the same as ACS). Equal ACS -> more kills wins; equal ACS and kills -> a tie
    (top frag bets refunded). No ACS in the scoreboard -> top frag left undecided."""
    team_result = detail.teams.get(host.team)
    team = [p for p in detail.players if p.team == host.team] or [host]
    out = {"won": team_result.won if team_result else None, "team": [p.riot_id for p in team],
           "topfrag_riot": None, "topfrag_acs": None, "topfrag_kills": None, "topfrag_tied": []}
    if has_acs(detail, host):
        # Valorant's own Performance Score (what the end screen shows) when everyone has one; otherwise ACS.
        use_perf = all(p.perf is not None for p in team)
        key = (lambda p: (round(p.perf, 2), p.kills)) if use_perf else (lambda p: (p.score, p.kills))
        top = max(team, key=key)
        value = (round(top.perf) if use_perf else round(top.acs) if top.acs else None)
        out.update(topfrag_riot=top.riot_id, topfrag_acs=value, topfrag_kills=top.kills,
                   topfrag_metric="performance score" if use_perf else "ACS",
                   topfrag_tied=[p.riot_id for p in team if key(p) == key(top)])
    return out


def settle(markets: dict, o: dict) -> dict:
    """{'win': 'win'|'loss'|'push', 'topfrag': <winning option's riot>, OTHER or 'push' (tie across options),
    plus who was on the team}."""
    win = "push" if o["won"] is None else ("win" if o["won"] else "loss")
    tf = None  # no ACS yet: top frag stays undecided
    if o.get("topfrag_riot"):
        tied = o.get("topfrag_tied") or [o["topfrag_riot"]]
        named = [opt["riot"] for opt in markets["topfrag"]]
        opts = {next((r for r in named if r.casefold() == (t or "").casefold()), OTHER) for t in tied}
        tf = opts.pop() if len(opts) == 1 else "push"
    team = [r.casefold() for r in o.get("team", [])]
    # Named players who weren't actually in this game (e.g. left the party after the last game): bets on
    # them are refunded, since they couldn't top frag. Only judged from a full team list.
    absent = [opt["riot"] for opt in markets["topfrag"]
              if opt["riot"] != OTHER and len(team) >= 2 and opt["riot"].casefold() not in team]
    return {"win": win, "topfrag": tf, "topfrag_riot": o["topfrag_riot"], "team": team, "absent": absent}
