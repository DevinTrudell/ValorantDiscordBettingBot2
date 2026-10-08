"""Valorant betting bot: bet play-money coins on a friend's upcoming Valorant game.

Flow:
  1. /link Name#TAG            link your Discord account to your Riot ID
  2. /panel                    posts the info message and sets the betting channel (nothing is manual)
  3. betting opens by itself   when a linked player's Discord status (or the Overwolf app) shows a match
                               starting; bets are Win/Loss and top frag on the player's team
  4. the "Bets open" post      Win / Loss / top-frag menu / Group bet right on it; each opens a private
                               slip with that pick filled in; anyone in the server can bet
  5. the bot checks HenrikDev (tracker.gg as a backup); when the game shows up it settles every bet and
     posts the full scoreboard, including the opponents' ranks and stats.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # before importing db, which reads env vars

import asyncio
import io
import json
import re
import unicodedata
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
import ui
import icons
import overwolf_data
from tracker import (VALORANT_TIERS, MatchDetail, PlayerNotFound, PlayerStats, TrackerClient, TrackerError,
                     parse_riot_id, tier_index)

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
# HenrikDev (HENRIK_API_KEY) is the main data source; tracker.gg is an optional backup for when it's down.
TRACKER_API_KEY = os.getenv("TRACKER_API_KEY", "").strip()
GUILD_ID = os.getenv("GUILD_ID")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
MATCH_TIMEOUT = timedelta(hours=float(os.getenv("MATCH_TIMEOUT_HOURS", "3")))
OPEN_TIMEOUT = 45  # seconds: opening betting (all the stat lookups) must finish within this
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

# What Valorant's Discord status says in each phase. Exact wording isn't documented, so these are
# deliberately broad; presence_log.jsonl records the real text so they can be tightened later.
PREGAME_RE = re.compile(r"agent select|pre-?game|character select|selecting agent|locking in", re.I)
INGAME_RE = re.compile(r"\b\d{1,2}\s*[-–:]\s*\d{1,2}\b|in[ -]game|in match|round \d", re.I)
SKIP_MODE_RE = re.compile(r"deathmatch|the range|practice|custom|replication|escalation|skirmish|2v2|snowball", re.I)
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
    hs = f" · {p.hs_pct:.0f}% HS" if p.hs_pct else ""  # (HenrikDev profiles don't include headshot %)
    return (f"{icons.rank(p.rank)} **{p.riot_id}** · {p.kd:.2f} K/D · {p.win_pct:.0f}% WR · "
            f"{p.acs:.0f} ACS{hs} ({p.matches} games)")


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
        self._polled: dict[int, datetime] = {}      # match_id -> last finished-match check (Valorant)

    # ---------- Discord status tracking (experiment: can we see when a match starts?) ----------

    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if not db.is_linked(after.id):  # only players who opted in with /link
            return
        acts = [activity_dict(a) for a in after.activities if "valorant" in (getattr(a, "name", "") or "").lower()]
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
        if p.get("game") not in (None, "valorant"):
            return {"ok": True, "action": "ignored (only Valorant is supported)"}
        me, mates, opps, ranks = overwolf_data.lineup(p)
        if not me and p.get("owner"):  # the app only sends your name at launch: a personal key says who it is
            me = db.linked_riot_id(p["owner"])
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
        # Fill gaps from Discord: the app sometimes sends no mode/map, and linked friends in the same Valorant
        # party are teammates even if the app didn't mark them as such.
        status, party, party_size = self.valorant_status((self.presence.get(host_id) or {}).get("activities") or [])
        if party:
            for uid, pr in self.presence.items():
                r = db.linked_riot_id(uid) if uid != host_id and self.valorant_text(pr["activities"])[1] == party else None
                if r and r.casefold() not in {x.casefold() for x in mates}:
                    opps = [o for o in opps if o.casefold() != r.casefold()]
                    mates.append(r)
        if mode == "any" and status_mode(status):
            mode = status_mode(status)
        log.info("Overwolf %s from %s: %d teammates, %d opponents", event, me, len(mates), len(opps))

        if event == "match_end":
            m = m or db.awaiting_match_for_host(host_id)  # Win/Loss may already be paid from Discord status
            if not m:
                return {"ok": True, "action": "no open bet to settle"}
            detail = overwolf_data.final_detail(p, me, ranks)
            if not detail:
                return {"ok": False, "reason": "End-of-game scoreboard was incomplete; the match history check will settle it."}
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
        if other := self.page_for_same_game(host_id, me, party):
            return {"ok": True, "action": f"not opening: same game as match {other['id']}"}
        channel_id = self.betting_channel_id()
        if not channel_id:
            return {"ok": False, "reason": "Run /panel in your betting channel first."}
        self.auto_opened[host_id] = now()
        try:
            match_id, problems = await self.open_match(
                guild_id=int(GUILD_ID or 0), channel_id=channel_id, opener_id=self.user.id, host_id=host_id,
                host_name=name, opponents=", ".join(opps) or None, teammates=", ".join(mates) or None,
                mode=mode, window=AUTO_WINDOW, known_ranks=ranks,
                map_name=overwolf_data.MAPS.get(p.get("map") or "") or status_map(status) or None)
        except ValueError as e:
            if not db.active_match_for_host(host_id):
                self.auto_opened.pop(host_id, None)  # didn't open: match start can try again
            return {"ok": False, "reason": str(e)}
        if ch := await self.channel(channel_id):
            when = "is in agent select" if event == "pregame" else "just started a match"
            await ch.send(f"🤖 Betting opened automatically: **{name}** {when}. Teams come straight from the game.",
                          allowed_mentions=discord.AllowedMentions.none())
        if problems:
            log.info("Match %s notes: %s", match_id, " · ".join(problems))
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
        has bet yet, so nobody's bet changes under them."""
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
            await self.refresh_market_message(m["id"])  # (no enemy recap post: opponents only feed the odds)

    async def settle_from_status(self, member: discord.Member, ours: int, theirs: int, text: str):
        """A game just ended in the player's Discord status ("Swiftplay (Ascent) 5 - 3", their team first):
        add it to their game history, and pay Win/Loss bets from the final score. Top-frag bets need the
        scoreboard, so they wait for Overwolf/HenrikDev."""
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
        if db.get_meta(f"result_msg:{m['id']}"):  # the game's post-game message is already up: it has this
            return
        await self.post_result(m, ch, pending_result_embed(m, player, won, score, map_name))

    async def post_result(self, m, ch, embed: discord.Embed):
        """Each game gets one post-game message: later updates (the scoreboard) edit it instead of adding more."""
        if prev := db.get_meta(f"result_msg:{m['id']}"):
            try:
                await ch.get_partial_message(int(prev)).edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                return
            except discord.HTTPException:
                pass  # deleted: post a new one
        msg = await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        db.set_meta(f"result_msg:{m['id']}", str(msg.id))

    @staticmethod
    def valorant_text(acts: list[dict]) -> tuple[str, str | None]:
        """All the text Valorant's own status shows (not Medal & co.), and its party ID."""
        return BetBot.valorant_status(acts)[:2]

    def page_for_same_game(self, host_id: int, riot: str | None, party: str | None):
        """Another player's open bet page for this same game (they're listed as a teammate there, or in the same
        Valorant party): one page per game, for whoever's game opened first."""
        for m in db.active_matches():
            if m["host_id"] == host_id or m["status"] not in ("open", "locked"):
                continue
            mates = {t["riot_id"].casefold() for t in json.loads(m["scouting"]).get("teammates", [])}
            host_party = self.valorant_text((self.presence.get(m["host_id"]) or {}).get("activities") or [])[1]
            if (riot and riot.casefold() in mates) or (party and host_party == party):
                return m
        for hid in self._opening - {host_id}:  # a party mate's page is being built right now
            if party and self.valorant_text((self.presence.get(hid) or {}).get("activities") or [])[1] == party:
                return {"id": "being opened"}
        return None

    @staticmethod
    def valorant_status(acts: list[dict]) -> tuple[str, str | None, int | None]:
        """(status text, party ID, party size) from Valorant's own status."""
        for a in acts:
            if (a.get("name") or "").strip().lower() == "valorant":
                text = " · ".join(a[f] for f in ("details", "state", "large_image_text", "small_image_text") if a.get(f))
                party = (a.get("party") or {}).get("id")
                size = ((a.get("party") or {}).get("size") or [None])[0]
                if text or party:
                    return text, party, size
        return "", None, None

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
        if other := self.page_for_same_game(member.id, db.linked_riot_id(member.id), party):
            log.info("Not opening for %s: same game as match %s", member.display_name, other["id"])
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
                map_name=status_map(text), party_size=self.valorant_status(acts)[2])
        except ValueError as e:  # not linked, already has a match open, etc.
            if not db.active_match_for_host(member.id):
                self.auto_opened.pop(member.id, None)
            log.info("Auto-open skipped for %s: %s", member.display_name, e)
            return
        log.info("Auto-opened match %s for %s (%s; %s)", match_id, member.display_name, phase, text)
        if ch := await self.channel(int(channel_id)):
            when = "is in agent select" if phase == "pregame" else "just started a match"
            await ch.send(f"🤖 Betting opened automatically: Discord shows **{member.display_name}** {when}.",
                          allowed_mentions=discord.AllowedMentions.none())
        if problems:  # behind-the-scenes notes (e.g. a data source unavailable): for the log, not the channel
            log.info("Match %s notes: %s", match_id, " · ".join(problems))

    async def setup_hook(self):
        self.tree.add_command(match_group)
        self.add_dynamic_items(ui.SlipButton, ui.TopFragButton, ui.TopFragSelect, ui.CancelBetsButton)  # the controls on each "Bets open" post survive restarts
        self.add_dynamic_items(ui.RetiredPanelButton)  # old panels' Open/Close betting explain how it works now
        if GUILD_ID:  # guild sync is instant; global sync can take up to an hour
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.Forbidden:  # not added to that server yet: commands appear after the next restart there
                log.warning("Not in server %s yet: add the bot there, then restart it to load the commands", GUILD_ID)
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
        await self.henrik.close()
        await super().close()

    async def on_guild_join(self, guild: discord.Guild):
        if GUILD_ID and guild.id == int(GUILD_ID):  # just added to the home server: load the commands there now
            await self.tree.sync(guild=guild)
            log.info("Added to %s: commands loaded", guild.name)

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        if not getattr(self, "_icons_started", False):  # rank/agent icons as the bot's own emojis (once)
            self._icons_started = True
            asyncio.create_task(icons.ensure(self))

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
                await self.post_bets_closed(m, ch)

        if now() - opened > MATCH_TIMEOUT:
            n = db.cancel_match(m["id"])
            if n is None:  # already settled/cancelled elsewhere
                return
            await self.refresh_market_message(m["id"])
            if ch:
                why = ("the top frag info never arrived" if m["status"] == "awaiting"
                       else "no finished game was found")
                await ch.send(f"⌛ For **{short(m['host_riot'])}**'s game, {why} within "
                              f"{MATCH_TIMEOUT.total_seconds() / 3600:g} h, so the {n} bet(s) still on hold were "
                              "refunded in full. The coins are back in everyone's balance.")
            return

        if now() - opened < MIN_GAME_TIME:
            return
        # Only look for the finished match once Discord shows they're out of the game (it can't exist before).
        # No status info at all (activity hidden, or the bot restarted mid-game): a slow check every 10 minutes.
        last = self._polled.get(m["id"])
        if m["status"] == "locked":
            status = self.presence.get(m["host_id"])
            if status is not None:
                text = self.valorant_text(status["activities"])[0]
                if SCORE_RE.search(text) or PREGAME_RE.search(text):
                    return  # still in agent select / the match: don't check yet
            elif last and now() - last < timedelta(minutes=10):
                return
        self._polled[m["id"]] = now()

        detail = None
        try:
            if not self.henrik.enabled:
                raise henrik.HenrikError("HENRIK_API_KEY isn't set")
            detail = await self.henrik_finished_match(m)
        except henrik.HenrikError as e:
            if not TRACKER_API_KEY:
                log.warning("Match %s: HenrikDev: %s", m["id"], e)
                return
            log.info("Match %s: HenrikDev unavailable (%s), checking tracker.gg", m["id"], e)
            detail = await self.find_finished_match(m)  # TrackerError is logged by the watcher
        if detail:
            await self.settle(m, detail)

    async def henrik_finished_match(self, m) -> MatchDetail | None:
        """The host's newest finished game from HenrikDev, if it started around when betting opened.
        Raises HenrikError when HenrikDev can't be reached (the caller falls back to tracker.gg)."""
        opened = datetime.fromisoformat(m["opened_at"])
        details = await self.henrik.recent_details(m["host_riot"], 1)
        d = details[0] if details else None
        if not d or d.id == json.loads(m["scouting"]).get("henrik_baseline"):
            return None
        if d.timestamp and d.timestamp < opened - timedelta(minutes=10):
            return None  # an older game: the one bet on hasn't finished yet
        return d

    async def find_finished_match(self, m) -> MatchDetail | None:
        """(tracker.gg backup) The newest game in the host's history that wasn't there when betting opened."""
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
        """Open betting and post the markets. Raises ValueError with a user-facing message."""
        if host_id in self._opening:
            raise ValueError(f"Betting is already being opened for {host_name}.")
        self._opening.add(host_id)
        task = asyncio.create_task(self._open_match(host_id=host_id, host_name=host_name, **kw))
        try:
            done, _ = await asyncio.wait({task}, timeout=OPEN_TIMEOUT)
            if not done:  # a lookup is stuck: say where, and give up so the next game can open
                buf = io.StringIO()
                task.print_stack(file=buf)
                log.warning("Opening betting for %s took over %ss; stuck at:\n%s", host_name, OPEN_TIMEOUT, buf.getvalue())
                task.cancel()
                raise ValueError(f"Opening betting for {host_name} timed out.")
            return task.result()
        finally:
            self._opening.discard(host_id)

    async def _open_match(self, *, guild_id: int, channel_id: int, opener_id: int, host_id: int, host_name: str,
                          opponents: str | None, teammates: str | None, mode: str, window: float,
                          known_ranks: dict[str, str] | None = None,
                          map_name: str | None = None, party_size: int | None = None) -> tuple[int, list[str]]:
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
        # HenrikDev first (rank, season record and the last 10 full scoreboards), tracker.gg only as a backup.
        stats_problem = []
        live_rank = next((v for k, v in (known_ranks or {}).items() if k.casefold() == riot.casefold()), None)
        details, henrik_baseline, baseline = [], None, None

        async def henrik_history():
            if not self.henrik.enabled:
                return []
            try:
                return await self.henrik.recent_details(riot, odds.FORM_GAMES)
            except henrik.HenrikError as e:
                log.warning("HenrikDev history for %s: %s", riot, e)
                return []
        host_stats, details = await asyncio.gather(henrik_stats(riot, live_rank), henrik_history())
        henrik_baseline = details[0].id if details else None
        if (host_stats is None or not details) and TRACKER_API_KEY:
            try:
                if host_stats is None:
                    host_stats = await self.tracker.get_profile(riot)
                if not details:
                    recent = await self.tracker.get_recent_matches(riot, mode)
                    baseline = recent[0].id if recent else None
                    details = await self.recent_details(recent, odds.FORM_GAMES)
            except PlayerNotFound:
                raise ValueError(f"{riot} wasn't found. They should re-check their /link.") from None
            except TrackerError as e:
                stats_problem.append(f"tracker.gg backup failed for {riot}: {e}")
        if host_stats is None:  # no data source answered: open anyway with average stats so betting still works
            host_stats = PlayerStats(riot_id=riot, rank=live_rank or "Unranked", tier=tier_index(live_rank))
            stats_problem.append(f"Couldn't load {riot}'s rank; using average stats.")

        opp_ids, bad_opp = split_ids(opponents)
        tm_ids, bad_tm = split_ids(teammates)
        opp_ids = [r for r in opp_ids if r.casefold() != riot.casefold()]
        tm_ids = [r for r in tm_ids if r.casefold() != riot.casefold()]
        form = odds.recent_form(riot, details)
        history = odds.history_form([dict(g) for g in db.games_for(host_id)])
        if not form and history:  # no scoreboards loaded: use the games the bot watched itself
            form = history
        host_stats = odds.blend(host_stats, form)
        map_record = None
        if map_name:  # their record on this map, from whichever source has more games on it
            recs = [f["maps"].get(map_name) for f in (form, history) if f and f.get("maps")]
            map_record = max((r for r in recs if r), key=lambda r: r[1], default=None)

        # Teammates are only players known to be in this game: the full team from the Overwolf app, or linked
        # friends Discord shows in the same party right now. Nobody is guessed from recent games; everyone else
        # is covered by the "Other teammates" option.
        party_note = []
        if party_size:  # Discord knows the party size right now: never list more party members than that
            tm_ids = tm_ids[:max(party_size - 1, 0)] if not known_ranks else tm_ids
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
        problems = stats_problem + party_note + [f"Not a Riot ID: {x}" for x in bad_opp + bad_tm] + \
            [f"Lookup failed: {x}" for x in opp_fail + tm_fail]
        return match_id, problems

    async def _post_bets_open(self, match_id: int, ch):
        """Post the 'Bets open' message with its buttons, and close betting exactly when the window ends."""
        m = db.get_match(match_id)
        try:
            msg = await ch.send(view=markets_view(m))
        except discord.HTTPException:
            db.cancel_match(match_id)
            raise ValueError("Couldn't post in that channel (check the bot's Send Messages / Embed Links "
                             "permissions).") from None
        db.update_match(match_id, message_id=msg.id)
        task = asyncio.create_task(self._close_on_time(match_id))  # the once-a-minute watcher is a backstop
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

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
            await self.post_bets_closed(m, ch)
        return True

    async def post_bets_closed(self, m, ch):
        """Betting just closed: now everyone can see every bet and how much (amounts were secret until now)."""
        mk = json.loads(m["markets"])
        bets = [b for b in db.bets_for_match(m["id"]) if b["status"] != "refunded"]
        per: dict[int, list] = {}
        for b in bets:
            per.setdefault(b["user_id"], []).append(b)
        lines = []
        for uid, bs in per.items():
            parts = [f"{ui.bet_icon(m, mk, b['market'], b['side'])} **{b['amount']:,}** on "
                     f"{odds.bet_label(mk, m['host_riot'], b['market'], b['side'])} ×{b['odds']:.2f}" for b in bs]
            lines.append(f"<@{uid}> " + " · ".join(parts))
        body = ("\n".join(lines) if lines else "Nobody bet on this game.")[:3500]
        items = [ui.T(f"### 🔒 Betting closed · {game_names(m)}"), ui.Sep(), ui.T(body)]
        if bets:
            items += [ui.Sep(), ui.T(f"-# {sum(b['amount'] for b in bets):,} coins bet by {len(per)} · GLHF!")]
        await ch.send(view=ui.card(*items, color=GREY), allowed_mentions=discord.AllowedMentions.none())

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
        mine = {(b["market"], b["side"]) for b in db.bets_for_match(match_id)
                if b["user_id"] == user_id and b["status"] != "refunded"}
        if any(mkt == "topfrag" for mkt, _, _ in picks) and not (
                any(mkt == "win" for mkt, _ in mine) or any(mkt == "win" for mkt, _, _ in picks)):
            raise ValueError("Bet on **Win** or **Loss** first, then pick the team top frag.")
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
                if user_id in {u for u, _ in db.game_players(m)} and side == "loss":
                    raise ValueError("No betting against yourself 😉")
                price = mk["win"][side]
            elif market == "combo":
                result, _ = odds.split_combo(side)
                existing = next((sd for (mkt, sd) in db.group_pools(db.bets_for_match(match_id)) if mkt == "combo"), None)
                if existing and existing != side:  # one group bet per game: join it, don't start another
                    raise ValueError(f"This game already has a group bet: **{odds.bet_label(mk, m['host_riot'], market, existing)}**. "
                                     "Press 👥 Join group bet on the post to join it (optional).")
                price = odds.combo_price(mk, side)
                if price is None:
                    raise ValueError("Pick a result and a top fragger for a group bet.")
                if user_id in {u for u, _ in db.game_players(m)} and result == "loss":
                    raise ValueError("No betting against yourself 😉")
            else:
                opt = next((o for o in mk["topfrag"] if o["riot"] == side), None)
                if not opt:
                    raise ValueError("That player isn't in this match's top-frag bet.")
                price = opt["odds"]
            if seen and price < seen.get((market, side), 0) - 1e-9:
                raise ValueError(f"The odds just moved: **{odds.bet_label(mk, m['host_riot'], market, side)}** "
                                 f"now pays ×{price:.2f}. Try again to bet at the new odds.")
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
        crowd = [dict(b) for b in db.bets_for_match(match_id) if b["user_id"] != m["host_id"]
                 and b["status"] != "refunded"]
        db.update_match(match_id, markets=json.dumps(odds.reprice(mk, crowd)))
        return placed, balance

    async def cancel_bet(self, user: discord.abc.User, match_id: int, bet_id: int) -> tuple[dict, int]:
        """Refund one of your own bets while betting is open, re-price the odds, update the post and say so
        publicly. Returns (the bet, new balance). Raises ValueError."""
        m = db.get_match(match_id)
        if not ui.betting_open(m):
            raise ValueError("Betting has closed, so bets are locked in.")
        bet = next((dict(b) for b in db.bets_for_match(match_id) if b["id"] == bet_id), None)
        amount = db.cancel_bet(bet_id, user.id)
        mine = [b for b in db.bets_for_match(match_id) if b["user_id"] == user.id and b["status"] == "pending"]
        if bet and bet["market"] == "win" and not any(b["market"] == "win" for b in mine):
            for b in mine:  # top frag needs a Win/Loss bet: those go too
                if b["market"] == "topfrag":
                    amount += db.cancel_bet(b["id"], user.id)
        self._reprice(m)
        balance = db.get_user(user.id)["balance"]
        await self.refresh_market_message(match_id)  # (not announced: nothing about bettors until betting closes)
        return bet, balance

    async def cancel_all_bets(self, user: discord.abc.User, match_id: int) -> tuple[int, int]:
        """↩ Start over: refund every bet this person has on the game (Win/Loss, top frag, group) while betting
        is open. Returns (coins refunded, new balance). Raises ValueError once betting has closed."""
        m = db.get_match(match_id)
        if not ui.betting_open(m):
            raise ValueError("Betting has closed, so bets are locked in.")
        refunded = sum(db.cancel_bet(b["id"], user.id) for b in db.bets_for_match(match_id)
                       if b["user_id"] == user.id and b["status"] == "pending")
        if refunded:
            self._reprice(m)
            await self.refresh_market_message(match_id)
        return refunded, db.get_user(user.id)["balance"]

    @staticmethod
    def _reprice(m):
        """Odds for the next bets after a change in what's been bet (the host's own bets don't count)."""
        crowd = [dict(b) for b in db.bets_for_match(m["id"]) if b["user_id"] != m["host_id"]
                 and b["status"] == "pending"]
        db.update_match(m["id"], markets=json.dumps(odds.reprice(json.loads(m["markets"]), crowd)))

    async def announce_bets(self, user: discord.abc.User, m, placed: list[dict], balance: int):
        """Public one-liner per bet (odds/payout stay private), then update the pools on the post."""
        # Nothing about who bet what is posted while betting is open; the full list goes out when it closes.
        await self.refresh_market_message(m["id"])

    async def refresh_market_message(self, match_id: int):
        """Re-draw the 'Bets open' post: current pools, and no buttons once betting has closed."""
        m = db.get_match(match_id)
        if not m or not m["message_id"]:
            return
        ch = await self.channel(m["channel_id"])
        if not ch:
            return
        msg, bets = ch.get_partial_message(m["message_id"]), db.bets_for_match(m["id"])
        try:
            await msg.edit(view=markets_view(m, bets))
        except discord.HTTPException:
            try:  # a post from before the card layout: keep it in the old embed style
                await msg.edit(embed=markets_embed(m, bets), view=ui.market_view(m) if ui.betting_open(m) else None)
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
        if ch and self.henrik.enabled:  # unrated modes record everyone as "Unrated": show their Competitive rank
            try:
                await self.henrik.fill_ranks(detail)
            except Exception:
                log.exception("Couldn't look up current ranks for match %s", m["id"])
        # Teammates listed by agent because their name was hidden: match them up by agent, and keep showing
        # the agent instead of their name.
        listed = {o["riot"] for o in markets.get("topfrag", [])}
        for o in markets.get("topfrag", []):
            if overwolf_data.is_hidden(o["riot"]):
                agent = o["riot"].rsplit("#", 1)[0].casefold()
                p = next((p for p in detail.players if p.team == host.team and p.riot_id not in listed
                          and (p.agent or "").casefold() == agent), None)
                if p:
                    p.riot_id = o["riot"]
        outcome = odds.compute_outcome(detail, host)
        results = odds.settle(markets, outcome)
        judge = lambda market, side: odds.judge(results, market, side)
        db.set_outcome(m["id"], results["win"])
        if results["topfrag"] is None:
            # Scoreboard without ACS (Overwolf): pay Win/Loss now, top frag waits for HenrikDev's scoreboard
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
            if detail.map in ("", "Unknown map") and (known := json.loads(m["scouting"]).get("map")):
                detail.map = known  # the Overwolf app didn't send the map: Discord's status had it
            [e] = result_embeds(m, markets, detail, host, outcome, results, bets)
            await self.post_result(m, ch, e)  # replaces the end-of-game message: the whole post-game is one box


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
        picks = Counter(p.agent for d in details if (p := d.find_player(rid)) and p.agent
                        and p.agent not in ("?", "Unknown"))
        if picks:
            out[rid] = picks.most_common(1)[0][0]
    return out


