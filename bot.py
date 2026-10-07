"""Valorant betting bot: bet play-money coins on a friend's upcoming Valorant game.

Flow:
  1. /link Name#TAG            link your Discord account to your Riot ID
  2. /panel                    posts the info message and sets the betting channel (nothing is manual)
  3. betting opens by itself   when a linked player's Discord status (or the Overwolf app) shows a match
                               starting; bets are Win/Loss and top frag on the player's team
  4. the "Bets open" post      Win / Loss / top-frag menu / Group bet right on it; each opens a private
                               slip with that pick filled in; anyone in the server can bet
  5. the bot polls tracker.gg; when the game shows up it settles every bet and
     posts the full scoreboard, including the opponents' ranks and stats.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # before importing db, which reads env vars

import asyncio
import json
import re
from collections import Counter
import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks

import dashboard
import db
import henrik
import odds
import overwatch as ow
import ui
import overwolf_data
from tracker import (VALORANT_TIERS, MatchDetail, PlayerNotFound, PlayerStats, TrackerClient, TrackerError,
                     parse_riot_id, tier_index)

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
TRACKER_API_KEY = os.environ["TRACKER_API_KEY"]
GUILD_ID = os.getenv("GUILD_ID")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
MATCH_TIMEOUT = timedelta(hours=float(os.getenv("MATCH_TIMEOUT_HOURS", "3")))
MIN_GAME_TIME = timedelta(minutes=8)  # no Valorant game finishes faster; skip polling until then
MIN_BET = int(os.getenv("MIN_BET", "10"))
PRESENCE_TRACKING = os.getenv("PRESENCE_TRACKING", "1") == "1"
PRESENCE_LOG = "presence_log.jsonl"
AUTO_OPEN = os.getenv("AUTO_OPEN", "1") == "1"            # open betting when Discord shows a match starting
# Seconds betting stays open after an automatic open. Round 1 (buy phase + fight) takes ~1.5–2.5 min,
# so 75 s closes it before the first round can end; a round result in the status closes it even sooner.
AUTO_WINDOW = int(os.getenv("AUTO_OPEN_WINDOW_SECONDS", "75")) / 60   # in minutes, as open_match expects
SCORE_RE = re.compile(r"\b(\d{1,2})\s*[-–:]\s*(\d{1,2})\b")
AUTO_COOLDOWN = timedelta(minutes=25)                     # one auto-open per player per game
# Overwatch without Overwolf: Discord only shows "Playing Overwatch", so betting opens when the game is launched
# (and again after each result while they keep playing), on their next Quick Play / Competitive game.
OW_WINDOW = int(os.getenv("OVERWATCH_WINDOW_SECONDS", "60")) / 60
OW_POLL = timedelta(minutes=2)  # how often to check their career profile for a finished game

# What Valorant's Discord status says in each phase. Exact wording isn't documented, so these are
# deliberately broad; presence_log.jsonl records the real text so they can be tightened later.
PREGAME_RE = re.compile(r"agent select|pre-?game|character select|selecting agent|locking in", re.I)
INGAME_RE = re.compile(r"\b\d{1,2}\s*[-–:]\s*\d{1,2}\b|in[ -]game|in match|round \d", re.I)
SKIP_MODE_RE = re.compile(r"deathmatch|the range|practice|custom|replication|escalation|skirmish|2v2|snowball", re.I)
OVERWATCH_MODES = {"RANKED": "ranked", "UNRANKED": "unranked"}  # Overwolf game_type values that open betting
MODE_WORDS = {"competitive": "competitive", "unrated": "unrated", "swiftplay": "swiftplay", "premier": "premier",
              "spike rush": "spikerush"}
MAP_RE = re.compile(r"\(([^)]+)\)")  # "Swiftplay (Ascent) 3 - 1" -> Ascent


def status_mode(text: str) -> str:
    return next((v for k, v in MODE_WORDS.items() if k in text.lower()), "any")


def status_map(text: str) -> str | None:
    mp = MAP_RE.search(text)
    return mp[1].strip() if mp else None


def rounds_to_win(text_or_mode: str) -> int:
    """Rounds a team needs to win the game: Swiftplay 5, Spike Rush 4, everything else 13."""
    mode = text_or_mode if text_or_mode in odds.MODE_ROUNDS else status_mode(text_or_mode)
    return odds.MODE_ROUNDS.get(mode, 13)


def finished_games(entries) -> list[tuple[int, str, int, int, str]]:
    """Finished games in a stream of (discord_id, iso time, Valorant status text) entries, oldest first:
    the last score shown before the status drops it (or flashes 0 - 0) is the final score."""
    last: dict[int, tuple[int, int, str, str]] = {}
    out = []
    for uid, at, text in entries:
        score = SCORE_RE.search(text)
        if score and int(score[1]) + int(score[2]):
            last[uid] = (int(score[1]), int(score[2]), text, at)
        elif uid in last:
            ours, theirs, final_text, final_at = last.pop(uid)
            if not SKIP_MODE_RE.search(final_text) and max(ours, theirs) >= rounds_to_win(final_text):
                out.append((uid, final_at, ours, theirs, final_text))
    return out

log = logging.getLogger("valbet")
GREEN, RED, GREY, GOLD = 0x2ECC71, 0xFF4655, 0x95A5A6, 0xF1C40F


def now() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime, style: str = "R") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def short(riot_id: str) -> str:
    return riot_id.split("#")[0]


def split_ids(text: str | None) -> tuple[list[str], list[str]]:
    good, bad, seen = [], [], set()
    for part in (text or "").replace(";", ",").split(","):
        if part.strip():
            rid = parse_riot_id(part)
            if rid and rid.casefold() in seen:
                continue  # same player listed twice
            if rid:
                seen.add(rid.casefold())
            (good if rid else bad).append(rid or part.strip())
    return good[:5], bad


def stat_line(p: PlayerStats) -> str:
    return (f"**{p.riot_id}** — {p.rank} · {p.kd:.2f} K/D · {p.win_pct:.0f}% WR · "
            f"{p.acs:.0f} ACS · {p.hs_pct:.0f}% HS ({p.matches} games)")


def activity_dict(a) -> dict:
    """Everything Discord tells us about one activity, as plain JSON."""
    d = {"type": str(getattr(a, "type", "")).replace("ActivityType.", ""), "name": getattr(a, "name", None)}
    for f in ("details", "state", "large_image_text", "small_image_text", "application_id"):
        if v := getattr(a, f, None):
            d[f] = str(v)
    if party := getattr(a, "party", None):
        d["party"] = party
    if start := getattr(a, "start", None):
        d["start"] = start.isoformat()
    return d


def _intents() -> discord.Intents:
    intents = discord.Intents.default()
    if PRESENCE_TRACKING:  # both are "privileged": switch them on in the Developer Portal → Bot
        intents.presences = True
        intents.members = True
    return intents


class BetBot(discord.Client):
    def __init__(self):
        super().__init__(intents=_intents(),
                         allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True))
        self.tree = app_commands.CommandTree(self)
        self._opening: set[int] = set()  # hosts whose betting is mid-open (blocks double opens)
        self._lineup_locks: dict[int, asyncio.Lock] = {}  # match_id -> serialises update_lineup
        self.tracker = TrackerClient(TRACKER_API_KEY)
        self.henrik = henrik.HenrikClient()  # optional: HENRIK_API_KEY in .env
        self.presence: dict[int, dict] = {}  # discord_id -> latest Valorant status (to find their party)
        self.phase: dict[int, str] = {}      # discord_id -> "pregame" | "ingame" | "other", from their status
        self.auto_opened: dict[int, datetime] = {}
        self.last_score: dict[int, tuple[int, int, str]] = {}  # discord_id -> (ours, theirs, status text)
        self._timers: set[asyncio.Task] = set()  # pending "close betting" timers
        self.ow = ow.OverwatchClient()
        self.ow_playing: dict[int, bool] = {}       # discord_id -> Discord shows them in Overwatch
        self._ow_polled: dict[int, datetime] = {}   # match_id -> last career-profile check

    # ---------- Discord status tracking (experiment: can we see when a match starts?) ----------

    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if not db.is_linked(after.id):  # only players who opted in with /link or /link-overwatch
            return
        acts = [activity_dict(a) for a in after.activities
                if any(g in (getattr(a, "name", "") or "").lower() for g in ("valorant", "overwatch"))]
        prev = self.presence.get(after.id)
        if prev and prev["activities"] == acts:
            return  # same change reported once per shared server
        entry = {"name": after.display_name, "activities": acts, "at": now().isoformat()}
        self.presence[after.id] = entry
        log.info("Status %s: %s", after.display_name, acts or "not playing Valorant")
        with open(PRESENCE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"user": str(after.id), **entry}) + "\n")
        if AUTO_OPEN:
            try:
                if db.linked_riot_id(after.id):
                    await self.maybe_auto_open(after, acts)
            except Exception:
                log.exception("Auto-open failed for %s", after.display_name)
            try:
                await self.maybe_overwatch(after.id, after.display_name, acts)
            except Exception:
                log.exception("Overwatch auto-open failed for %s", after.display_name)

    # ---------- Overwatch from Discord status + career profile (no Overwolf) ----------

    @staticmethod
    def in_overwatch(acts: list[dict]) -> bool:
        return any("overwatch" in (a.get("name") or "").lower() for a in acts)

    async def maybe_overwatch(self, user_id: int, name: str, acts: list[dict]):
        """Discord shows they've just launched Overwatch: open betting on their next game."""
        playing, was = self.in_overwatch(acts), self.ow_playing.get(user_id, False)
        self.ow_playing[user_id] = playing
        if playing and not was:
            await self.open_overwatch(user_id, name, "just launched Overwatch")

    async def open_overwatch(self, host_id: int, name: str, why: str) -> int | None:
        u = db.get_user(host_id)
        if not u["battletag"] or db.active_match_for_host(host_id) or host_id in self._opening:
            return None
        if not u["ow_id"]:  # linked before the profile was public: try to find it now
            try:
                db.set_battletag(host_id, u["battletag"], await self.ow.find_player(u["battletag"]))
            except ow.OverwatchError as e:
                log.info("Overwatch auto-open skipped for %s: %s", name, e)
                return None
            u = db.get_user(host_id)
        channel_id = self.betting_channel_id()
        ch = await self.channel(channel_id) if channel_id else None
        if not ch:
            log.info("Overwatch auto-open skipped for %s: run /panel first", name)
            return None
        self._opening.add(host_id)
        try:
            try:
                recs = await self.ow.records(u["ow_id"])
            except ow.OverwatchError as e:
                log.info("Overwatch auto-open skipped for %s: %s", name, e)
                return None
            both = ow.total(recs)
            opened = now()
            if db.active_match_for_host(host_id):
                return None
            match_id = db.create_match(
                guild_id=int(GUILD_ID or 0), channel_id=channel_id, opener_id=self.user.id, host_id=host_id,
                host_riot=u["battletag"], mode="any", status="open", game="overwatch",
                opened_at=opened.isoformat(), lock_at=(opened + timedelta(minutes=OW_WINDOW)).isoformat(),
                baseline_match_id=None, markets=odds.win_market(both.win_pct, both.played),
                scouting={"host": PlayerStats(riot_id=u["battletag"]).to_dict(), "ow_baseline": ow.snapshot(recs)})
        finally:
            self._opening.discard(host_id)
        try:
            await self._post_bets_open(match_id, ch)
        except ValueError as e:
            log.warning("Couldn't post Overwatch bets for %s: %s", name, e)
            return None
        await ch.send(f"🤖 Betting opened automatically: **{name}** {why}. Bets are on their next **Quick Play or "
                      "Competitive** game (Arcade doesn't count).", allowed_mentions=discord.AllowedMentions.none())
        log.info("Opened Overwatch match %s for %s (%s)", match_id, name, why)
        return match_id

    async def check_overwatch(self, m):
        """Has a Quick Play / Competitive game finished since betting opened? Pay Win/Loss from it."""
        if m["status"] != "locked":
            return  # still open: only games after betting closes count
        last = self._ow_polled.get(m["id"])
        if last and now() - last < OW_POLL:
            return
        self._ow_polled[m["id"]] = now()
        u = db.get_user(m["host_id"])
        if not u["ow_id"]:
            return
        base = json.loads(m["scouting"]).get("ow_baseline")
        if not base:
            return
        try:
            res = ow.result_since(base, await self.ow.records(u["ow_id"]))
        except ow.OverwatchError as e:
            log.warning("Overwatch match %s: %s", m["id"], e)
            return
        if not res:
            return
        outcome, games = res
        name = await self.display_name(m["host_id"])
        if outcome == "mixed":  # two or more games finished between checks with different results: can't tell
            n = db.cancel_match(m["id"])
            await self.refresh_market_message(m["id"])
            if ch := await self.channel(m["channel_id"]):
                await ch.send(f"↩️ {games} of **{name}**'s Overwatch games finished between checks with different "
                              f"results, so the bot can't tell which one was bet on. {n or 0} bet(s) refunded in full.")
        else:
            await self.pay_result(m, name, None if outcome == "draw" else outcome == "win",
                                  "" if games == 1 else f"({games} games, all {outcome}s)")
        self._ow_polled.pop(m["id"], None)
        if self.ow_playing.get(m["host_id"]):  # still in Overwatch: open betting on the next game
            await self.open_overwatch(m["host_id"], name, "is still playing Overwatch")

    # ---------- opening betting automatically from Discord status ----------

    @staticmethod
    def betting_channel_id() -> int | None:
        """Where automatic bets go: the /panel channel, else wherever the last bet was."""
        last = db.recent_matches(1)
        cid = db.get_meta("panel_channel") or (last[0]["channel_id"] if last else None)
        return int(cid) if cid else None

    # ---------- live game data from the Overwolf app ----------

    async def overwolf_event(self, p: dict) -> dict:
        """Called by the Overwolf app on: agent select ('pregame'), roster changes ('roster'),
        'match_start' and 'match_end'. Opens betting, fills in teams/opponents, and settles."""
        if p.get("game") == "overwatch":
            return await self.overwatch_event(p)
        me, mates, opps, ranks = overwolf_data.lineup(p)
        if not me:
            return {"ok": False, "reason": "Couldn't read your Riot ID from Valorant."}
        host_id = db.user_by_riot(me)
        if not host_id:
            return {"ok": False, "reason": f"{me} hasn't used /link in Discord."}
        if p.get("owner") and p["owner"] != host_id:  # a personal key only reports its owner's own games
            return {"ok": False, "reason": f"This address belongs to someone else; {me} needs their own /overwolf-link."}
        mode, skip = overwolf_data.parse_mode(p.get("mode"))
        if skip:
            return {"ok": True, "action": "skipped (custom game or non-5v5 mode)"}
        event, m = p.get("event"), db.active_match_for_host(host_id)
        name = await self.display_name(host_id)
        log.info("Overwolf %s from %s: %d teammates, %d opponents", event, me, len(mates), len(opps))

        if event == "match_end":
            m = m or db.awaiting_match_for_host(host_id)  # Win/Loss may already be paid from Discord status
            if not m:
                return {"ok": True, "action": "no open bet to settle"}
            detail = overwolf_data.final_detail(p, me, ranks)
            if not detail:
                return {"ok": False, "reason": "End-of-game scoreboard was incomplete; tracker.gg will settle it."}
            await self.settle(m, detail)
            self.auto_opened.pop(host_id, None)  # game's over: the next one can open straight away
            return {"ok": True, "action": f"settled match {m['id']}"}

        if m:  # already open: add anyone newly visible (e.g. opponents appear once the match loads)
            await self.update_lineup(m, mates, opps, ranks)
            return {"ok": True, "action": f"updated match {m['id']}"}

        if event not in ("pregame", "match_start"):
            return {"ok": True, "action": "no open match"}
        last = self.auto_opened.get(host_id)
        if not AUTO_OPEN or (last and now() - last < AUTO_COOLDOWN):
            return {"ok": True, "action": "not opening (auto-open off or on cooldown)"}
        channel_id = self.betting_channel_id()
        if not channel_id:
            return {"ok": False, "reason": "Run /panel in your betting channel first."}
        self.auto_opened[host_id] = now()
        try:
            match_id, problems = await self.open_match(
                guild_id=int(GUILD_ID or 0), channel_id=channel_id, opener_id=self.user.id, host_id=host_id,
                host_name=name, opponents=", ".join(opps) or None, teammates=", ".join(mates) or None,
                mode=mode, window=AUTO_WINDOW, known_ranks=ranks,
                map_name=overwolf_data.MAPS.get(p.get("map") or "") or None)
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
        if ch := await self.channel(channel_id):
            when = "is in agent select" if event == "pregame" else "just started a match"
            await ch.send(f"🤖 Betting opened automatically: **{name}** {when}. Teams come straight from the game.",
                          allowed_mentions=discord.AllowedMentions.none())
        return {"ok": True, "action": f"opened match {match_id}", "problems": problems}

    async def display_name(self, user_id: int) -> str:
        g = self.get_guild(int(GUILD_ID)) if GUILD_ID else None
        member = g.get_member(user_id) if g else None
        if member:
            return member.display_name
        try:
            return (await self.fetch_user(user_id)).display_name
        except discord.HTTPException:
            return "Someone"

    async def update_lineup(self, m, mates: list[str], opps: list[str], ranks: dict[str, str]):
        """Add newly seen teammates/opponents to an open match. Odds are only re-priced while nobody
        has bet yet, so nobody's bet changes under them; the enemy recap posts once opponents appear."""
        async with self._lineup_locks.setdefault(m["id"], asyncio.Lock()):
            m = db.get_match(m["id"])
            if not m or m["status"] not in ("open", "locked"):
                return
            sc = json.loads(m["scouting"])
            known_opp = {p["riot_id"].casefold() for p in sc.get("opponents", [])}
            known_tm = {p["riot_id"].casefold() for p in sc.get("teammates", [])}
            host_key = m["host_riot"].casefold()
            new_opps = [r for r in opps if r.casefold() not in known_opp | known_tm | {host_key}]
            no_bets = m["status"] == "open" and not db.bets_for_match(m["id"])
            new_tms = [r for r in mates if r.casefold() not in known_tm | {host_key}] if no_bets else []
            new_opps = [r for r in new_opps if r.casefold() not in {t.casefold() for t in new_tms}]
            if not new_opps and not new_tms:
                return
            (opp_stats, _), (tm_stats, _) = await asyncio.gather(scout(new_opps, True, ranks),
                                                                  scout(new_tms, True, ranks))
            # Re-check right before writing (no awaits below until the update): a bet may have landed meanwhile.
            fresh = db.get_match(m["id"])
            if not fresh or fresh["status"] not in ("open", "locked"):
                return
            no_bets = fresh["status"] == "open" and not db.bets_for_match(m["id"])
            sc["opponents"] = sc.get("opponents", []) + [s.to_dict() for s in opp_stats]
            if no_bets:
                sc["teammates"] = (sc.get("teammates", []) + [s.to_dict() for s in tm_stats])[:odds.TEAM_SIZE - 1]
            changes = {"scouting": json.dumps(sc)}
            if no_bets:
                changes["markets"] = json.dumps(price_markets(sc, m["mode"]))
            db.update_match(m["id"], **changes)
            await self.refresh_market_message(m["id"])
            if opp_stats and (ch := await self.channel(m["channel_id"])):
                try:
                    await ch.send(embed=enemy_recap_embed(
                        m["host_riot"], [PlayerStats.from_dict(o) for o in sc["opponents"]]))
                except discord.HTTPException as e:
                    log.warning("Couldn't post the enemy recap for match %s: %s", m["id"], e)

    async def settle_from_status(self, member: discord.Member, ours: int, theirs: int, text: str):
        """A game just ended in the player's Discord status ("Swiftplay (Ascent) 5 - 3", their team first):
        add it to their game history, and pay Win/Loss bets from the final score. Top-frag bets need the
        scoreboard, so they wait for Overwolf/tracker.gg."""
        if SKIP_MODE_RE.search(text) or max(ours, theirs) < rounds_to_win(text):
            # left early / remake / not a 5v5 game: not a real result, leave any bets to the fallbacks
            log.info("Ignoring final status for %s: %d-%d (%s)", member.display_name, ours, theirs, text)
            return
        db.record_game(member.id, now().isoformat(), status_mode(text), status_map(text), ours, theirs)
        m = db.active_match_for_host(member.id)
        if not m:
            return
        won = None if ours == theirs else ours > theirs
        await self.pay_result(m, member.display_name, won, f"{ours}–{theirs}", status_map(text))

    async def pay_result(self, m, player: str, won: bool | None, score: str = "", map_name: str | None = None):
        """Pay Win/Loss bets from a known result and post the results message. Top-frag bets (Valorant)
        need the scoreboard, so they're left waiting."""
        result = "push" if won is None else ("win" if won else "loss")
        # Win/Loss bets, plus group combos whose result part is already wrong (the rest wait for the top frag)
        bets = db.settle_bets(m["id"], {"win", "combo"},
                              lambda market, side: odds.judge({"win": result}, market, side))
        if bets is None:
            return
        db.set_outcome(m["id"], result)
        log.info("Settled Win/Loss on match %s: %s %s", m["id"], result, score)
        await self.refresh_market_message(m["id"])
        ch = await self.channel(m["channel_id"])
        if not ch:
            return
        m = db.get_match(m["id"])
        e = discord.Embed(
            title=f"🏁 {player}: {'Victory' if won else 'Defeat' if won is False else 'Draw'}"
                  + (f" {score}" if score else "") + (f" on {map_name}" if map_name else ""),
            description=f"**Bets**\n{bettor_breakdown(m, db.bets_for_match(m['id']))}"[:4000],
            color=GREEN if won else RED if won is False else GREY)
        team_name = f"{player}'s team"
        e.add_field(name="Who won", value={"win": f"✅ {team_name} won", "loss": f"❌ {team_name} lost",
                                           "push": "↩️ Draw, result bets refunded"}[result])
        if json.loads(m["markets"]).get("topfrag"):  # Overwatch games have no top-frag bet
            waiting = m["status"] != "resolved"
            held = sum(b["amount"] for b in db.bets_for_match(m["id"]) if b["status"] == "pending")
            e.add_field(name="Top frag", value=(f"⏳ Info not received yet: **{held:,} coins** of top frag and group "
                                                "bets are on hold until it is." if waiting
                                                else "No top frag bets on this game"))
            if waiting:
                e.set_footer(text="Coins on hold pay out as soon as the top frag info (ACS from tracker.gg) arrives, "
                                  f"or are refunded in full after {MATCH_TIMEOUT.total_seconds() / 3600:g} h. "
                                  "/mybets shows yours.")
        await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def valorant_text(acts: list[dict]) -> tuple[str, str | None]:
        """All the text Valorant's own status shows (not Medal & co.), and its party ID."""
        for a in acts:
            if (a.get("name") or "").strip().lower() == "valorant":
                text = " · ".join(a[f] for f in ("details", "state", "large_image_text", "small_image_text") if a.get(f))
                party = (a.get("party") or {}).get("id")
                if text or party:
                    return text, party
        return "", None

    async def maybe_auto_open(self, member: discord.Member, acts: list[dict]):
        text, party = self.valorant_text(acts)
        phase = "pregame" if PREGAME_RE.search(text) else "ingame" if INGAME_RE.search(text) else "other"
        before, self.phase[member.id] = self.phase.get(member.id), phase
        score = SCORE_RE.search(text)
        rounds_played = int(score[1]) + int(score[2]) if score else 0
        # Game over: the status drops the score (or flashes "0 - 0") right after a final score.
        last = self.last_score.get(member.id)
        if score and rounds_played:
            self.last_score[member.id] = (int(score[1]), int(score[2]), text)
        elif last:
            self.last_score.pop(member.id, None)
            await self.settle_from_status(member, *last)
        # Valorant's status shows the live score ("Swiftplay (Ascent) 1 - 0"): close betting as soon as
        # the first round has a result, even if the betting window hasn't run out yet.
        if rounds_played >= 1 and (m := db.active_match_for_host(member.id)) and m["status"] == "open":
            if await self.lock_match(m):
                log.info("Closed betting on match %s: first round finished (%s)", m["id"], text)
        # Open on entering agent select; if that was missed, on entering the match itself (an observed
        # transition from "other", never from an unknown starting state).
        if phase == "other" or phase == before or (phase == "ingame" and before != "other"):
            return
        if rounds_played >= 1:
            return  # rounds already played: too late to open betting on this match
        if SKIP_MODE_RE.search(text):
            return
        last = self.auto_opened.get(member.id)
        if last and now() - last < AUTO_COOLDOWN:
            return
        channel_id = self.betting_channel_id()
        if not channel_id:
            log.info("Auto-open skipped for %s: run /panel in a channel first", member.display_name)
            return
        # Linked friends in the same Valorant party are their teammates.
        mates = [r for uid, p in self.presence.items() if uid != member.id
                 and self.valorant_text(p["activities"])[1] == party and party and (r := db.linked_riot_id(uid))]
        self.auto_opened[member.id] = now()
        try:
            match_id, problems = await self.open_match(
                guild_id=member.guild.id, channel_id=int(channel_id), opener_id=self.user.id,
                host_id=member.id, host_name=member.display_name, opponents=None,
                teammates=", ".join(mates[:odds.TEAM_SIZE - 1]) or None, mode=status_mode(text), window=AUTO_WINDOW,
                map_name=status_map(text))
        except ValueError as e:  # not linked, already has a match open, etc.
            log.info("Auto-open skipped for %s: %s", member.display_name, e)
            return
        log.info("Auto-opened match %s for %s (%s; %s)", match_id, member.display_name, phase, text)
        if ch := await self.channel(int(channel_id)):
            when = "is in agent select" if phase == "pregame" else "just started a match"
            await ch.send(f"🤖 Betting opened automatically: Discord shows **{member.display_name}** {when}."
                          + (f"\n-# {' · '.join(problems)}" if problems else ""),
                          allowed_mentions=discord.AllowedMentions.none())

    async def setup_hook(self):
        self.tree.add_command(match_group)
        self.add_dynamic_items(ui.SlipButton, ui.TopFragSelect)  # the controls on each "Bets open" post survive restarts
        self.add_dynamic_items(ui.RetiredPanelButton)  # old panels' Open/Close betting explain how it works now
        if GUILD_ID:  # guild sync is instant; global sync can take up to an hour
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.backfill_games()
        self.watcher.start()
        self.daily_payout.start()
        await dashboard.start(self)

    @staticmethod
    def backfill_games():
        """Add games from the Discord status log to the game history (once; already-known games are skipped)."""
        if db.get_meta("games_backfilled") or not os.path.exists(PRESENCE_LOG):
            return
        entries = []
        with open(PRESENCE_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    entries.append((int(e["user"]), e["at"], BetBot.valorant_text(e["activities"])[0]))
                except (ValueError, KeyError, TypeError):
                    continue
        added = sum(db.record_game(uid, at, status_mode(text), status_map(text), ours, theirs)
                    for uid, at, ours, theirs, text in finished_games(entries))
        db.set_meta("games_backfilled", "1")
        log.info("Game history: added %d finished games from the status log", added)

    async def close(self):
        await self.tracker.close()
        await self.ow.close()
        await self.henrik.close()
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        # Anyone already in Overwatch when the bot starts: Discord won't send a "just launched" change for them.
        for g in self.guilds:
            for member in g.members:
                acts = [activity_dict(a) for a in member.activities]
                if db.is_linked(member.id) and self.in_overwatch(acts) and not self.ow_playing.get(member.id):
                    try:
                        await self.maybe_overwatch(member.id, member.display_name, acts)
                    except Exception:
                        log.exception("Overwatch check on startup failed for %s", member.display_name)

    async def channel(self, channel_id: int) -> discord.abc.Messageable | None:
        try:
            return self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        except discord.HTTPException:
            return None

    # ---------- background watcher ----------

    @tasks.loop(seconds=POLL_SECONDS)
    async def watcher(self):
        for m in db.active_matches():
            try:
                await self.process_match(m)
            except TrackerError as e:
                log.warning("Match %s: tracker.gg error: %s", m["id"], e)
            except Exception:
                log.exception("Match %s: unexpected error", m["id"])

    @watcher.before_loop
    async def _before_watcher(self):
        await self.wait_until_ready()

    # Checks every 10 minutes; pays once per UTC day (the db remembers the last payout date). Silent: no channel post.
    @tasks.loop(minutes=10)
    async def daily_payout(self):
        try:
            n = db.pay_daily_if_due()
            if n is not None:
                log.info("Daily payout: %s coins to %s users", db.DAILY_AMOUNT, n)
        except Exception:
            log.exception("Daily payout failed")

    @daily_payout.before_loop
    async def _before_daily(self):
        await self.wait_until_ready()

    async def process_match(self, m):
        opened = datetime.fromisoformat(m["opened_at"])
        ch = await self.channel(m["channel_id"])

        if (m["status"] == "open" and now() >= datetime.fromisoformat(m["lock_at"])
                and db.lock_match(m["id"], now().isoformat())):
            await self.refresh_market_message(m["id"])
            if ch:
                await ch.send(f"🔒 Betting is closed for **{short(m['host_riot'])}**'s match "
                              f"(#{m['id']}). Waiting for the game to finish…")

        if now() - opened > MATCH_TIMEOUT:
            n = db.cancel_match(m["id"])
            if n is None:  # already settled/cancelled elsewhere
                return
            await self.refresh_market_message(m["id"])
            if ch:
                why = ("the top frag info never arrived" if m["status"] == "awaiting"
                       else "no finished game was found")
                await ch.send(f"⌛ For **{short(m['host_riot'])}**'s game (#{m['id']}), {why} within "
                              f"{MATCH_TIMEOUT.total_seconds() / 3600:g} h, so the {n} bet(s) still on hold were "
                              "refunded in full. The coins are back in everyone's balance.")
            return

        if m["game"] == "overwatch":  # settled from their career profile (or the Overwolf app), not tracker.gg
            await self.check_overwatch(m)
            return
        if now() - opened < MIN_GAME_TIME:
            return

        try:
            detail = await self.find_finished_match(m)
        except TrackerError as e:
            if not self.henrik.enabled:
                raise
            log.info("Match %s: tracker.gg unavailable (%s), checking HenrikDev", m["id"], e)
            detail = await self.henrik_finished_match(m)
        if detail:
            await self.settle(m, detail)

    async def henrik_finished_match(self, m) -> MatchDetail | None:
        """The host's newest finished game from HenrikDev, if it started around when betting opened."""
        opened = datetime.fromisoformat(m["opened_at"])
        try:
            details = await self.henrik.recent_details(m["host_riot"], 1)
        except henrik.HenrikError as e:
            log.warning("Match %s: HenrikDev: %s", m["id"], e)
            return None
        d = details[0] if details else None
        if not d or d.id == json.loads(m["scouting"]).get("henrik_baseline"):
            return None
        if d.timestamp and d.timestamp < opened - timedelta(minutes=10):
            return None  # an older game: the one bet on hasn't finished yet
        return d

    async def find_finished_match(self, m) -> MatchDetail | None:
        """The newest game in the host's history that wasn't there when betting opened."""
        opened = datetime.fromisoformat(m["opened_at"])
        for s in await self.tracker.get_recent_matches(m["host_riot"], m["mode"]):
            if s.id == m["baseline_match_id"]:
                return None
            # Guard against a previous game that tracker.gg hadn't indexed yet at open time.
            if s.timestamp and s.timestamp < opened - timedelta(minutes=10):
                return None
            return await self.tracker.get_match(s.id)
        return None

    # ---------- match actions (all automatic: nobody opens, closes, settles or cancels by hand) ----------

    async def open_match(self, *, host_id: int, host_name: str, **kw) -> tuple[int, list[str]]:
        """Open betting and post the markets (+ enemy recap). Raises ValueError with a user-facing message."""
        if host_id in self._opening:
            raise ValueError(f"Betting is already being opened for {host_name}.")
        self._opening.add(host_id)
        try:
            return await self._open_match(host_id=host_id, host_name=host_name, **kw)
        finally:
            self._opening.discard(host_id)

    async def _open_match(self, *, guild_id: int, channel_id: int, opener_id: int, host_id: int, host_name: str,
                          opponents: str | None, teammates: str | None, mode: str, window: float,
                          known_ranks: dict[str, str] | None = None,
                          map_name: str | None = None) -> tuple[int, list[str]]:
        riot = db.get_user(host_id)["riot_id"]
        if not riot:
            raise ValueError(f"{host_name} hasn't linked a Riot ID yet. They need to use /link.")
        active_msg = (f"{host_name} already has an active match. It settles when the game finishes, "
                      f"or refunds automatically after {MATCH_TIMEOUT.total_seconds() / 3600:g} hours.")
        if db.active_match_for_host(host_id):
            raise ValueError(active_msg)
        ch = await self.channel(channel_id)
        if not ch:
            raise ValueError("The bot can't see that channel.")
        stats_problem = []
        try:
            host_stats = await self.tracker.get_profile(riot)
        except PlayerNotFound:
            raise ValueError(f"{riot} wasn't found on tracker.gg. They should re-check their /link.") from None
        except TrackerError as e:
            # tracker.gg down or key not active yet: open anyway with average stats so betting still works.
            live_rank = next((v for k, v in (known_ranks or {}).items() if k.casefold() == riot.casefold()), None)
            host_stats = PlayerStats(riot_id=riot, rank=live_rank or "Unranked", tier=tier_index(live_rank))
            stats_problem = [f"Couldn't load {riot}'s stats ({e}), so the odds use average stats"
                             + (f" and their in-game rank ({live_rank})." if live_rank and tier_index(live_rank) else ".")]

        recent = []
        try:
            recent = await self.tracker.get_recent_matches(riot, mode)
        except TrackerError as e:
            log.warning("Couldn't read match history for %s: %s", riot, e)
        baseline = recent[0].id if recent else None

        opp_ids, bad_opp = split_ids(opponents)
        tm_ids, bad_tm = split_ids(teammates)
        opp_ids = [r for r in opp_ids if r.casefold() != riot.casefold()]
        tm_ids = [r for r in tm_ids if r.casefold() != riot.casefold()]
        # Their last few finished games: recent form for the odds, and who their party is.
        details = await self.recent_details(recent, odds.FORM_GAMES)
        henrik_baseline = None
        if not details and self.henrik.enabled:  # tracker.gg unavailable: HenrikDev has the same scoreboards
            try:
                details = await self.henrik.recent_details(riot, 5)
                henrik_baseline = details[0].id if details else None
            except henrik.HenrikError as e:
                log.warning("HenrikDev history for %s: %s", riot, e)
        form = odds.recent_form(riot, details)
        history = odds.history_form([dict(g) for g in db.games_for(host_id)])
        if not form and history:  # tracker.gg unavailable: use the games the bot watched itself
            form = history
        host_stats = odds.blend(host_stats, form)
        map_record = None
        if map_name:  # their record on this map, from whichever source has more games on it
            recs = [f["maps"].get(map_name) for f in (form, history) if f and f.get("maps")]
            map_record = max((r for r in recs if r), key=lambda r: r[1], default=None)

        party_note = []
        # Their party: linked friends Discord shows in it, plus whoever queued with them in their last game
        # (exact party IDs from HenrikDev), plus regulars from recent games if there's nothing else to go on.
        known = {r.casefold() for r in tm_ids}
        found = [r for r in henrik.party_of(riot, details) if r.casefold() not in known]
        if not tm_ids and not found:
            found = self.likely_party(riot, details)
        if found:
            tm_ids = (tm_ids + found)[:odds.TEAM_SIZE - 1]
            party_note = [f"Party found from recent games: {', '.join(short(r) for r in found)}"]
        tm_keys = {r.casefold() for r in tm_ids} | {riot.casefold()}
        opp_ids = [r for r in opp_ids if r.casefold() not in tm_keys]  # a teammate can't also be an opponent
        (opp_stats, opp_fail), (tm_stats, tm_fail) = await asyncio.gather(
            scout(opp_ids, keep_on_error=bool(known_ranks), known_ranks=known_ranks),
            scout(tm_ids, keep_on_error=True, known_ranks=known_ranks))
        tm_forms = {s.riot_id: odds.recent_form(s.riot_id, details) for s in tm_stats}
        tm_stats = [odds.blend(s, tm_forms[s.riot_id]) for s in tm_stats]
        # How much each player's ACS swings game to game (for the top-frag odds).
        spreads = {r: {"acs_sd": f.get("acs_sd"), "acs_n": f.get("acs_n")}
                   for r, f in {riot: form, **tm_forms}.items() if f and f.get("acs_sd")}

        scouting = {"host": host_stats.to_dict(), "form": form,
                    "teammates": [s.to_dict() for s in tm_stats],
                    "opponents": [s.to_dict() for s in opp_stats],
                    "map": map_name, "map_record": map_record, "spreads": spreads,
                    "agents": main_agents([riot, *tm_ids], details), "henrik_baseline": henrik_baseline,
                    "calibration": odds.fit_calibration(db.predictions())}
        markets = price_markets(scouting, mode)
        opened = now()
        if db.active_match_for_host(host_id):  # one may have appeared while we were scouting
            raise ValueError(active_msg)
        match_id = db.create_match(
            guild_id=guild_id, channel_id=channel_id, opener_id=opener_id,
            host_id=host_id, host_riot=riot, mode=mode, status="open",
            opened_at=opened.isoformat(), lock_at=(opened + timedelta(minutes=window)).isoformat(),
            baseline_match_id=baseline, markets=markets, scouting=scouting,
        )
        await self._post_bets_open(match_id, ch)
        if opp_stats:
            try:
                await ch.send(embed=enemy_recap_embed(riot, opp_stats))
            except discord.HTTPException as e:
                log.warning("Couldn't post the enemy recap for match %s: %s", match_id, e)
        problems = stats_problem + party_note + [f"Not a Riot ID: {x}" for x in bad_opp + bad_tm] + \
            [f"Lookup failed: {x}" for x in opp_fail + tm_fail]
        return match_id, problems

    async def _post_bets_open(self, match_id: int, ch):
        """Post the 'Bets open' message with its buttons, and close betting exactly when the window ends."""
        m = db.get_match(match_id)
        try:
            msg = await ch.send(embed=markets_embed(m), view=ui.market_view(m))
        except discord.HTTPException:
            db.cancel_match(match_id)
            raise ValueError("Couldn't post in that channel (check the bot's Send Messages / Embed Links "
                             "permissions).") from None
        db.update_match(match_id, message_id=msg.id)
        task = asyncio.create_task(self._close_on_time(match_id))  # the once-a-minute watcher is a backstop
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    # ---------- Overwatch (live data from the Overwolf app only; Overwatch shares nothing with Discord) ----------

    async def overwatch_event(self, p: dict) -> dict:
        """'match_start' opens Win/Loss betting for a linked player's Ranked/Unranked game;
        'match_end' pays it from Overwolf's victory/defeat result."""
        me = (p.get("me") or "").strip()
        host_id = db.battletag_owner(me) if me else None
        if not host_id:
            return {"ok": False, "reason": f"{me or 'Your BattleTag'} hasn't used /link-overwatch in Discord."}
        if p.get("owner") and p["owner"] != host_id:
            return {"ok": False, "reason": f"This address belongs to someone else; {me} needs their own /overwolf-link."}
        game_type = str(p.get("game_type") or "").upper()
        if game_type not in OVERWATCH_MODES:
            return {"ok": True, "action": f"skipped ({game_type.lower() or 'unknown mode'})"}
        event, m = p.get("event"), db.active_match_for_host(host_id)
        name = await self.display_name(host_id)
        log.info("Overwolf Overwatch %s from %s (%s)", event, me, game_type)

        if event == "match_end":
            if not m or m["game"] != "overwatch":
                return {"ok": True, "action": "no open bet to settle"}
            outcome = str(p.get("outcome") or "").lower()
            if outcome not in ("victory", "defeat"):
                return {"ok": False, "reason": "No result in the end-of-game data; bets refund after the timeout."}
            await self.pay_result(m, name, outcome == "victory", map_name=p.get("map_name"))
            self.auto_opened.pop(host_id, None)
            return {"ok": True, "action": f"settled match {m['id']}"}

        if event != "match_start":
            return {"ok": True, "action": "nothing to do"}
        if m:
            return {"ok": True, "action": f"match {m['id']} already open"}
        last = self.auto_opened.get(host_id)
        if not AUTO_OPEN or (last and now() - last < AUTO_COOLDOWN):
            return {"ok": True, "action": "not opening (auto-open off or on cooldown)"}
        ch_id = self.betting_channel_id()
        ch = await self.channel(ch_id) if ch_id else None
        if not ch:
            return {"ok": False, "reason": "Run /panel in your betting channel first."}
        self.auto_opened[host_id] = now()
        opened = now()
        match_id = db.create_match(
            guild_id=int(GUILD_ID or 0), channel_id=ch_id, opener_id=self.user.id, host_id=host_id,
            host_riot=me, mode=OVERWATCH_MODES[game_type], status="open", game="overwatch",
            opened_at=opened.isoformat(), lock_at=(opened + timedelta(minutes=AUTO_WINDOW)).isoformat(),
            baseline_match_id=None, markets=odds.even_win_market(),
            scouting={"host": PlayerStats(riot_id=me).to_dict()})
        try:
            await self._post_bets_open(match_id, ch)
        except ValueError as e:
            return {"ok": False, "reason": str(e)}
        await ch.send(f"🤖 Betting opened automatically: **{name}** just started an Overwatch match.",
                      allowed_mentions=discord.AllowedMentions.none())
        return {"ok": True, "action": f"opened match {match_id}"}

    async def recent_details(self, recent, n: int) -> list[MatchDetail]:
        """Full scoreboards for the newest n finished games (newest first); games that fail to load are skipped."""
        results = await asyncio.gather(*(self.tracker.get_match(s.id) for s in recent[:n]), return_exceptions=True)
        return [r for r in results if isinstance(r, MatchDetail)]

    @staticmethod
    def likely_party(riot: str, details: list[MatchDetail], games: int = 5, min_games: int = 2) -> list[str]:
        """tracker.gg can't see a game before it ends, so guess the party: players who were on this
        player's team in at least `min_games` of their last `games` matches (premades/duos repeat; randoms don't)."""
        counts: Counter[str] = Counter()
        for detail in details[:games]:
            me = detail.find_player(riot)
            if me:
                counts.update(p.riot_id for p in detail.players
                              if p.team == me.team and p.riot_id.casefold() != riot.casefold())
        return [r for r, n in counts.most_common(odds.TEAM_SIZE - 1) if n >= min_games]

    async def _close_on_time(self, match_id: int):
        """Close betting the moment the window ends (the once-a-minute watcher is only a backstop)."""
        m = db.get_match(match_id)
        await asyncio.sleep(max((datetime.fromisoformat(m["lock_at"]) - now()).total_seconds(), 0))
        m = db.get_match(match_id)
        if m and m["status"] == "open":
            await self.lock_match(m)

    async def lock_match(self, m) -> bool:
        if m["status"] != "open":
            return False
        if not db.lock_match(m["id"], now().isoformat()):
            return False
        await self.refresh_market_message(m["id"])
        if ch := await self.channel(m["channel_id"]):
            await ch.send(f"🔒 Betting closed for **{short(m['host_riot'])}**'s match. GLHF!")
        return True

    def place_bets(self, user_id: int, match_id: int, picks: list[tuple[str, str, int]],
                   seen: dict[tuple[str, str], float] | None = None, group: bool = False) -> tuple[list[dict], int]:
        """Place 1–2 bets from a bet slip, all or none. Returns (placed bets, new balance). Raises ValueError.
        `seen`: the odds the slip showed; if they've dropped since (other bets moved them), nothing is placed
        so the person can look at the new odds first. Afterwards the odds are re-priced for the next bets.
        `group`: join the group pool on each pick (bonus payout once the pool is big enough)."""
        m = db.get_match(match_id)
        if not ui.betting_open(m):
            raise ValueError("Betting is closed for this match.")
        if not picks:
            raise ValueError("Pick a result or a top fragger first.")
        mk = json.loads(m["markets"])
        # One side per person: with odds that move, betting both sides could lock in a sure profit.
        mine = {(b["market"], b["side"]) for b in db.bets_for_match(match_id) if b["user_id"] == user_id}
        for market, side, _ in picks:
            other = next((s for mkt, s in mine if mkt == market and s != side), None)
            if other:
                raise ValueError(f"You already bet on **{odds.bet_label(mk, m['host_riot'], market, other)}** "
                                 "in this game. You can add more to that bet, but not bet against it.")
        placed = []
        for market, side, amount in picks:
            if amount < MIN_BET:
                raise ValueError(f"Minimum bet is {MIN_BET} coins.")
            if market == "win":
                if side not in ("win", "loss"):
                    raise ValueError("Pick Win or Loss.")
                if user_id == m["host_id"] and side == "loss":
                    raise ValueError("No betting against yourself 😉")
                price = mk["win"][side]
            elif market == "combo":
                result, _ = odds.split_combo(side)
                price = odds.combo_price(mk, side)
                if price is None:
                    raise ValueError("Pick a result and a top fragger for a group bet.")
                if user_id == m["host_id"] and result == "loss":
                    raise ValueError("No betting against yourself 😉")
            else:
                opt = next((o for o in mk["topfrag"] if o["riot"] == side), None)
                if not opt:
                    raise ValueError("That player isn't in this match's top-frag bet.")
                price = opt["odds"]
            if seen and price < seen.get((market, side), 0) - 1e-9:
                raise ValueError(f"The odds just moved: **{odds.bet_label(mk, m['host_riot'], market, side)}** "
                                 f"now pays ×{price:.2f}. Press Place again to accept.")
            placed.append({"market": market, "side": side, "amount": amount, "odds": price, "grp": group})
        total = sum(b["amount"] for b in placed)
        balance = db.get_user(user_id)["balance"]
        if total > balance:
            raise ValueError(f"That's {total:,} coins in total, but you only have {balance:,}.")
        for b in placed:
            balance = db.place_bet(match_id, user_id, b["market"], b["side"], b["amount"], b["odds"], group)
        if group:
            pools = db.group_pools(db.bets_for_match(match_id))
            for b in placed:
                b["pool"] = pools.get((b["market"], b["side"]))
        crowd = [dict(b) for b in db.bets_for_match(match_id) if b["user_id"] != m["host_id"]]
        db.update_match(match_id, markets=json.dumps(odds.reprice(mk, crowd)))
        return placed, balance

    async def announce_bets(self, user: discord.abc.User, m, placed: list[dict], balance: int):
        """Public one-liner per bet (odds/payout stay private), then update the pools on the post."""
        mk = json.loads(m["markets"])
        name = getattr(user, "display_name", user.name)
        if ch := await self.channel(m["channel_id"]):
            lines = []
            for b in placed:
                label = odds.bet_label(mk, m["host_riot"], b["market"], b["side"])
                if b.get("grp") and (pool := b.get("pool")):
                    lines.append(f"👥 **{name}** `◎ {balance:,}` put **{b['amount']:,}** into the "
                                 f"**{label}** group pool · {pool_status(pool)}")
                else:
                    lines.append(f"🎟️ **{name}** `◎ {balance:,}` bet **{b['amount']:,}** on **{label}**")
            await ch.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())
        await self.refresh_market_message(m["id"])

    async def refresh_market_message(self, match_id: int):
        """Re-draw the 'Bets open' post: current pools, and no buttons once betting has closed."""
        m = db.get_match(match_id)
        if not m or not m["message_id"]:
            return
        ch = await self.channel(m["channel_id"])
        if not ch:
            return
        try:
            await ch.get_partial_message(m["message_id"]).edit(
                embed=markets_embed(m, db.bets_for_match(m["id"])),
                view=ui.market_view(m) if ui.betting_open(m) else None)
        except discord.HTTPException as e:
            log.warning("Couldn't update the post for match %s: %s", m["id"], e)

    async def settle(self, m, detail: MatchDetail):
        m = db.get_match(m["id"])  # the caller's row may be stale
        if not m or m["status"] not in ("open", "locked", "awaiting"):
            return
        ch = await self.channel(m["channel_id"])
        host = detail.find_player(m["host_riot"])
        if not host:
            log.warning("Match %s: host not in tracker match %s", m["id"], detail.id)
            return
        markets = json.loads(m["markets"])
        outcome = odds.compute_outcome(detail, host)
        results = odds.settle(markets, outcome)
        judge = lambda market, side: odds.judge(results, market, side)
        db.set_outcome(m["id"], results["win"])
        if results["topfrag"] is None:
            # Scoreboard without ACS (Overwolf): pay Win/Loss now, top frag waits for tracker.gg's ACS
            # (and so do group combos, unless their result part is already wrong).
            known = {k: v for k, v in results.items() if k != "topfrag"}
            paid = db.settle_bets(m["id"], {"win", "combo"}, lambda market, side: odds.judge(known, market, side))
            if not paid:  # nothing new to pay (e.g. Win/Loss already paid from Discord status)
                return
            bets = db.bets_for_match(m["id"])
        else:
            if results.get("absent"):
                reprice_for_lineup(m, markets, results["absent"])
            bets = db.settle_match(m["id"], judge, detail.id, {"outcome": outcome, "results": results})
            if bets is None:
                return
        await self.refresh_market_message(m["id"])
        if ch:
            for e in result_embeds(m, markets, detail, host, outcome, results, bets):
                await ch.send(embed=e)


