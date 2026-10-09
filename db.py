"""SQLite storage for balances, matches and bets. All money moves happen in one transaction."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

STARTING_BALANCE = int(os.getenv("STARTING_BALANCE", "1000"))
DAILY_AMOUNT = int(os.getenv("DAILY_AMOUNT", "50"))
OVERWATCH = os.getenv("OVERWATCH", "0") == "1"  # Overwatch session bets: off until they're finished

conn = sqlite3.connect(os.getenv("DB_PATH", "valbet.db"), isolation_level=None)
conn.row_factory = sqlite3.Row
conn.executescript(
    """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS users (
        discord_id INTEGER PRIMARY KEY,
        balance    INTEGER NOT NULL,
        riot_id    TEXT,
        last_daily TEXT
    );
    CREATE TABLE IF NOT EXISTS matches (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id          INTEGER,
        channel_id        INTEGER,
        message_id        INTEGER,
        opener_id         INTEGER,
        host_id           INTEGER,
        host_riot         TEXT,
        mode              TEXT,
        status            TEXT,   -- open | locked | resolved | cancelled
        opened_at         TEXT,
        lock_at           TEXT,
        baseline_match_id TEXT,
        markets           TEXT,
        scouting          TEXT,
        tracker_match_id  TEXT,
        result            TEXT
    );
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS bets (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        market   TEXT NOT NULL,
        side     TEXT NOT NULL,
        amount   INTEGER NOT NULL,
        odds     REAL NOT NULL,
        status   TEXT NOT NULL DEFAULT 'pending',  -- pending | won | lost | push | refunded
        payout   INTEGER NOT NULL DEFAULT 0
    );
    -- Every finished Valorant game the bot saw in a linked player's Discord status, bets or not:
    -- their own record for the odds while tracker.gg isn't available, and per-map records.
    CREATE TABLE IF NOT EXISTS games (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id INTEGER NOT NULL,
        ended_at   TEXT NOT NULL,
        mode       TEXT,
        map        TEXT,
        ours       INTEGER,
        theirs     INTEGER,
        outcome    TEXT NOT NULL,   -- win | loss | draw
        UNIQUE (discord_id, ended_at)
    );
    """
)
# Columns added after the first release (existing databases get them on startup).
for _table, _col, _ddl in (("users", "battletag", "TEXT"), ("matches", "game", "TEXT NOT NULL DEFAULT 'valorant'"),
                           ("matches", "outcome", "TEXT"),  # win | loss | push, for checking the odds afterwards
                           ("bets", "grp", "INTEGER NOT NULL DEFAULT 0"),  # 1 = part of a group pool
                           ("users", "ow_id", "TEXT")):  # Blizzard player ID for the linked BattleTag
    if _col not in {r[1] for r in conn.execute(f"PRAGMA table_info({_table})")}:
        conn.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} {_ddl}")

# Group pools: everyone who joins the pool on the same outcome gets a bonus on their payout once the pool
# is big enough, e.g. "500:10,1000:20,2000:30" = +10% from 500 coins, +20% from 1,000, +30% from 2,000.
GROUP_TIERS = sorted((int(a), int(b)) for a, b in (t.split(":") for t in
                     os.getenv("GROUP_TIERS", "500:10,1000:20,2000:30").split(",") if t.strip()))
GROUP_MIN_PEOPLE = int(os.getenv("GROUP_MIN_PEOPLE", "2"))


def group_bonus(total: int, people: int) -> int:
    """Payout bonus in % for a pool with this many coins from this many different people."""
    if people < GROUP_MIN_PEOPLE:
        return 0
    return max((pct for need, pct in GROUP_TIERS if total >= need), default=0)


def next_tier(total: int) -> tuple[int, int] | None:
    """(coins needed, bonus %) for the next bonus tier, or None at the top."""
    return next(((need, pct) for need, pct in GROUP_TIERS if total < need), None)


def group_pools(bets) -> dict[tuple[str, str], dict]:
    """{(market, side): {"total", "people", "bonus"}} for the group pools among these bets."""
    pools: dict[tuple[str, str], dict] = {}
    for b in bets:
        if b["grp"] and b["status"] != "refunded":
            p = pools.setdefault((b["market"], b["side"]), {"total": 0, "users": set()})
            p["total"] += b["amount"]
            p["users"].add(b["user_id"])
    return {k: {"total": p["total"], "people": len(p["users"]), "bonus": group_bonus(p["total"], len(p["users"]))}
            for k, p in pools.items()}


def _payouts(c, match_id: int):
    """payout(bet, status) for this match: winnings, plus the group bonus for group-pool bets."""
    pools = group_pools(c.execute("SELECT * FROM bets WHERE match_id = ?", (match_id,)).fetchall())

    def payout(b, st):
        if st == "push":
            return b["amount"]
        if st != "won":
            return 0
        bonus = pools.get((b["market"], b["side"]), {}).get("bonus", 0) if b["grp"] else 0
        return int(b["amount"] * b["odds"] * (1 + bonus / 100))
    return payout


class Tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK."""

    def __enter__(self):
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def __exit__(self, exc_type, *_):
        conn.execute("ROLLBACK" if exc_type else "COMMIT")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- users ----------