# ---------- embeds ----------

def game_names(m) -> str:
    """Everyone linked who's playing in this game: 'Sam, Alex & Jordan'."""
    names = [short(r) for _, r in db.game_players(m)]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " & " + names[-1]


def markets_embed(m, bets=None) -> discord.Embed:
    """The Bets open post, kept short: who's playing, when betting closes, the record, the pot and the group bet.
    The odds are on the buttons underneath."""
    mk = json.loads(m["markets"])
    scouting = json.loads(m["scouting"])
    players = game_names(m)
    where = " · ".join(x for x in ((m["mode"] or "").title() if m["mode"] != "any" else "",
                                   scouting.get("map") or "") if x)
    is_open = ui.betting_open(m)
    if is_open:
        title = f"🎲 {players}" + (f" · {where}" if where else "")
        lines = [f"Betting closes {ts(datetime.fromisoformat(m['lock_at']))}"]
    else:
        title = {"locked": f"🔒 {players} · game on", "resolved": f"🏁 {players} · settled",
                 "cancelled": f"🚫 {players} · cancelled", "awaiting": f"🏁 {players} · result paid"
                 }.get(m["status"], f"🔒 {players}")
        lines = []
    form = scouting.get("form")
    if form and form.get("games"):
        streak = form.get("streak", 0)
        lines.append(f"Last {form['games']}: **{form['wins']}–{form['losses']}**"
                     + (f" · {'🔥' if streak > 0 else '🧊'} {abs(streak)} in a row" if abs(streak) >= 2 else ""))
    live = [b for b in bets or [] if not (b["status"] == "refunded" and m["status"] != "cancelled")]  # skip taken-back bets
    if live:
        people = len({b["user_id"] for b in live})
        lines.append(f"🎟️ {people} {'person has' if people == 1 else 'people have'} bet" if is_open else
                     f"💰 **{sum(b['amount'] for b in live):,}** coins bet by {people}")
    groups = [(side, p) for (mkt, side), p in db.group_pools(bets or []).items() if mkt == "combo"]
    if groups and not is_open:  # the group bet's pick is only shown once betting has closed
        side, p = groups[0]
        lines.append(f"👥 Group bet: **{odds.bet_label(mk, m['host_riot'], 'combo', side)}** · "
                     f"{p['total']:,} from {p['people']}" + (f" · +{p['bonus']}%" if p["bonus"] else ""))
    e = discord.Embed(title=title, description="\n".join(lines) or None, color=GOLD if is_open else GREY)
    e.set_footer(text="Type /info for details")
    return e