bot = BetBot()


def pool_status(pool: dict) -> str:
    """'1,250 coins from 3 people · +20% bonus · +30% at 2,000'"""
    s = f"{pool['total']:,} coins from {pool['people']} {'person' if pool['people'] == 1 else 'people'}"
    if pool["bonus"]:
        s += f" · **+{pool['bonus']}% bonus**"
    elif pool["people"] < db.GROUP_MIN_PEOPLE and db.group_bonus(pool["total"], db.GROUP_MIN_PEOPLE):
        s += f" · needs {db.GROUP_MIN_PEOPLE - pool['people']} more person for its bonus"
    if nxt := db.next_tier(pool["total"]):
        s += f" · +{nxt[1]}% at {nxt[0]:,}"
    return s


def group_rules() -> str:
    tiers = ", ".join(f"+{pct}% from {need:,}" for need, pct in db.GROUP_TIERS)
    return (f"👥 **Group bets:** pick a result and a top fragger together and chip into that pick's shared pool. "
            f"Once the pool reaches these amounts (with at least {db.GROUP_MIN_PEOPLE} people in it), everyone in "
            f"it gets a bonus on their winnings: {tiers}.")


def price_markets(sc: dict, mode: str) -> dict:
    """Odds for a match from everything scouted for it (stored with the match, so it can be re-priced
    when Overwolf adds players)."""
    host = PlayerStats.from_dict(sc["host"])
    team = [host] + [PlayerStats.from_dict(t) for t in sc.get("teammates", [])]
    opps = [PlayerStats.from_dict(o) for o in sc.get("opponents", [])]
    markets = odds.build_markets(host, team, opps, sc.get("form"), mode=mode, map_record=sc.get("map_record"),
                                 cal=sc.get("calibration"), forms=sc.get("spreads"))
    agents = {k.casefold(): v for k, v in (sc.get("agents") or {}).items()}
    for o in markets["topfrag"]:
        if agent := agents.get(o["riot"].casefold()):
            o["agent"] = agent
    return markets