def get_user(discord_id: int) -> sqlite3.Row:
    conn.execute("INSERT OR IGNORE INTO users (discord_id, balance) VALUES (?, ?)", (discord_id, STARTING_BALANCE))
    return conn.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,)).fetchone()


def linked_riot_id(discord_id: int) -> str | None:
    """Riot ID if this user has used /link, without creating a user row."""
    row = conn.execute("SELECT riot_id FROM users WHERE discord_id = ?", (discord_id,)).fetchone()
    return row[0] if row else None


def game_players(m) -> list[tuple[int, str]]:
    """Linked people playing in a match's game, host first: [(discord_id, riot_id)]. They can't bet on
    their own team losing."""
    out = [(m["host_id"], m["host_riot"])]
    for t in json.loads(m["scouting"] or "{}").get("teammates", []):
        uid = riot_owner(t["riot_id"])
        if uid and uid not in {u for u, _ in out}:
            out.append((uid, t["riot_id"]))
    return out


def user_by_riot(riot_id: str) -> int | None:
    """Discord ID of whoever linked this Riot ID (case-insensitive)."""
    return riot_owner(riot_id)


def riot_owner(riot_id: str) -> int | None:
    """Discord ID that has this Riot ID linked, compared with Python casefold (unicode-safe)."""
    want = riot_id.casefold()
    for discord_id, rid in conn.execute("SELECT discord_id, riot_id FROM users WHERE riot_id IS NOT NULL"):
        if rid.casefold() == want:
            return discord_id
    return None


def battletag_owner(battletag: str) -> int | None:
    """Discord ID that has this Overwatch BattleTag linked (case-insensitive)."""
    want = battletag.casefold()
    for discord_id, tag in conn.execute("SELECT discord_id, battletag FROM users WHERE battletag IS NOT NULL"):
        if tag.casefold() == want:
            return discord_id
    return None


def set_battletag(discord_id: int, battletag: str, ow_id: str | None = None):
    get_user(discord_id)
    conn.execute("UPDATE users SET battletag = ?, ow_id = ? WHERE discord_id = ?", (battletag, ow_id, discord_id))


def linked_battletag(discord_id: int) -> tuple[str, str | None] | None:
    """(BattleTag, Blizzard player ID or None) if this user linked Overwatch, without creating a user row."""
    row = conn.execute("SELECT battletag, ow_id FROM users WHERE discord_id = ?", (discord_id,)).fetchone()
    return (row[0], row[1]) if row and row[0] else None


def is_linked(discord_id: int) -> bool:
    """Has a Riot ID or BattleTag linked (the bot only follows the games of people who opted in)."""
    row = conn.execute("SELECT riot_id, battletag FROM users WHERE discord_id = ?", (discord_id,)).fetchone()
    return bool(row and (row[0] or row[1]))


def set_riot_id(discord_id: int, riot_id: str):
    get_user(discord_id)
    conn.execute("UPDATE users SET riot_id = ? WHERE discord_id = ?", (riot_id, discord_id))