def markets_view(m, bets=None) -> discord.ui.LayoutView:
    """The 'Bets open' post as a card: who's playing, mode and map, a win-chance bar, recent record, the
    countdown and how many people have bet, then the buttons. Once betting closes: totals, no buttons.
    Amounts and picks stay secret until then."""
    mk = json.loads(m["markets"])
    scouting = json.loads(m["scouting"])
    players = game_names(m)
    where = " · ".join(x for x in ((m["mode"] or "").title() if m["mode"] != "any" else "",
                                   scouting.get("map") or "") if x)
    is_open = ui.betting_open(m)
    if is_open:
        head = f"## 🎲 {players}" + (f"\n-# {where}" if where else "")
    else:
        state = {"locked": "game on", "resolved": "settled", "cancelled": "cancelled",
                 "awaiting": "result paid"}.get(m["status"], "betting closed")
        icon = {"cancelled": "🚫", "resolved": "🏁", "awaiting": "🏁"}.get(m["status"], "🔒")
        head = f"## {icon} {players}\n-# " + " · ".join(x for x in (where, state) if x)
    lines = []
    if is_open:
        p = mk["win"]["p"]
        filled = round(p * 10)
        lines.append(f"{'🟩' * filled}{'⬛' * (10 - filled)}  **{p * 100:.0f}%** win chance")
    form = scouting.get("form")
    if form and form.get("games"):
        streak = form.get("streak", 0)
        lines.append(f"{'📈' if form['wins'] >= form['losses'] else '📉'} Last {form['games']}: "
                     f"**{form['wins']}–{form['losses']}**"
                     + (f" · {'🔥' if streak > 0 else '🧊'} {abs(streak)} in a row" if abs(streak) >= 2 else ""))
    live = [b for b in bets or [] if not (b["status"] == "refunded" and m["status"] != "cancelled")]
    people = len({b["user_id"] for b in live})
    if is_open:
        lines.append(f"⏳ Closes {ts(datetime.fromisoformat(m['lock_at']))} · 🎟️ "
                     + (f"{people} {'person has' if people == 1 else 'people have'} bet" if people else "no bets yet"))
    elif live:
        lines.append(f"💰 **{sum(b['amount'] for b in live):,}** coins bet by {people}")
    groups = [(side, p) for (mkt, side), p in db.group_pools(bets or []).items() if mkt == "combo"]
    if groups and not is_open:  # the group bet's pick is only shown once betting has closed
        side, p = groups[0]
        lines.append(f"👥 Group bet: **{odds.bet_label(mk, m['host_riot'], 'combo', side)}** · "
                     f"{p['total']:,} from {p['people']}" + (f" · +{p['bonus']}%" if p["bonus"] else ""))
    items = [ui.T(head), ui.Sep()]
    if lines:
        items.append(ui.T("\n".join(lines)))
    if is_open:
        items += ui.market_rows(m)
    items.append(ui.T("-# Type /info for details"))
    return ui.card(*items, color=GOLD if is_open else GREY)


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
            name=f"{p.riot_id} — {p.rank} · {threat(p)}",  # (field names can't show custom emojis)
            value=(f"{icons.rank(p.rank, fallback=False)} K/D **{p.kd:.2f}** · Win **{p.win_pct:.0f}%** · "
                   f"ACS **{p.acs:.0f}** · HS **{p.hs_pct:.0f}%**\n"
                   f"{p.kills_per_match:.1f} kills/game · {p.matches} games{mains}"),
            inline=False,
        )
    e.set_footer(text="Recent stats")
    return e