def reprice_for_lineup(m, markets: dict, absent: list[str]):
    """Some listed players weren't in the game (a random took their slot). Like a bookmaker's adjustment for a
    withdrawn runner: re-price top frag for the real lineup and scale each pending top-frag / group bet's odds
    by (chance as offered) / (chance with the real lineup). Bets on the missing players are refunded anyway."""
    sc = json.loads(m["scouting"])
    gone = {r.casefold() for r in absent}
    real = dict(sc, teammates=[t for t in sc.get("teammates", []) if t["riot_id"].casefold() not in gone])
    if PlayerStats.from_dict(sc["host"]).riot_id.casefold() in gone:
        return  # the player being bet on wasn't in it: nothing sensible to re-price
    new = {o["riot"]: o["p_model"] for o in price_markets(real, m["mode"])["topfrag"]}
    old = {o["riot"]: o.get("p_model", o["p"]) for o in markets["topfrag"]}
    factor = {r: old[r] / new[r] for r in old if r in new and new[r] > 0}
    changed = 0
    with db.Tx() as c:
        for b in c.execute("SELECT * FROM bets WHERE match_id = ? AND status = 'pending' "
                           "AND market IN ('topfrag', 'combo')", (m["id"],)).fetchall():
            riot = b["side"] if b["market"] == "topfrag" else odds.split_combo(b["side"])[1]
            if riot in factor:
                c.execute("UPDATE bets SET odds = ? WHERE id = ?", (round(max(b["odds"] * factor[riot], 1.01), 2), b["id"]))
                changed += 1
    log.info("Match %s: %s not in the game; re-priced %d top-frag bet(s): %s", m["id"], absent, changed,
             {short(r) if r != odds.OTHER else "other": round(f, 3) for r, f in factor.items()})