def unlink(discord_id: int) -> bool:
    """Forget someone's Riot ID and BattleTag (balance and bets stay). False if nothing was linked."""
    return conn.execute("UPDATE users SET riot_id = NULL, battletag = NULL, ow_id = NULL WHERE discord_id = ? "
                        "AND (riot_id IS NOT NULL OR battletag IS NOT NULL)", (discord_id,)).rowcount == 1


def transfer(src: int, dst: int, amount: int):
    get_user(src), get_user(dst)
    with Tx() as c:
        bal = c.execute("SELECT balance FROM users WHERE discord_id = ?", (src,)).fetchone()[0]
        if bal < amount:
            raise ValueError(f"You only have {bal:,} coins.")
        c.execute("UPDATE users SET balance = balance - ? WHERE discord_id = ?", (amount, src))
        c.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (amount, dst))


def pay_daily_if_due() -> int | None:
    """Give every user DAILY_AMOUNT once per UTC day. Returns how many were paid, or None if already paid today.

    The date is stored in the db, so restarting the bot never pays twice and a missed day is paid on startup.
    """
    today = _now().date().isoformat()
    with Tx() as c:
        row = c.execute("SELECT value FROM meta WHERE key = 'last_daily_payout'").fetchone()
        if row and row[0] == today:
            return None
        n = c.execute("UPDATE users SET balance = balance + ?", (DAILY_AMOUNT,)).rowcount
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_daily_payout', ?)", (today,))
    return n


def overwolf_key(discord_id: int) -> str:
    """This person's own key for sending their games from the Overwolf app (made on first use)."""
    import secrets
    return get_or_create_meta(f"ow_key:{discord_id}", lambda: secrets.token_urlsafe(18))


def overwolf_key_owner(key: str) -> int | None:
    import secrets
    for k, v in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'ow_key:%'"):
        if secrets.compare_digest(v.encode(), key.encode()):
            return int(k.split(":", 1)[1])
    return None


def pay_aces(game_id: str, payouts: list[tuple[int, int]]) -> bool:
    """Pay ace bonuses once per game (a game can be on more than one host's bet page). False if already paid."""
    with Tx() as c:
        if c.execute("SELECT 1 FROM meta WHERE key = ?", (f"aces:{game_id}",)).fetchone():
            return False
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (f"aces:{game_id}", "paid"))
        for discord_id, coins in payouts:
            c.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (coins, discord_id))
    return True


def get_meta(key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def get_or_create_meta(key: str, make) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row:
        return row[0]
    value = make()
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, value))
    return value


def all_users() -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM users ORDER BY balance DESC").fetchall()