def odds_rank_name(avg_tier: float) -> str:
    return VALORANT_TIERS[min(max(round(avg_tier), 1), len(VALORANT_TIERS)) - 1]


def bettor_breakdown(m, bets) -> str:
    """One short line per bettor: net on this game, then each bet ('✅ Win 500→1,065 · ❌ Team top frag Sam 100').
    The explanations (on hold, refunds, bonuses) live in /info."""
    mk = json.loads(m["markets"])
    icon = {"won": "✅", "lost": "❌", "push": "↩️", "refunded": "↩️", "pending": "⏳"}
    groups = db.group_pools(bets)

    def label(b) -> str:
        if b["market"] == "win":
            return "Win" if b["side"] == "win" else "Loss"
        if b["market"] == "topfrag":
            return f"Team top frag {short(odds.option_name(b['side'], mk))}"
        result, riot = odds.split_combo(b["side"])
        bonus = groups.get((b["market"], b["side"]), {}).get("bonus", 0)
        return (f"👥 {'Win' if result == 'win' else 'Loss'} + team top frag {short(odds.option_name(riot, mk))}"
                + (f" +{bonus}%" if bonus else ""))

    per: dict[int, list] = {}
    for b in bets:
        if b["status"] != "refunded" or m["status"] == "cancelled":  # cancelled before the game: not news
            per.setdefault(b["user_id"], []).append(b)
    settled_net = lambda bs: sum(b["payout"] - b["amount"] for b in bs if b["status"] != "pending")
    lines = []
    for uid, bs in sorted(per.items(), key=lambda kv: -settled_net(kv[1])):
        parts = [f"{icon.get(b['status'], '•')} {label(b)} {b['amount']:,}"
                 + (f"→{b['payout']:,}" if b["status"] == "won" else "") for b in bs]
        net = settled_net(bs)
        lines.append(f"<@{uid}> **{'+' if net >= 0 else ''}{net:,}** · " + " · ".join(parts))
    return "\n".join(lines) or "No bets."