def main_agents(riot_ids: list[str], details: list[MatchDetail]) -> dict[str, str]:
    """Each player's most-played agent across these recent games (shown as 'Name (Jett main)')."""
    out = {}
    for rid in riot_ids:
        picks = Counter(p.agent for d in details if (p := d.find_player(rid)) and p.agent and p.agent != "?")
        if picks:
            out[rid] = picks.most_common(1)[0][0]
    return out


# ---------- embeds ----------

def markets_embed(m, bets=None) -> discord.Embed:
    mk = json.loads(m["markets"])
    scouting = json.loads(m["scouting"])
    host = short(m["host_riot"])
    overwatch = m["game"] == "overwatch"
    mode = ("" if m["mode"] == "any" else f" {m['mode']}") + (" Overwatch" if overwatch else "")
    is_open = ui.betting_open(m)
    if is_open:
        title, desc = f"🎲 Bets open: {host}'s next{mode} game", (
            f"Betting closes {ts(datetime.fromisoformat(m['lock_at']))}. Anyone can bet, playing or not: press "
            "**Win**, **Loss**" + ("" if overwatch else ", a top fragger or **👥 Group bet**")
            + " below to open your private bet slip." + ("" if overwatch else
                                                "\nBoth bets are optional and pay out separately.")
            + "\nOdds shift a little as people bet; every bet keeps the odds it was placed at."
            + ("" if overwatch else "\nIf a listed teammate isn't actually in the game, bets on them are refunded "
                                    "and the other top-frag odds are adjusted for the real lineup."))
    else:
        title = {"locked": f"🔒 Betting closed: {host}'s game is on",
                 "resolved": f"🏁 Settled: {host}'s game", "cancelled": f"🚫 Cancelled: {host}'s game",
                 "awaiting": f"🏁 Result paid: {host}'s game · top frag info not received yet"
                 }.get(m["status"], f"🔒 Betting closed: {host}'s game")
        desc = "Odds are decimal payouts (×2.00 doubles your stake)."
    e = discord.Embed(title=title, description=desc, color=GOLD if is_open else GREY)

    pools: dict[tuple[str, str], int] = {}
    for b in bets or []:
        pools[(b["market"], b["side"])] = pools.get((b["market"], b["side"]), 0) + b["amount"]
    pool = (lambda market, side: f" · {pools.get((market, side), 0):,} bet") if bets is not None else (lambda *_: "")
    e.add_field(name="Match result",
                value=f"**Win** ×{mk['win']['win']:.2f}{pool('win', 'win')}\n"
                      f"**Loss** ×{mk['win']['loss']:.2f}{pool('win', 'loss')}", inline=True)
    if overwatch:  # Win/Loss only: priced from, and settled by, their Overwatch career profile
        if base := scouting.get("ow_baseline"):
            names = {"quickplay": "Quick Play", "competitive": "Competitive"}
            rows = [f"{names.get(mode, mode)}: **{w:,}–{l:,}** ({w / p * 100 if p else 50:.0f}% wins)"
                    for mode, (p, w, l) in base.items() if p]
            e.add_field(name="🎯 Record", value="\n".join(rows) or "No games yet", inline=True)
            e.set_footer(text=f"Match #{m['id']} · bets are on their next Quick Play or Competitive game (not Arcade) · "
                              "pays out a few minutes after it ends, from their Overwatch career profile")
        else:
            e.set_footer(text=f"Match #{m['id']} · settles automatically when the match ends")
        return e
    tf_lines = [f"{odds.option_label(o, mk)} ×{o['odds']:.2f}{pool('topfrag', o['riot'])}" for o in mk["topfrag"]]
    e.add_field(name=f"Top frag on {host}'s team (highest ACS)", value="\n".join(tf_lines)[:1024], inline=True)
    groups = db.group_pools(bets or [])
    if groups or is_open:
        lines = [f"**{odds.bet_label(mk, m['host_riot'], mkt, side)}**: {pool_status(p)}"
                 for (mkt, side), p in sorted(groups.items(), key=lambda kv: -kv[1]["total"])]
        if is_open:
            lines.append(group_rules().replace("👥 **Group bets:** ", "").capitalize() + " Both parts must be right, "
                         "and it pays both odds multiplied. Press **👥 Group bet**.")
        e.add_field(name="👥 Group bets (result + top frag)", value="\n".join(lines)[:1024], inline=False)
    host = PlayerStats.from_dict(scouting["host"])
    scout_lines = [f"🎯 {stat_line(host)}"]
    if form := scouting.get("form"):
        streak = form["streak"]
        streak_txt = f" · {'🔥 W' if streak > 0 else '🧊 L'}{abs(streak)} streak" if abs(streak) >= 2 else ""
        lobby = (f" · lobbies avg {odds_rank_name(form['opp_tier'])}" if form.get("opp_tier") else "")
        perf = f" · {form['kd']:.2f} K/D · {form['acs'] or 0:.0f} ACS" if form.get("kd") is not None else ""
        source = " (games the bot saw)" if form.get("source") == "bot history" else ""
        scout_lines.append(f"📈 Last {form['games']}{source}: **{form['wins']}–{form['losses']}**{streak_txt}"
                           f"{perf}{lobby}")
    if (rec := scouting.get("map_record")) and scouting.get("map"):
        scout_lines.append(f"🗺️ {scouting['map']}: **{rec[0]}–{rec[1] - rec[0]}** recently")
    if m["mode"] in odds.MODE_ROUNDS:
        scout_lines.append(f"⏱️ First to {odds.MODE_ROUNDS[m['mode']]} rounds: upsets are likelier, so the odds "
                           "sit closer to even")
    if cal := scouting.get("calibration"):
        scout_lines.append(f"🧠 Odds tuned from {cal['games']} finished games")
    scout_lines += [f"🟦 {stat_line(PlayerStats.from_dict(p))}" for p in scouting.get("teammates", [])]
    if not scouting.get("opponents"):
        scout_lines.append("_No opponents given; winner odds use the host's win rate only. "
                           "Opponents are revealed automatically when the game is settled._")
    e.add_field(name="Scouting report", value="\n".join(scout_lines)[:1024], inline=False)
    e.set_footer(text=f"Match #{m['id']} · settles automatically from tracker.gg")
    return e