def recent_matches(limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM matches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def leaderboard(limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM users ORDER BY balance DESC LIMIT ?", (limit,)).fetchall()


# ---------- matches ----------

def create_match(**f) -> int:
    f["markets"] = json.dumps(f["markets"])
    f["scouting"] = json.dumps(f["scouting"])
    cols = ", ".join(f)
    cur = conn.execute(f"INSERT INTO matches ({cols}) VALUES ({', '.join('?' * len(f))})", tuple(f.values()))
    return cur.lastrowid


def update_match(match_id: int, **f):
    sets = ", ".join(f"{k} = ?" for k in f)
    conn.execute(f"UPDATE matches SET {sets} WHERE id = ?", (*f.values(), match_id))


def get_match(match_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()


def active_match_for_host(host_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM matches WHERE host_id = ? AND status IN ('open', 'locked') ORDER BY id DESC LIMIT 1",
        (host_id,),
    ).fetchone()


def awaiting_match_for_host(host_id: int) -> sqlite3.Row | None:
    """Their latest game whose result is in but whose top-frag bets still need the scoreboard."""
    return conn.execute(
        "SELECT * FROM matches WHERE host_id = ? AND status = 'awaiting' ORDER BY id DESC LIMIT 1", (host_id,),
    ).fetchone()


def active_matches(channel_id: int | None = None) -> list[sqlite3.Row]:
    if channel_id is None:
        return conn.execute("SELECT * FROM matches WHERE status IN ('open', 'locked', 'awaiting')").fetchall()
    return conn.execute(
        "SELECT * FROM matches WHERE channel_id = ? AND status IN ('open', 'locked', 'awaiting') ORDER BY id DESC",
        (channel_id,),
    ).fetchall()


def set_outcome(match_id: int, outcome: str):
    """Remember how the game went (win | loss | push), so predictions can be checked later."""
    conn.execute("UPDATE matches SET outcome = ? WHERE id = ?", (outcome, match_id))


def predictions() -> list[tuple[float, bool]]:
    """(predicted win chance before any self-tuning, won?) for every finished Valorant game with bets open."""
    out = []
    for markets, outcome in conn.execute(
            "SELECT markets, outcome FROM matches WHERE game = 'valorant' AND outcome IN ('win', 'loss')"):
        win = json.loads(markets)["win"]
        if "p_raw" in win:  # games priced before predictions were saved don't count
            out.append((win["p_raw"], outcome == "win"))
    return out


# ---------- game history (from Discord status) ----------

def record_game(discord_id: int, ended_at: str, mode: str | None, map_name: str | None,
                ours: int, theirs: int) -> bool:
    outcome = "win" if ours > theirs else "loss" if theirs > ours else "draw"
    return conn.execute(
        "INSERT OR IGNORE INTO games (discord_id, ended_at, mode, map, ours, theirs, outcome) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (discord_id, ended_at, mode, map_name, ours, theirs, outcome)).rowcount == 1


def games_for(discord_id: int, limit: int = 50) -> list[sqlite3.Row]:
    """Their finished games, newest first."""
    return conn.execute("SELECT * FROM games WHERE discord_id = ? ORDER BY ended_at DESC LIMIT ?",
                        (discord_id, limit)).fetchall()


# ---------- bets ----------

def place_bet(match_id: int, user_id: int, market: str, side: str, amount: int, odds: float,
              grp: bool = False) -> int:
    get_user(user_id)
    with Tx() as c:
        status = c.execute("SELECT status FROM matches WHERE id = ?", (match_id,)).fetchone()[0]
        if status != "open":
            raise ValueError("Betting is closed for this match.")
        bal = c.execute("SELECT balance FROM users WHERE discord_id = ?", (user_id,)).fetchone()[0]
        if bal < amount:
            raise ValueError(f"You only have {bal:,} coins.")
        c.execute("UPDATE users SET balance = balance - ? WHERE discord_id = ?", (amount, user_id))
        c.execute("INSERT INTO bets (match_id, user_id, market, side, amount, odds, grp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (match_id, user_id, market, side, amount, odds, int(grp)))
        return bal - amount


def cancel_bet(bet_id: int, user_id: int) -> int:
    """Take back your own bet while betting is still open: full refund. Returns the amount. Raises ValueError."""
    with Tx() as c:
        b = c.execute("SELECT * FROM bets WHERE id = ? AND user_id = ?", (bet_id, user_id)).fetchone()
        if not b or b["status"] != "pending":
            raise ValueError("That bet can't be cancelled any more.")
        status = c.execute("SELECT status FROM matches WHERE id = ?", (b["match_id"],)).fetchone()[0]
        if status != "open":
            raise ValueError("Betting has closed, so bets are locked in.")
        c.execute("UPDATE bets SET status = 'refunded', payout = amount WHERE id = ?", (bet_id,))
        c.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (b["amount"], user_id))
        return b["amount"]


def bets_for_match(match_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bets WHERE match_id = ?", (match_id,)).fetchall()


def pending_bets_for_user(user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT b.*, m.host_riot, m.markets FROM bets b JOIN matches m ON m.id = b.match_id "
        "WHERE b.user_id = ? AND b.status = 'pending' ORDER BY b.id",
        (user_id,),
    ).fetchall()


def settle_bets(match_id: int, markets: set[str], judge) -> list[sqlite3.Row] | None:
    """Pay out pending bets in some markets only (e.g. Win/Loss as soon as the final score is known),
    leaving the rest pending for a full settle later. Returns the bets just settled, or None if the
    match isn't active. Marks the match resolved if nothing is left pending."""
    with Tx() as c:
        status = c.execute("SELECT status FROM matches WHERE id = ?", (match_id,)).fetchone()[0]
        if status not in ("open", "locked", "awaiting"):
            return None
        done = []
        pay = _payouts(c, match_id)
        for b in c.execute("SELECT * FROM bets WHERE match_id = ? AND status = 'pending'", (match_id,)).fetchall():
            if b["market"] not in markets:
                continue
            st = judge(b["market"], b["side"])
            if st is None:  # can't be decided yet (e.g. a combo still needs the top frag)
                continue
            payout = pay(b, st)
            c.execute("UPDATE bets SET status = ?, payout = ? WHERE id = ?", (st, payout, b["id"]))
            if payout:
                c.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (payout, b["user_id"]))
            done.append(b["id"])
        # Nothing left: done. Otherwise 'awaiting' the scoreboard: still settled/refunded later,
        # but no longer blocks the player from opening betting on their next game. A Valorant game also
        # waits for its scoreboard with nothing left to pay, so the post-game recap still gets posted.
        left = c.execute("SELECT 1 FROM bets WHERE match_id = ? AND status = 'pending'", (match_id,)).fetchone()
        no_board = c.execute("SELECT 1 FROM matches WHERE id = ? AND game = 'valorant' AND tracker_match_id IS NULL",
                             (match_id,)).fetchone()
        c.execute("UPDATE matches SET status = ? WHERE id = ?", ("awaiting" if left or no_board else "resolved", match_id))
    if not done:
        return []
    return conn.execute(f"SELECT * FROM bets WHERE id IN ({','.join('?' * len(done))})", done).fetchall()


def settle_match(match_id: int, judge, tracker_match_id: str, result: dict) -> list[sqlite3.Row] | None:
    """Pay out every pending bet. judge(market, side) -> 'won' | 'lost' | 'push'.
    Returns the settled bets, or None if already settled."""
    with Tx() as c:
        status = c.execute("SELECT status FROM matches WHERE id = ?", (match_id,)).fetchone()[0]
        if status not in ("open", "locked", "awaiting"):
            return None
        pay = _payouts(c, match_id)
        for b in c.execute("SELECT * FROM bets WHERE match_id = ? AND status = 'pending'", (match_id,)).fetchall():
            st = judge(b["market"], b["side"])
            payout = pay(b, st)
            c.execute("UPDATE bets SET status = ?, payout = ? WHERE id = ?", (st, payout, b["id"]))
            if payout:
                c.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (payout, b["user_id"]))
        c.execute("UPDATE matches SET status = 'resolved', tracker_match_id = ?, result = ? WHERE id = ?",
                  (tracker_match_id, json.dumps(result), match_id))
    return bets_for_match(match_id)


def lock_match(match_id: int, lock_at_iso: str) -> bool:
    """open -> locked. False if the match wasn't open (already locked, settled or cancelled)."""
    return conn.execute("UPDATE matches SET status = 'locked', lock_at = ? WHERE id = ? AND status = 'open'",
                        (lock_at_iso, match_id)).rowcount == 1


def cancel_match(match_id: int) -> int | None:
    """Refund everything. Returns number of bets refunded, or None if the match wasn't open/locked."""
    with Tx() as c:
        row = c.execute("SELECT status FROM matches WHERE id = ?", (match_id,)).fetchone()
        if not row or row[0] not in ("open", "locked", "awaiting"):
            return None
        bets = c.execute("SELECT * FROM bets WHERE match_id = ? AND status = 'pending'", (match_id,)).fetchall()
        for b in bets:
            c.execute("UPDATE bets SET status = 'refunded', payout = amount WHERE id = ?", (b["id"],))
            c.execute("UPDATE users SET balance = balance + ? WHERE discord_id = ?", (b["amount"], b["user_id"]))
        c.execute("UPDATE matches SET status = 'cancelled' WHERE id = ?", (match_id,))
    return len(bets)