# Why some coins aren't back yet, wherever pending bets show up.
HELD_NOTE = (f"waiting for the final scoreboard (with ACS) for top frag, which hasn't been received yet. They pay out "
             f"as soon as it arrives, or are refunded in full after {MATCH_TIMEOUT.total_seconds() / 3600:g} h "
             "if it never does.")


def result_embeds(m, markets, detail: MatchDetail, host, outcome, results, bets) -> list[discord.Embed]:
    """The whole post-game in one box: scoreboard, top frag, then one line per bettor."""
    board = scoreboard_embed(detail, host, outcome)  # ⭐ on the scoreboard marks the team top frag
    notes = []
    if results["topfrag"] is None:
        notes.append("⏳ Final scores coming in a minute")
    elif results["topfrag"] == "push":
        notes.append("↩️ Team top frag tied: refunded")
    if absent := results.get("absent"):
        notes.append(f"↩️ {', '.join(short(r) for r in absent)} not in game: refunded")
    extra = "".join(n + "\n\n" for n in notes) + f"**Bets**\n{bettor_breakdown(m, bets)}"
    board.description = (((board.description + "\n\n") if board.description else "") + extra)[:4096]
    board.title = f"{game_names(m)} · {board.title}"
    return [board]


def pending_result_embed(m, player: str, won: bool | None, score: str, map_name: str | None) -> discord.Embed:
    """Win/Loss paid from the final score; the scoreboard (top frag) replaces this message when it arrives."""
    verdict = "Victory" if won else "Defeat" if won is False else "Draw"
    title = " · ".join(x for x in (game_names(m) or player, map_name, f"{score} {verdict}".strip()) if x)
    lines = []
    if json.loads(m["markets"]).get("topfrag") and m["status"] != "resolved":
        lines.append("⏳ Scoreboard coming in a minute")
    lines.append(f"**Bets**\n{bettor_breakdown(m, db.bets_for_match(m['id']))}")
    return discord.Embed(title=title, description="\n\n".join(lines)[:4096],
                         color=GREEN if won else RED if won is False else GREY)


def _width(ch: str) -> int:
    """How many monospace columns a character takes (CJK / full-width = 2, combining marks = 0)."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def pad(text: str, width: int) -> str:
    """Cut/pad text to exactly `width` monospace columns, so wide characters (愛あなた) don't break alignment."""
    out, used = "", 0
    for ch in text:
        w = _width(ch)
        if used + w > width - 1:  # keep at least one space before the next column
            break
        out += ch
        used += w
    return out + " " * (width - used)