def threat(p: PlayerStats) -> str:
    score = p.kd * 0.6 + p.acs / 220 * 0.4
    if score >= 1.15:
        return "🔥 Threat"
    if score <= 0.9:
        return "🎯 Weak link"
    return "⚖️ Even"


def enemy_recap_embed(host_riot: str, opps: list[PlayerStats]) -> discord.Embed:
    """Pre-game recap of every player on the enemy team, strongest first."""
    opps = sorted(opps, key=lambda p: -(p.kd * 0.6 + p.acs / 220 * 0.4))
    tiers = [p.tier for p in opps if p.tier]
    avg_rank = odds_rank_name(sum(tiers) / len(tiers)) if tiers else "Unranked"
    avg_kd = sum(p.kd for p in opps) / len(opps)
    e = discord.Embed(
        title=f"🟥 Enemy team recap: {short(host_riot)}'s next game",
        description=f"Average rank **{avg_rank}** · average K/D **{avg_kd:.2f}**",
        color=RED,
    )
    for p in opps:
        mains = f"\nMains: {', '.join(p.top_agents)}" if p.top_agents else ""
        e.add_field(
            name=f"{p.riot_id} — {p.rank} · {threat(p)}",
            value=(f"K/D **{p.kd:.2f}** · Win **{p.win_pct:.0f}%** · ACS **{p.acs:.0f}** · HS **{p.hs_pct:.0f}%**\n"
                   f"{p.kills_per_match:.1f} kills/game · {p.matches} games{mains}"),
            inline=False,
        )
    e.set_footer(text="Season stats from tracker.gg")
    return e


def odds_rank_name(avg_tier: float) -> str:
    return VALORANT_TIERS[min(max(round(avg_tier), 1), len(VALORANT_TIERS)) - 1]


def bettor_breakdown(m, bets) -> str:
    """Every bettor: each bet and how it went, their net result on this game, and their balance now."""
    mk = json.loads(m["markets"])
    icon = {"won": "✅", "lost": "❌", "push": "↩️", "refunded": "↩️", "pending": "⏳"}
    per: dict[int, list] = {}
    groups = db.group_pools(bets)
    for b in bets:
        per.setdefault(b["user_id"], []).append(b)
    settled_net = lambda bs: sum(b["payout"] - b["amount"] for b in bs if b["status"] != "pending")
    lines = []
    for uid, bs in sorted(per.items(), key=lambda kv: -settled_net(kv[1])):
        parts = []
        for b in bs:
            label = odds.bet_label(mk, m["host_riot"], b["market"], b["side"])
            if b["grp"]:
                bonus = groups.get((b["market"], b["side"]), {}).get("bonus", 0)
                label = f"👥 {label}" + (f" (+{bonus}% group bonus)" if bonus else "")
            outcome = {"won": f"won {b['payout']:,}", "lost": "lost",
                       "pending": "on hold: top frag info not received yet"}.get(b["status"], "refunded")
            parts.append(f"{icon.get(b['status'], '•')} {label} · {b['amount']:,} → {outcome}")
        net = settled_net(bs)
        held = sum(b["amount"] for b in bs if b["status"] == "pending")
        lines.append(f"<@{uid}>\n" + "\n".join(parts)
                     + f"\n**{'+' if net >= 0 else ''}{net:,}** this game · balance **{db.get_user(uid)['balance']:,}**"
                     + (f"\n⏳ **{held:,} coins on hold** (not in your balance yet): {HELD_NOTE}" if held else ""))
    return "\n\n".join(lines) or "Nobody bet on this game."