def scoreboard_embed(detail: MatchDetail, host, outcome) -> discord.Embed:
    """Like Valorant's end screen: everyone in one list, sorted by performance score; 🟩 your team, 🟥 the other
    team; rank icon, name, performance score and K/D/A."""
    if not any(p.perf is not None for p in detail.players):
        return _scoreboard_embed_by_team(detail, host, outcome)  # no performance scores (tracker.gg backup data)
    mine_t = detail.teams.get(host.team)
    other_t = next((t for tid, t in detail.teams.items() if tid != host.team), None)
    won = mine_t.won if mine_t else None
    score = (f" {mine_t.rounds_won}–{other_t.rounds_won}" if mine_t and other_t and mine_t.rounds_won is not None
             and other_t.rounds_won is not None else "")
    verdict = "Victory" if won else "Defeat" if won is False else "Draw"
    sp = icons.blank()
    rows = [f"{sp}{sp}{sp}`Score    K / D / A ` `Player`"]
    for p in sorted(detail.players, key=lambda p: (-(p.perf or 0), -p.kills)):
        square = "🟩" if p.team == host.team else "🟥"
        top = " ⭐" if p.riot_id == outcome.get("topfrag_riot") else ""
        kda = f"{p.kills} / {p.deaths} / {p.assists}"
        agent = icons.agent(p.agent) or sp
        rank = icons.rank(p.rank, fallback=False) or sp
        # Numbers first in a fixed-width box, the name after it: names of any width (愛あなた, emoji) can't
        # push the columns out of line.
        name = short(p.riot_id).replace("`", "'")  # a backtick would break the code style
        rows.append(f"{square}{agent}{rank}`{p.perf or 0:>5.0f}  {kda:>11} ` `{name}`{top}")
    board = discord.Embed(title=f"{detail.map} ·{score} {verdict}", description="\n".join(rows)[:4096],
                          color=GREEN if won else RED if won is False else GREY)
    return board


def _scoreboard_embed_by_team(detail: MatchDetail, host, outcome) -> discord.Embed:
    """Scoreboard: agent + rank icons, name, K/D/A and ACS per player, both teams; the map's picture in the
    corner, the final score and result in the title, green/red for a win/loss. Icons fall back to text."""
    mine_t = detail.teams.get(host.team)
    other_t = next((t for tid, t in detail.teams.items() if tid != host.team), None)
    won = mine_t.won if mine_t else None
    score = (f" {mine_t.rounds_won}–{other_t.rounds_won}" if mine_t and other_t and mine_t.rounds_won is not None
             and other_t.rounds_won is not None else "")
    verdict = "Victory" if won else "Defeat" if won is False else "Draw"
    board = discord.Embed(title=f"📊 {detail.map} ·{score} {verdict}",
                          color=GREEN if won else RED if won is False else GREY)
    if pic := icons.map_picture(detail.map):
        board.set_thumbnail(url=pic)
    for team_id in sorted({p.team for p in detail.players}, key=lambda t: t != host.team):
        players = sorted((p for p in detail.players if p.team == team_id), key=lambda p: (-p.score, -p.kills))
        rows = []
        for p in players:
            top = p.riot_id == outcome.get("topfrag_riot")
            agent = icons.agent(p.agent) or f"`{p.agent[:9]}`"
            acs = f" · `{p.acs:.0f} ACS`" if p.acs is not None else ""
            rows.append(f"{agent} {icons.rank(p.rank)} **{short(p.riot_id)}**{' ⭐' if top else ''}"
                        f" · `{p.kills}/{p.deaths}/{p.assists}`{acs}")
        team = detail.teams.get(team_id)
        rounds = f" · {team.rounds_won}" if team and team.rounds_won is not None else ""
        board.add_field(name=f"{'🟦 Your team' if team_id == host.team else '🟥 Opponents'}{rounds}",
                        value="\n".join(rows)[:1024] or "—", inline=False)
    board.set_footer(text="⭐ top frag (highest ACS) · K/D/A · ACS = average combat score")
    return board


# ---------- helpers ----------

async def scout(ids: list[str], keep_on_error: bool = False,
                known_ranks: dict[str, str] | None = None) -> tuple[list[PlayerStats], list[str]]:
    """Look up several players: rank and season record from HenrikDev, tracker.gg as a backup.
    keep_on_error: if nothing can load someone (but they exist), keep them with average stats instead of
    dropping them (used for teammates, so they stay bettable).
    known_ranks: ranks already known from the live game (Overwolf), used over the looked-up rank."""
    known = {k.casefold(): v for k, v in (known_ranks or {}).items()}
    hidden = [r for r in ids if overwolf_data.is_hidden(r)]  # hidden names ("Reyna#AGENT"): nothing to look up
    ids = [r for r in ids if r not in hidden]
    # HenrikDev for everyone at once, each given a few seconds at most
    results = await asyncio.gather(*(asyncio.wait_for(henrik_stats(r, known.get(r.casefold())), 10) for r in ids),
                                   return_exceptions=True)
    need = [rid for rid, res in zip(ids, results) if not isinstance(res, PlayerStats)]
    backup = dict(zip(need, await asyncio.gather(*(bot.tracker.get_profile(r) for r in need),
                                                 return_exceptions=True))) if need and TRACKER_API_KEY else {}
    ok, failed = [], []
    for rid, res in zip(ids, results):
        rank = known.get(rid.casefold())
        fallback = backup.get(rid)
        if isinstance(res, PlayerStats):
            ok.append(res)
        elif isinstance(fallback, PlayerStats):  # HenrikDev couldn't help: tracker.gg's profile
            if not fallback.tier and rank:
                fallback.rank, fallback.tier = rank, tier_index(rank)
            ok.append(fallback)
        elif keep_on_error and not isinstance(fallback, PlayerNotFound):
            ok.append(PlayerStats(riot_id=rid, rank=rank or "Unranked", tier=tier_index(rank)))
            if not known_ranks:
                failed.append(f"{rid} (stats unavailable, using average stats)")
        else:
            why = next((x for x in (fallback, res) if isinstance(x, BaseException)), "no data")
            failed.append(f"{rid} ({why})")
    ok += [PlayerStats(riot_id=r, rank="Unranked", tier=0) for r in hidden]
    return ok, failed


async def henrik_stats(riot_id: str, live_rank: str | None = None) -> PlayerStats | None:
    """A player's current rank and recent season record from HenrikDev (the main data source).
    `live_rank` (from the game itself, via Overwolf) wins over HenrikDev's rank. None if unavailable."""
    if not bot.henrik.enabled:
        return None
    try:
        s = await bot.henrik.summary(riot_id)
    except henrik.HenrikError:  # rate limit: fall back to average stats for this one
        return None
    if not s:
        return None
    rank = live_rank if tier_index(live_rank) else (s.get("rank") or "Unranked")
    games, wins = s.get("games") or 0, s.get("wins") or 0
    return PlayerStats(riot_id=riot_id, rank=rank, tier=tier_index(rank), matches=games,
                       win_pct=wins / games * 100 if games else 50.0)


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
@app_commands.describe(riot_id="Your Riot ID, e.g. TenZ#0505")
async def link(inter: discord.Interaction, riot_id: str):
    rid = parse_riot_id(riot_id)
    if not rid:
        return await inter.response.send_message("That doesn't look like a Riot ID (Name#TAG).", ephemeral=True)
    owner = db.riot_owner(rid)
    if owner and owner != inter.user.id:
        return await inter.response.send_message("That Riot ID is already linked to another Discord account.",
                                                 ephemeral=True)
    await inter.response.defer(ephemeral=True)
    if bot.henrik.enabled:
        try:
            await bot.henrik.region(rid)  # the account lookup: does this Riot ID exist?
        except henrik.HenrikError as e:
            if "not found" in str(e):
                return await inter.followup.send(f"Couldn't find **{rid}**. Check the spelling and tag.",
                                                 ephemeral=True)
            db.set_riot_id(inter.user.id, rid)  # HenrikDev is down: save it anyway, stats load later
            return await inter.followup.send(f"Linked to **{rid}**. Couldn't check it right now ({e}); "
                                             "stats load when betting opens.", ephemeral=True)
        db.set_riot_id(inter.user.id, rid)
        s = await henrik_stats(rid)
        return await inter.followup.send(f"Linked to {rank_line(rid, s)}", ephemeral=True)
    try:  # no HenrikDev key: tracker.gg
        stats = await bot.tracker.get_profile(rid)
    except PlayerNotFound:
        return await inter.followup.send(f"Couldn't find **{rid}**. Check the spelling and tag.", ephemeral=True)
    except TrackerError as e:
        db.set_riot_id(inter.user.id, rid)
        return await inter.followup.send(f"Linked to **{rid}**. Couldn't load stats right now ({e}); "
                                         "they'll load when betting opens.", ephemeral=True)
    db.set_riot_id(inter.user.id, rid)
    await inter.followup.send(f"Linked to {stat_line(stats)}", ephemeral=True)