# Why some coins aren't back yet, wherever pending bets show up.
HELD_NOTE = (f"waiting for the top frag info (ACS from tracker.gg), which hasn't been received yet. They pay out "
             f"as soon as it arrives, or are refunded in full after {MATCH_TIMEOUT.total_seconds() / 3600:g} h "
             "if it never does.")


def result_embeds(m, markets, detail: MatchDetail, host, outcome, results, bets) -> list[discord.Embed]:
    won = outcome["won"]
    my_team = detail.teams.get(host.team)
    other = next((t for tid, t in detail.teams.items() if tid != host.team), None)
    score = (f"{my_team.rounds_won}–{other.rounds_won}"
             if my_team and other and my_team.rounds_won is not None and other.rounds_won is not None else "")
    verdict = "Victory" if won else "Defeat" if won is False else "Draw"
    host_line = (f"{short(m['host_riot'])} · {host.agent} · {host.kills}/{host.deaths}/{host.assists}"
                 + (f" · {host.acs:.0f} ACS" if host.acs is not None else ""))
    e = discord.Embed(
        title=f"🏁 {short(m['host_riot'])}: {verdict} {score} on {detail.map}",
        description=(f"{host_line}\n\n**Bets**\n{bettor_breakdown(m, bets)}")[:4000],
        color=GREEN if won else RED if won is False else GREY,
    )
    team_name = f"{short(m['host_riot'])}'s team"
    e.add_field(name="Who won", value={"win": f"✅ {team_name} won", "loss": f"❌ {team_name} lost",
                                       "push": "↩️ Draw, result bets refunded"}[results["win"]])
    if results["topfrag"] is None:
        tf_value = "⏳ Waiting for ACS from tracker.gg"
        e.set_footer(text=f"Match #{m['id']} · top frag bets pay out once ACS is available; otherwise they're "
                          f"refunded after {MATCH_TIMEOUT.total_seconds() / 3600:g} h")
    else:
        acs = (f"{outcome['topfrag_acs']} ACS" if outcome.get("topfrag_acs") else "the highest ACS") + \
              (f" · {outcome['topfrag_kills']} kills" if outcome.get("topfrag_kills") is not None else "")
        if results["topfrag"] == "push":
            tied = outcome.get("topfrag_tied") or [outcome["topfrag_riot"]]
            tf_value = (f"↩️ Same ACS and kills, top frag bets refunded\n★ {', '.join(short(r) for r in tied)} "
                        f"with {acs}")
        else:
            other = " (someone else)" if results["topfrag"] == odds.OTHER else ""
            tf_value = f"★ **{short(outcome['topfrag_riot'])}**{other} with {acs}"
        e.set_footer(text=f"Match #{m['id']}")
    if absent := results.get("absent"):
        who = ", ".join(short(r) for r in absent)
        tf_value += (f"\n↩️ {who} {'weren' if len(absent) > 1 else 'wasn'}'t in this game: bets on them were "
                     "refunded, and the other top-frag odds were re-priced for the real lineup")
    e.add_field(name="Top frag (highest ACS)", value=tf_value)

    board = discord.Embed(title=f"Scoreboard — {detail.map}", color=GREY)
    for team_id in sorted({p.team for p in detail.players}, key=lambda t: t != host.team):
        players = sorted((p for p in detail.players if p.team == team_id), key=lambda p: (-p.score, -p.kills))
        rows = [f"`{p.agent[:9]:<9}` **{p.riot_id}**{' ★' if p.riot_id == outcome.get('topfrag_riot') else ''} "
                f"{p.kills}/{p.deaths}/{p.assists}" + (f" · {p.acs:.0f} ACS" if p.acs is not None else "")
                + f" · {p.rank}" for p in players]
        board.add_field(name="🟦 Your team" if team_id == host.team else "🟥 Opponents",
                        value="\n".join(rows)[:1024] or "—", inline=False)
    return [e, board]


# ---------- helpers ----------

async def scout(ids: list[str], keep_on_error: bool = False,
                known_ranks: dict[str, str] | None = None) -> tuple[list[PlayerStats], list[str]]:
    """Look up several players. keep_on_error: if tracker.gg can't load someone (but they exist),
    keep them with average stats instead of dropping them (used for teammates, so they stay bettable).
    known_ranks: ranks already known from the live game (Overwolf), used when tracker.gg has none."""
    known = {k.casefold(): v for k, v in (known_ranks or {}).items()}
    results = await asyncio.gather(*(bot.tracker.get_profile(r) for r in ids), return_exceptions=True)
    ok, failed = [], []
    for rid, res in zip(ids, results):
        rank = known.get(rid.casefold())
        if isinstance(res, PlayerStats):
            if not res.tier and rank:
                res.rank, res.tier = rank, tier_index(rank)
            ok.append(res)
        elif keep_on_error and not isinstance(res, PlayerNotFound):
            ok.append(PlayerStats(riot_id=rid, rank=rank or "Unranked", tier=tier_index(rank)))
            if not known_ranks:
                failed.append(f"{rid} (stats unavailable, using average stats)")
        else:
            failed.append(f"{rid} ({res})")
    return ok, failed


def pick_match(inter: discord.Interaction, host: discord.Member | None):
    if host:
        m = db.active_match_for_host(host.id)
        return m, None if m else f"{host.display_name} has no active match."
    ms = db.active_matches(inter.channel_id)
    if not ms:
        return None, "No active match in this channel. Betting opens by itself when a linked player's game starts."
    if len(ms) > 1:
        return None, "Several matches are active here. Pick one with the `host` option."
    return ms[0], None


# ---------- account commands ----------

@bot.tree.command(description="Link your Discord account to your Riot ID (Name#TAG)")
@app_commands.describe(riot_id="Your Riot ID, e.g. Player#NA1")
async def link(inter: discord.Interaction, riot_id: str):
    rid = parse_riot_id(riot_id)
    if not rid:
        return await inter.response.send_message("That doesn't look like a Riot ID (Name#TAG).", ephemeral=True)
    owner = db.riot_owner(rid)
    if owner and owner != inter.user.id:
        return await inter.response.send_message("That Riot ID is already linked to another Discord account.",
                                                 ephemeral=True)
    await inter.response.defer(ephemeral=True)
    try:
        stats = await bot.tracker.get_profile(rid)
    except PlayerNotFound:
        return await inter.followup.send(f"Couldn't find **{rid}** on tracker.gg. Check the spelling and tag.", ephemeral=True)
    except TrackerError as e:
        # tracker.gg is down or the key isn't active yet: save the Riot ID anyway, stats load later.
        db.set_riot_id(inter.user.id, rid)
        return await inter.followup.send(f"Linked to **{rid}**. Couldn't load stats right now ({e}); "
                                         "they'll load when betting opens.", ephemeral=True)
    db.set_riot_id(inter.user.id, rid)
    await inter.followup.send(f"Linked to {stat_line(stats)}", ephemeral=True)


BATTLETAG_RE = re.compile(r"^[^\s#@<>`]{2,12}#\d{3,6}$")


@bot.tree.command(name="link-overwatch", description="Link your Overwatch BattleTag (Name#1234) for Overwatch betting")
@app_commands.describe(battletag="Your BattleTag, e.g. Player#1234")
async def link_overwatch(inter: discord.Interaction, battletag: str):
    tag = re.sub(r"\s*#\s*", "#", battletag.strip())
    if not BATTLETAG_RE.match(tag):
        return await inter.response.send_message("That doesn't look like a BattleTag (Name#1234).", ephemeral=True)
    owner = db.battletag_owner(tag)
    if owner and owner != inter.user.id:
        return await inter.response.send_message("That BattleTag is already linked to another Discord account.",
                                                 ephemeral=True)
    await inter.response.defer(ephemeral=True)
    await inter.followup.send(await link_battletag(inter.user.id, tag), ephemeral=True)


async def link_battletag(user_id: int, tag: str) -> str:
    """Save a BattleTag and find its public career profile. Returns a message for the person."""
    try:
        ow_id = await bot.ow.find_player(tag)
        recs = await bot.ow.records(ow_id)
    except ow.OverwatchError as e:
        db.set_battletag(user_id, tag)
        return (f"Linked to **{tag}**, but the bot can't read the Overwatch profile yet: {e}. It tries again each "
                "time the game is launched.")
    db.set_battletag(user_id, tag, ow_id)
    both = ow.total(recs)
    return (f"Linked to **{tag}**: {both.won:,}–{both.lost:,} in Quick Play + Competitive "
            f"({both.win_pct:.0f}% wins). When Discord shows Overwatch running, betting opens by itself on the next "
            "Quick Play or Competitive game and pays out a few minutes after it ends.")


@bot.tree.command(name="link-for", description="Link a friend's Riot ID / BattleTag for them (server managers)")
@app_commands.describe(user="Who to link", riot_id="Their Riot ID, e.g. Player#NA1",
                       battletag="Their Overwatch BattleTag, e.g. Player#1234 (optional)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def link_for(inter: discord.Interaction, user: discord.Member, riot_id: str | None = None,
                   battletag: str | None = None):
    if user.bot:
        return await inter.response.send_message("Bots can't be linked.", ephemeral=True)
    if not riot_id and not battletag:
        return await inter.response.send_message("Give a Riot ID, a BattleTag, or both.", ephemeral=True)
    rid = parse_riot_id(riot_id) if riot_id else None
    if riot_id and not rid:
        return await inter.response.send_message("That doesn't look like a Riot ID (Name#TAG).", ephemeral=True)
    tag = re.sub(r"\s*#\s*", "#", battletag.strip()) if battletag else None
    if tag and not BATTLETAG_RE.match(tag):
        return await inter.response.send_message("That doesn't look like a BattleTag (Name#1234).", ephemeral=True)
    if rid and (owner := db.riot_owner(rid)) and owner != user.id:
        return await inter.response.send_message("That Riot ID is already linked to another Discord account.",
                                                 ephemeral=True)
    if tag and (owner := db.battletag_owner(tag)) and owner != user.id:
        return await inter.response.send_message("That BattleTag is already linked to another Discord account.",
                                                 ephemeral=True)
    await inter.response.defer(ephemeral=True)
    notes = []
    if rid:
        db.set_riot_id(user.id, rid)
        notes.append(f"Riot ID **{rid}**")
    if tag:
        notes.append(await link_battletag(user.id, tag))
    await inter.followup.send(
        f"Linked {user.mention}: " + "\n".join(notes) + "\nThey can change it with /link or remove it with /unlink.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="overwolf-link",
                  description="Your private address for the HomeAssistant Game Events Overwolf app")
async def overwolf_link(inter: discord.Interaction):
    host = os.getenv("OVERWOLF_WEBHOOK_HOST") or "valbet"
    url = f"http://{host}.local:{dashboard.PORT}/api/ha/{db.overwolf_key(inter.user.id)}"
    await inter.response.send_message(
        "**Send your games to the bot automatically** (optional, gives full teammate names and instant Overwatch "
        "results):\n1. Install **HomeAssistant Game Events** from the Overwolf store.\n"
        f"2. Paste this into its **Webhook URL** and press Save:\n`{url}`\n"
        "It only reports **your own** games. Keep it private: anyone with it could send results as you.\n"
        "-# Not on the bot's home network? You also need the Tailscale steps the bot owner sends you.",
        ephemeral=True)


@bot.tree.command(description="Remove your linked Riot ID and BattleTag (your coins stay)")
async def unlink(inter: discord.Interaction):
    if db.active_match_for_host(inter.user.id):
        return await inter.response.send_message("Betting is open on your current game. Try again after it ends.",
                                                 ephemeral=True)
    done = db.unlink(inter.user.id)
    await inter.response.send_message(
        "Unlinked. The bot won't follow your games any more; your coins are kept." if done
        else "You don't have anything linked.", ephemeral=True)


@bot.tree.command(name="scout", description="Look up a player's Valorant stats on tracker.gg")
async def scout_cmd(inter: discord.Interaction, riot_id: str):
    rid = parse_riot_id(riot_id)
    if not rid:
        return await inter.response.send_message("That doesn't look like a Riot ID (Name#TAG).", ephemeral=True)
    await inter.response.defer()
    try:
        await inter.followup.send(stat_line(await bot.tracker.get_profile(rid)))
    except TrackerError as e:
        await inter.followup.send(f"Couldn't scout **{rid}**: {e}")


@bot.tree.command(description="Check a coin balance")
async def balance(inter: discord.Interaction, user: discord.Member | None = None):
    user = user or inter.user
    u = db.get_user(user.id)
    riot = f" · {u['riot_id']}" if u["riot_id"] else ""
    held = sum(b["amount"] for b in db.pending_bets_for_user(user.id))
    await inter.response.send_message(f"💰 **{user.display_name}** has **{u['balance']:,}** coins{riot}"
                                      + (f" · ⏳ {held:,} more in bets that haven't paid out yet (/mybets)"
                                         if held else ""))


@bot.tree.command(description="Give some of your coins to another user")
async def give(inter: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1]):
    if user.id == inter.user.id or user.bot:
        return await inter.response.send_message("Pick someone else.", ephemeral=True)
    try:
        db.transfer(inter.user.id, user.id, amount)
    except ValueError as e:
        return await inter.response.send_message(str(e), ephemeral=True)
    await inter.response.send_message(f"💸 {inter.user.mention} gave {user.mention} **{amount:,}** coins.")


@bot.tree.command(description="Richest bettors")
async def leaderboard(inter: discord.Interaction):
    rows = db.leaderboard()
    lines = [f"**{i}.** <@{r['discord_id']}> — {r['balance']:,}" for i, r in enumerate(rows, 1)]
    e = discord.Embed(title="🏆 Leaderboard", description="\n".join(lines) or "Nobody yet.", color=GOLD)
    await inter.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(description="How accurate the bot's Win/Loss odds have been so far")
async def accuracy(inter: discord.Interaction):
    preds = db.predictions()
    e = discord.Embed(title="🧠 How good are the odds?", color=GOLD)
    if not preds:
        e.description = ("No finished games with saved predictions yet. Every game betting opens on from now on "
                         "is checked against its result.")
        return await inter.response.send_message(embed=e)
    n = len(preds)
    brier = sum((p - y) ** 2 for p, y in preds) / n
    correct = sum((p > 0.5) == y for p, y in preds if p != 0.5)
    called = sum(1 for p, _ in preds if p != 0.5)
    e.description = (f"**{n}** finished games with a prediction.\n"
                     f"Favourite won **{correct}/{called}**" + (f" ({correct / called:.0%})" if called else "")
                     + f"\nScore **{brier:.3f}** (lower is better; always saying 50/50 scores 0.250)")
    buckets = {}
    for p, y in preds:
        key = min(int(p * 10), 9)
        buckets.setdefault(key, []).append((p, y))
    rows = [f"Said {sum(p for p, _ in b) / len(b):.0%} → won {sum(y for _, y in b) / len(b):.0%} ({len(b)} games)"
            for _, b in sorted(buckets.items())]
    e.add_field(name="Predicted vs. what happened", value="\n".join(rows)[:1024], inline=False)
    cal = odds.fit_calibration(preds)
    e.add_field(name="Self-tuning", value=(f"On: learned from {cal['games']} games" if cal else
                                           f"Starts after {odds.CAL_MIN_GAMES} games ({n} so far)"), inline=False)
    await inter.response.send_message(embed=e)


# ---------- betting ----------

@bot.tree.command(description="Post the betting control panel here (automatic bets post in this channel)")
async def panel(inter: discord.Interaction):
    try:
        await inter.channel.send(embed=ui.panel_embed())
    except discord.HTTPException:
        return await inter.response.send_message("I can't post in this channel. Check my permissions here.",
                                                 ephemeral=True)
    db.set_meta("panel_channel", str(inter.channel_id))  # automatic betting posts here too
    await inter.response.send_message("Control panel posted. Pin it so it's easy to find. Automatic betting "
                                      "(when Discord shows someone starting a match) will post here too.",
                                      ephemeral=True)


@bot.tree.command(description="Open your private bet slip for the match in this channel")
async def bet(inter: discord.Interaction):
    await ui.with_match(inter, ui.open_slip, statuses=("open",),
                        none_msg="No betting is open in this channel right now.")


@bot.tree.command(description="Your unsettled bets")
async def mybets(inter: discord.Interaction):
    rows = db.pending_bets_for_user(inter.user.id)
    if not rows:
        return await inter.response.send_message("You have no open bets.", ephemeral=True)
    where = {"open": "betting still open", "locked": "game in progress",
             "awaiting": "⏳ game over, top frag info not received yet"}
    lines = []
    for b in rows:
        status = db.get_match(b["match_id"])["status"]
        lines.append(f"#{b['match_id']} · {b['amount']:,} on "
                     f"**{odds.bet_label(json.loads(b['markets']), b['host_riot'], b['market'], b['side'])}** "
                     f"(×{b['odds']:.2f} → pays {int(b['amount'] * b['odds']):,}"
                     + (" + group bonus" if b["grp"] else "") + f") · {where.get(status, status)}")
    total = sum(b["amount"] for b in rows)
    note = f"\n\n**{total:,} coins** are in these bets, so they're not in your balance right now."
    if any(db.get_match(b["match_id"])["status"] == "awaiting" for b in rows):
        note += f"\n⏳ The ones marked on hold are {HELD_NOTE}"
    await inter.response.send_message(("\n".join(lines) + note)[:2000], ephemeral=True)


# ---------- /match ----------

match_group = app_commands.Group(name="match", description="See the current game's bets")


@match_group.command(name="status", description="Show markets, odds and money in the pool")
async def match_status(inter: discord.Interaction, host: discord.Member | None = None):
    m, err = pick_match(inter, host)
    if err:
        return await inter.response.send_message(err, ephemeral=True)
    await inter.response.send_message(embed=markets_embed(m, db.bets_for_match(m["id"])))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot.run(DISCORD_TOKEN, log_handler=None)