def rank_line(riot_id: str, s: PlayerStats | None) -> str:
    """'<rank icon> **Name#TAG** · Gold 2 · 54% wins in 37 games' from HenrikDev's summary."""
    if not s:
        return f"**{riot_id}**"
    record = f" · {s.win_pct:.0f}% wins in {s.matches} competitive games" if s.matches else ""
    return f"{icons.rank(s.rank, fallback=False)} **{riot_id}** · {s.rank}{record}"


@bot.tree.command(name="link-for", description="Link a friend's Riot ID for them (server managers)")
@app_commands.describe(user="Who to link", riot_id="Their Riot ID, e.g. Player#NA1")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def link_for(inter: discord.Interaction, user: discord.Member, riot_id: str):
    if user.bot:
        return await inter.response.send_message("Bots can't be linked.", ephemeral=True)
    rid = parse_riot_id(riot_id)
    if not rid:
        return await inter.response.send_message("That doesn't look like a Riot ID (Name#TAG).", ephemeral=True)
    if (owner := db.riot_owner(rid)) and owner != user.id:
        return await inter.response.send_message("That Riot ID is already linked to another Discord account.",
                                                 ephemeral=True)
    db.set_riot_id(user.id, rid)
    await inter.response.send_message(
        f"Linked {user.mention}: Riot ID **{rid}**\nThey can change it with /link or remove it with /unlink.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="overwolf-link",
                  description="Your private address for the HomeAssistant Game Events Overwolf app")
async def overwolf_link(inter: discord.Interaction):
    host = os.getenv("OVERWOLF_WEBHOOK_HOST") or "valbet"
    url = f"http://{host}.local:{dashboard.PORT}/api/ha/{db.overwolf_key(inter.user.id)}"
    await inter.response.send_message(
        "**Send your games to the bot automatically** (optional, gives full teammate names at agent select):\n1. Install **HomeAssistant Game Events** from the Overwolf store.\n"
        f"2. Paste this into its **Webhook URL** and press Save:\n`{url}`\n"
        "It only reports **your own** games. Keep it private: anyone with it could send results as you.\n"
        "-# Not on the bot's home network? You also need the Tailscale steps the bot owner sends you.",
        ephemeral=True)


@bot.tree.command(description="Remove your linked Riot ID (your coins stay)")
async def unlink(inter: discord.Interaction):
    if db.active_match_for_host(inter.user.id):
        return await inter.response.send_message("Betting is open on your current game. Try again after it ends.",
                                                 ephemeral=True)
    done = db.unlink(inter.user.id)
    await inter.response.send_message(
        "Unlinked. The bot won't follow your games any more; your coins are kept." if done
        else "You don't have anything linked.", ephemeral=True)


@bot.tree.command(name="scout", description="Look up a player's Valorant rank and recent games")
async def scout_cmd(inter: discord.Interaction, riot_id: str):
    rid = parse_riot_id(riot_id)
    if not rid:
        return await inter.response.send_message("That doesn't look like a Riot ID (Name#TAG).", ephemeral=True)
    await inter.response.defer()
    if bot.henrik.enabled:
        try:
            details = await bot.henrik.recent_details(rid, odds.FORM_GAMES)
        except henrik.HenrikError as e:
            if not TRACKER_API_KEY:
                return await inter.followup.send(f"Couldn't scout **{rid}**: {e}")
        else:
            line = rank_line(rid, await henrik_stats(rid))
            if f := odds.recent_form(rid, details):
                line += (f"\nLast {f['games']}: **{f['wins']}–{f['losses']}** · {f['kd']:.2f} K/D · "
                         f"{f['acs'] or 0:.0f} ACS · {f['kills']:.1f} kills a game")
            return await inter.followup.send(line)
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


def info_guide() -> list[discord.Embed]:
    """Everything about how the betting works, in depth."""
    tiers = ", ".join(f"+{pct}% from {need:,}" for need, pct in db.GROUP_TIERS)
    hours = f"{MATCH_TIMEOUT.total_seconds() / 3600:g}"
    how = discord.Embed(title="📖 How betting works", color=GOLD, description=(
        "**Betting opens by itself** when a linked player's game starts (from their Discord status, or the "
        "Overwolf app at agent select). A **Bets open** post appears in the betting channel.\n"
        f"**Betting closes** {int(AUTO_WINDOW * 60)} seconds later, or as soon as the first round ends, whichever is "
        "first. Nobody can open, close, settle or cancel a game by hand.\n"
        "**Anyone in the server can bet**, playing or not. Bets are private; the channel only sees "
        "who bet how much on what.\n"
        "**To bet:** press **Win** or **Loss** on the post. Your slip starts at 100 coins: change it with "
        "**−500…−10 / +10…+500**, **All in** or **✏️ Type** (\"all\" works), then **✅ Bet**. The team top frag "
        "comes up right after (optional). **↩ Start over** refunds everything you've bet on that game so you can "
        "pick again."))
    how.add_field(name="🏆 Match result", inline=False, value=(
        "Win or Loss for the team the linked players are on. There's **one post per game**, even when several "
        "linked friends play together, and anyone in the server can bet on it. Anyone playing in that game can't "
        "bet on their own team losing. A draw refunds."))
    how.add_field(name="🎯 Team top frag", inline=False, value=(
        "The best player on the linked players' own team (not the whole lobby). "
        "**Needs a Win or Loss bet on the same game first** (cancelling that also cancels your top-frag bets). "
        "Who has the **highest Performance Score** on the player's team (the number Valorant's end screen shows; "
        "ACS if that isn't available). Equal scores: more kills wins; equal kills too: top-frag bets are refunded.\n"
        "Options: the player, teammates known to be in the game (the whole team with the Overwolf app, otherwise "
        "linked friends Discord shows in their party), and **Other teammates** for the randoms.\n"
        "If a listed teammate isn't actually in the game, bets on them are refunded and the other top-frag bets "
        "are paid at odds re-priced for the real lineup."))
    how.add_field(name="👥 Group bet", inline=False, value=(
        "One per game: a result **and** a top fragger, both must be right, paying both odds multiplied. The first "
        "person to press **Create group bet** picks it (others wait up to 2 minutes); after that anyone *can* "
        "press **Join group bet** (optional) and only chooses an amount. Both ask to confirm.\n"
        f"Bonus for everyone in it, set by the pool's size when betting closes (needs {db.GROUP_MIN_PEOPLE}+ "
        f"people): {tiers}."))
    how.add_field(name="❌ Cancelling", inline=False, value=(
        "**✖ My bets** on the post lists your bets, each with a **Cancel** button (full refund) until betting "
        "closes."))
    odds_e = discord.Embed(title="📈 Odds and payouts", color=GOLD, description=(
        "Odds are what a bet pays back, stake included: 100 at ×1.90 returns 190. There's a 5% house edge.\n"
        "**Win/Loss odds** start from how often the group wins (about 47%). Win rate, streaks and map records "
        "were tested on the group's past games and didn't predict results, so they're left out. Opponents' "
        "ranks and K/D move it when the Overwolf app names them (at most ±8%). After 30 finished games the bot "
        "also tunes itself from how its predictions turned out (`/accuracy`).\n"
        "**Top-frag odds** simulate the game thousands of times from each player's usual score and how much it "
        "swings (random teammates average about 213 ACS).\n"
        "**Odds shift a little as people bet** (at most ±5%), like a sportsbook. Every bet keeps the odds it "
        "was placed at. You can add to a bet but not also bet against it in the same game."))
    odds_e.add_field(name="💸 Payouts", inline=False, value=(
        "**Win/Loss** pays the moment the game ends (from the final score in the player's status).\n"
        "**Top frag and group bets** pay about a minute later, once the full scoreboard is in; the recap "
        "shows everyone's rank, Performance Score and K/D/A.\n"
        f"If a result never arrives (left early, remake, data unavailable), bets still waiting are refunded in "
        f"full after {hours} hours. `/mybets` shows yours, `/balance` your coins."))
    odds_e.add_field(name="🏁 Reading the post-game box", inline=False, value=(
        "One message per game. It first shows the result and Win/Loss payouts, then turns into the full "
        "scoreboard once it's in (🟩 the player's team, 🟥 opponents, sorted by Performance Score; ⭐ team top frag).\n"
        "Each bettor's line: their **net coins** on the game, then each bet: ✅ won (stake→payout), ❌ lost, "
        "↩️ refunded, ⏳ on hold. **👥** = group bet, **+N%** = its group bonus.\n"
        "⏳ On hold: the coins aren't in your balance yet; they pay once the scoreboard arrives, or are refunded "
        f"after {hours} hours. \"Not in game: refunded\" means a listed teammate wasn't in the match, so bets on "
        "them came back and the other top-frag bets were paid at odds re-priced for the real lineup. Ranks in "
        "Swiftplay and other unrated modes show each player's current Competitive rank."))
    odds_e.add_field(name="💰 Coins", inline=False, value=(
        f"Everyone starts with **{db.STARTING_BALANCE:,}** and gets **{db.DAILY_AMOUNT:,} free every day** at "
        "midnight UTC. Coins are play money. `/give` sends some to a friend, `/leaderboard` shows the richest."))
    setup = discord.Embed(title="🔗 Linking and games", color=GOLD, description=(
        "**Valorant:** `/link Name#TAG` once (a server manager can use `/link-for`). Turn on Discord's "
        "**Share my activity** so betting can open for your games. `/unlink` stops it.\n"
        "**Overwolf (optional):** install **HomeAssistant Game Events** from the Overwolf store and paste your "
        "address from `/overwolf-link`. It names every teammate at agent select.\n"
        "Other commands: `/scout Name#TAG`, `/accuracy`, `/panel` (posts the instructions and sets the betting "
        "channel)."))
    return [how, odds_e, setup]


def info_match(m) -> discord.Embed:
    """Everything about one game: odds and pools for every bet, the group bet, and the scouting report."""
    mk = json.loads(m["markets"])
    sc = json.loads(m["scouting"])
    bets = [b for b in db.bets_for_match(m["id"]) if b["status"] != "refunded"]
    pools: dict = {}
    for b in bets:
        pools[(b["market"], b["side"])] = pools.get((b["market"], b["side"]), 0) + b["amount"]
    host = short(m["host_riot"])
    e = discord.Embed(title=f"🔎 {game_names(m)} · in depth", color=GOLD,
                      description=f"Status: **{m['status']}** · betting closes {ts(datetime.fromisoformat(m['lock_at']))}")
    e.add_field(name="🏆 Match result", inline=True, value=(
        f"Win ×{mk['win']['win']:.2f} · {pools.get(('win', 'win'), 0):,} bet\n"
        f"Loss ×{mk['win']['loss']:.2f} · {pools.get(('win', 'loss'), 0):,} bet\n"
        f"Chance of a win: {mk['win']['p'] * 100:.0f}%"))
    if mk.get("topfrag"):
        e.add_field(name="🎯 Team top frag", inline=True, value="\n".join(
            f"{odds.option_label(o, mk)} ×{o['odds']:.2f} · {pools.get(('topfrag', o['riot']), 0):,} bet"
            for o in mk["topfrag"])[:1024])
        groups = [(side, p) for (mkt, side), p in db.group_pools(bets).items() if mkt == "combo"]
        if groups:
            side, p = groups[0]
            e.add_field(name="👥 Group bet", inline=False, value=(
                f"**{odds.bet_label(mk, m['host_riot'], 'combo', side)}** ×{odds.combo_price(mk, side) or 0:.2f} · "
                + pool_status(p)))
    lines = []
    hp = sc.get("host")
    if hp and hp.get("riot_id"):
        lines.append(f"🎯 {stat_line(PlayerStats.from_dict(hp))}")
    if form := sc.get("form"):
        streak = form.get("streak", 0)
        lines.append(f"📈 Last {form['games']}: **{form['wins']}–{form['losses']}**"
                     + (f" · {'🔥 W' if streak > 0 else '🧊 L'}{abs(streak)} streak" if abs(streak) >= 2 else "")
                     + (f" · {form['kd']:.2f} K/D · {form['acs'] or 0:.0f} ACS" if form.get("kd") is not None else ""))
    if (rec := sc.get("map_record")) and sc.get("map"):
        lines.append(f"🗺️ {sc['map']}: **{rec[0]}–{rec[1] - rec[0]}** recently")
    if m["mode"] in odds.MODE_ROUNDS:
        lines.append(f"⏱️ First to {odds.MODE_ROUNDS[m['mode']]} rounds: upsets are likelier, odds sit closer to even")
    lines += [f"🟦 {stat_line(PlayerStats.from_dict(t))}" for t in sc.get("teammates", [])]
    lines += [f"🟥 {stat_line(PlayerStats.from_dict(o))}" for o in sc.get("opponents", [])]
    if lines:
        e.add_field(name="📋 Scouting", inline=False, value="\n".join(lines)[:1024])
    return e


@bot.tree.command(name="info", description="Everything about how the betting works, and the current game in depth")
async def info_cmd(inter: discord.Interaction):
    embeds = info_guide()
    size = lambda e: len(e.title or "") + len(e.description or "") + sum(len(f.name) + len(f.value) for f in e.fields)
    for m in [m for m in db.active_matches(inter.channel_id) if m["status"] in ("open", "locked", "awaiting")][:3]:
        extra = info_match(m)
        if sum(size(e) for e in embeds) + size(extra) <= 5800:  # Discord: 6,000 characters per message
            embeds.append(extra)
    await inter.response.send_message(embeds=embeds[:10], ephemeral=True)


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
