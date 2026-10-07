"""Discord buttons, menus and pop-up forms: the control panel and the private bet slip.
Betting only ever opens automatically, when a linked player's game starts.

Everything here talks to the bot through `interaction.client` (BetBot), so it has no import of bot.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import discord
from discord import ButtonStyle, SelectOption

import db
import odds

RESULT_STAKES = (50, 100, 250)
TOPFRAG_STAKES = (25, 50, 100)
GOLD, GREY, RED = 0xF1C40F, 0x95A5A6, 0xFF4655


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _short(riot: str) -> str:
    return riot.split("#")[0]


def _ts(iso: str) -> str:
    return f"<t:{int(datetime.fromisoformat(iso).timestamp())}:R>"


def betting_open(m) -> bool:
    return m is not None and m["status"] == "open" and _now() < datetime.fromisoformat(m["lock_at"])


def _button(label: str, style: ButtonStyle, row: int, callback=None, disabled: bool = False, emoji=None):
    b = discord.ui.Button(label=label, style=style, row=row, disabled=disabled or callback is None, emoji=emoji)
    if callback:
        b.callback = callback
    return b


# ---------- picking which match a button applies to ----------

class MatchPicker(discord.ui.View):
    def __init__(self, matches, callback):
        super().__init__(timeout=120)
        select = discord.ui.Select(placeholder="Which match?", options=[
            SelectOption(label=f"#{m['id']} · {_short(m['host_riot'])}'s game"[:100], value=str(m["id"]),
                         description="Betting open" if m["status"] == "open" else "Game in progress")
            for m in matches[:25]])

        async def chosen(inter: discord.Interaction):
            await callback(inter, db.get_match(int(select.values[0])))
        select.callback = chosen
        self.add_item(select)


async def with_match(inter: discord.Interaction, callback, statuses=("open", "locked", "awaiting"), none_msg=None):
    matches = [m for m in db.active_matches(inter.channel_id) if m["status"] in statuses]
    if not matches:
        return await inter.response.send_message(
            none_msg or "No active match in this channel. Betting opens by itself when a linked player's game "
                        "starts.", ephemeral=True)
    if len(matches) == 1:
        return await callback(inter, matches[0])
    await inter.response.send_message("Several matches are active here:", view=MatchPicker(matches, callback),
                                      ephemeral=True)


# ---------- the private bet slip ----------

class StakeModal(discord.ui.Modal):
    def __init__(self, slip: "SlipView", which: str):
        super().__init__(title="Custom stake")
        self.slip, self.which = slip, which
        self.amount = discord.ui.TextInput(label="Coins to bet", placeholder="e.g. 175", max_length=9)
        self.add_item(self.amount)

    async def on_submit(self, inter: discord.Interaction):
        try:
            value = int(self.amount.value.replace(",", "").strip())
            if value <= 0:
                raise ValueError
        except ValueError:
            self.slip.error = "Type a whole number of coins."
        else:
            setattr(self.slip, "result_stake" if self.which == "win" else "tf_stake", value)
            self.slip.error = None
        self.slip.build()
        await inter.response.edit_message(embed=self.slip.embed(), view=self.slip)


class SlipView(discord.ui.View):
    """Pick a result and/or a top fragger (both optional), set a stake for each, then place."""

    def __init__(self, match_id: int, user_id: int, win: str | None = None, tf: str | None = None):
        super().__init__(timeout=900)
        self.match_id, self.user_id = match_id, user_id
        self.win: str | None = win
        self.tf: str | None = tf  # a top-frag option: a player's Riot ID, or odds.OTHER
        self.result_stake, self.tf_stake = 100, 50
        self.error: str | None = None
        self._placing = False
        self.build()

    def match(self):
        return db.get_match(self.match_id)

    def markets(self) -> dict:
        return json.loads(self.match()["markets"])

    def picks(self) -> list[tuple[str, str, int]]:
        out = []
        if self.win:
            out.append(("win", self.win, self.result_stake))
        if self.tf:
            out.append(("topfrag", self.tf, self.tf_stake))
        return out

    def price_of(self, market: str, side: str) -> float | None:
        mk = self.markets()
        if market == "win":
            return mk["win"].get(side)
        return next((o["odds"] for o in mk["topfrag"] if o["riot"] == side), None)

    def build(self):
        self.clear_items()
        mk = self.markets()
        if self.tf is not None and self.tf not in [o["riot"] for o in mk["topfrag"]]:
            self.tf = None  # markets can be repriced while a slip is open

        def pick_win(side):
            async def cb(inter):
                self.win = None if self.win == side else side  # click again to un-pick
                await self.refresh(inter)
            return cb

        for side, label, style in (("win", "Win", ButtonStyle.success), ("loss", "Loss", ButtonStyle.danger)):
            chosen = self.win == side
            self.add_item(_button(f"{'✓ ' if chosen else ''}{label} ×{mk['win'][side]:.2f}",
                                  ButtonStyle.primary if chosen else style, 0, pick_win(side)))
        self.add_item(_button("No result bet", ButtonStyle.primary if self.win is None else ButtonStyle.secondary,
                              0, pick_win(self.win) if self.win else None))

        has_topfrag = bool(mk["topfrag"])  # Overwatch games are Win/Loss only
        if has_topfrag:
            # Every player on the team gets their own entry, listed the same way.
            options = [SelectOption(label=f"{odds.option_label(o, mk)} top frags"[:100], value=o["riot"][:100],
                                    description=f"Pays ×{o['odds']:.2f}", default=self.tf == o["riot"])
                       for o in mk["topfrag"]]
            options.append(SelectOption(label="No top frag bet", value="none", default=self.tf is None))
            select = discord.ui.Select(placeholder="Top frag on the team: highest ACS (optional)", row=1,
                                       options=options)

            async def pick_tf(inter):
                self.tf = None if select.values[0] == "none" else select.values[0]
                await self.refresh(inter)
            select.callback = pick_tf
            self.add_item(select)

        stake_rows = [(2, "win", RESULT_STAKES, self.result_stake, self.win)]
        if has_topfrag:
            stake_rows.append((3, "tf", TOPFRAG_STAKES, self.tf_stake, self.tf))
        for row, which, stakes, current, active in stake_rows:
            self.add_item(_button("Result stake" if which == "win" else "Top frag stake", ButtonStyle.secondary, row))
            for amt in stakes:
                self.add_item(_button(str(amt), ButtonStyle.primary if current == amt else ButtonStyle.secondary,
                                      row, self._set_stake(which, amt), disabled=not active))
            custom = current not in stakes
            self.add_item(_button(f"{current:,}…" if custom else "Custom…",
                                  ButtonStyle.primary if custom else ButtonStyle.secondary, row,
                                  self._custom(which), disabled=not active))

        n = len(self.picks())
        self.add_item(_button("Place 2 bets" if n == 2 else "Place bet", ButtonStyle.success, 4,
                              self._place if n else None))
        self.add_item(_button("Clear", ButtonStyle.secondary, 4, self._clear if n else None))

    def _set_stake(self, which, amt):
        async def cb(inter):
            setattr(self, "result_stake" if which == "win" else "tf_stake", amt)
            await self.refresh(inter)
        return cb

    def _custom(self, which):
        async def cb(inter):
            await inter.response.send_modal(StakeModal(self, which))
        return cb

    async def _clear(self, inter):
        self.win = self.tf = None
        await self.refresh(inter)

    async def refresh(self, inter):
        self.error = None
        self.build()
        await inter.response.edit_message(embed=self.embed(), view=self)

    def embed(self) -> discord.Embed:
        m, mk = self.match(), self.markets()
        bal = db.get_user(self.user_id)["balance"]
        e = discord.Embed(title=f"🎟️ Your bet slip: {_short(m['host_riot'])}'s game",
                          description=f"Closes {_ts(m['lock_at'])} · Balance **{bal:,}**"
                                      + ("\nBoth bets are optional and pay out separately." if mk["topfrag"] else ""),
                          color=GOLD)
        lines = []
        self.shown = {}  # the odds this slip is showing; placing fails if they've dropped since
        for market, side, stake in self.picks():
            o = self.price_of(market, side)
            if o is None:
                continue
            self.shown[(market, side)] = o
            lines.append(f"**{odds.bet_label(mk, m['host_riot'], market, side)}** · {stake:,} → pays **{int(stake * o):,}**")
        if lines:
            total = sum(s for _, _, s in self.picks())
            lines.append(f"Total stake **{total:,}**")
        e.add_field(name="Your picks", value="\n".join(lines) or "Nothing picked. You can sit this one out.", inline=False)
        if self.error:
            e.add_field(name="⚠️", value=self.error, inline=False)
        return e

    async def _place(self, inter: discord.Interaction):
        if self._placing or self.is_finished():
            return await inter.response.defer()
        self._placing = True
        try:
            placed, balance = inter.client.place_bets(inter.user.id, self.match_id, self.picks(),
                                                      getattr(self, "shown", None))
        except ValueError as e:
            self._placing = False
            self.error = str(e)
            self.build()
            return await inter.response.edit_message(embed=self.embed(), view=self)
        m, mk = self.match(), self.markets()
        lines = [f"**{odds.bet_label(mk, m['host_riot'], b['market'], b['side'])}** · {b['amount']:,} at "
                 f"×{b['odds']:.2f} → pays **{int(b['amount'] * b['odds']):,}**" for b in placed]
        done = discord.Embed(title="✅ Bets placed", description="\n".join(lines) + f"\n\nBalance **{balance:,}**",
                             color=0x2ECC71)
        self.stop()
        await inter.response.edit_message(embed=done, view=None)
        await inter.client.announce_bets(inter.user, m, placed, balance)


class GroupSlipView(discord.ui.View):
    """Group bet: a result AND a top fragger (both must be right), paid at both odds multiplied together.
    Everyone on the same pick shares a pool; once the pool is big enough, they all get a bonus."""

    def __init__(self, match_id: int, user_id: int):
        super().__init__(timeout=900)
        self.match_id, self.user_id = match_id, user_id
        self.win: str | None = None
        self.tf: str | None = None
        self.result_stake = 100  # (named like SlipView's so StakeModal works for both)
        self.error: str | None = None
        self._placing = False
        self.build()

    def match(self):
        return db.get_match(self.match_id)

    def markets(self) -> dict:
        return json.loads(self.match()["markets"])

    def side(self) -> str | None:
        return odds.combo_side(self.win, self.tf) if self.win and self.tf else None

    def pools(self) -> dict:
        return db.group_pools(db.bets_for_match(self.match_id))

    def build(self):
        self.clear_items()
        m, mk = self.match(), self.markets()
        if self.tf is not None and self.tf not in [o["riot"] for o in mk["topfrag"]]:
            self.tf = None
        is_host = self.user_id == m["host_id"]

        def pick_win(side):
            async def cb(inter):
                self.win = side
                await self.refresh(inter)
            return cb

        for side, label, style in (("win", "Win", ButtonStyle.success), ("loss", "Loss", ButtonStyle.danger)):
            chosen = self.win == side
            self.add_item(_button(f"{'✓ ' if chosen else ''}{label}", ButtonStyle.primary if chosen else style, 0,
                                  pick_win(side), disabled=is_host and side == "loss"))

        select = discord.ui.Select(placeholder="…and who top frags on the team (highest ACS)", row=1, options=[
            SelectOption(label=f"{odds.option_label(o, mk)} top frags"[:100], value=o["riot"][:100],
                         default=self.tf == o["riot"]) for o in mk["topfrag"]])

        async def pick_tf(inter):
            self.tf = select.values[0]
            await self.refresh(inter)
        select.callback = pick_tf
        self.add_item(select)

        self.add_item(_button("Stake", ButtonStyle.secondary, 2))
        for amt in RESULT_STAKES:
            self.add_item(_button(str(amt), ButtonStyle.primary if self.result_stake == amt else ButtonStyle.secondary,
                                  2, self._set_stake(amt)))
        custom = self.result_stake not in RESULT_STAKES
        self.add_item(_button(f"{self.result_stake:,}…" if custom else "Custom…",
                              ButtonStyle.primary if custom else ButtonStyle.secondary, 2, self._custom))

        pools = [(s, p) for (mkt, s), p in self.pools().items() if mkt == "combo"
                 and not (is_host and odds.split_combo(s)[0] == "loss")]
        if pools:
            join = discord.ui.Select(placeholder="…or join an existing group pool", row=3, options=[
                SelectOption(label=odds.bet_label(mk, m["host_riot"], "combo", s)[:100], value=s[:100],
                             description=f"{p['total']:,} coins · {p['people']} people"
                                         + (f" · +{p['bonus']}%" if p["bonus"] else ""),
                             default=s == self.side())
                for s, p in sorted(pools, key=lambda sp: -sp[1]["total"])[:25]])

            async def pick_pool(inter):
                self.win, self.tf = odds.split_combo(join.values[0])
                await self.refresh(inter)
            join.callback = pick_pool
            self.add_item(join)

        self.add_item(_button("👥 Join group bet", ButtonStyle.success, 4, self._place if self.side() else None))

    def _set_stake(self, amt):
        async def cb(inter):
            self.result_stake = amt
            await self.refresh(inter)
        return cb

    async def _custom(self, inter):
        await inter.response.send_modal(StakeModal(self, "win"))

    async def refresh(self, inter):
        self.error = None
        self.build()
        await inter.response.edit_message(embed=self.embed(), view=self)

    def embed(self) -> discord.Embed:
        m, mk = self.match(), self.markets()
        bal = db.get_user(self.user_id)["balance"]
        tiers = ", ".join(f"**+{pct}%** from {need:,}" for need, pct in db.GROUP_TIERS)
        e = discord.Embed(
            title=f"👥 Group bet: {_short(m['host_riot'])}'s game",
            description=(f"Closes {_ts(m['lock_at'])} · Balance **{bal:,}**\n"
                         "Pick a result **and** who top frags. Both must be right; it pays both odds multiplied. "
                         f"Everyone on the same pick shares a pool, and once it's big enough (with at least "
                         f"{db.GROUP_MIN_PEOPLE} people) they all get a bonus on their winnings: {tiers}."),
            color=GOLD)
        self.shown = {}
        side = self.side()
        price = odds.combo_price(mk, side) if side else None
        if side and price:
            self.shown[("combo", side)] = price
            pool = self.pools().get(("combo", side), {"total": 0, "people": 0, "bonus": 0})
            after = {"total": pool["total"] + self.result_stake,
                     "people": pool["people"] + (0 if any(b["user_id"] == self.user_id and b["grp"] and
                                                         b["side"] == side for b in db.bets_for_match(self.match_id))
                                                 else 1)}
            bonus = db.group_bonus(after["total"], after["people"])
            value = (f"**{odds.bet_label(mk, m['host_riot'], 'combo', side)}** at ×{price:.2f}\n"
                     f"{self.result_stake:,} → pays **{int(self.result_stake * price):,}**"
                     + (f", or **{int(self.result_stake * price * (1 + bonus / 100)):,}** with the pool's "
                        f"+{bonus}% bonus as it stands after you join" if bonus else "")
                     + f"\nPool now: {pool['total']:,} coins from {pool['people']} "
                       f"{'person' if pool['people'] == 1 else 'people'}"
                     + (f" · next bonus +{nt[1]}% at {nt[0]:,}" if (nt := db.next_tier(after["total"])) else ""))
        else:
            value = "Pick Win or Loss **and** a top fragger, or join a pool someone already started."
        e.add_field(name="Your pick", value=value, inline=False)
        if self.error:
            e.add_field(name="⚠️", value=self.error, inline=False)
        return e

    async def _place(self, inter: discord.Interaction):
        if self._placing or self.is_finished():
            return await inter.response.defer()
        self._placing = True
        try:
            placed, balance = inter.client.place_bets(inter.user.id, self.match_id,
                                                      [("combo", self.side(), self.result_stake)],
                                                      getattr(self, "shown", None), group=True)
        except ValueError as e:
            self._placing = False
            self.error = str(e)
            self.build()
            return await inter.response.edit_message(embed=self.embed(), view=self)
        m, mk = self.match(), self.markets()
        b = placed[0]
        pool = b.get("pool") or {"total": b["amount"], "people": 1, "bonus": 0}
        done = discord.Embed(
            title="✅ You're in the group bet",
            description=(f"**{odds.bet_label(mk, m['host_riot'], b['market'], b['side'])}** · {b['amount']:,} at "
                         f"×{b['odds']:.2f} → pays **{int(b['amount'] * b['odds']):,}**"
                         + (f" (+{pool['bonus']}% bonus right now)" if pool["bonus"] else "")
                         + f"\nThe bonus is set by how big the pool is when betting closes.\n\nBalance **{balance:,}**"),
            color=0x2ECC71)
        self.stop()
        await inter.response.edit_message(embed=done, view=None)
        await inter.client.announce_bets(inter.user, m, placed, balance)


async def open_group_slip(inter: discord.Interaction, m):
    if not betting_open(m):
        return await inter.response.send_message("Betting is closed for this match.", ephemeral=True)
    if not json.loads(m["markets"]).get("topfrag"):
        return await inter.response.send_message("Group bets need a top-frag pick, so they're Valorant only.",
                                                 ephemeral=True)
    slip = GroupSlipView(m["id"], inter.user.id)
    await inter.response.send_message(embed=slip.embed(), view=slip, ephemeral=True)


async def open_slip(inter: discord.Interaction, m, win: str | None = None, tf: str | None = None):
    if not betting_open(m):
        return await inter.response.send_message("Betting is closed for this match.", ephemeral=True)
    if inter.user.id == m["host_id"] and win == "loss":
        win = None
    slip = SlipView(m["id"], inter.user.id, win, tf)
    await inter.response.send_message(embed=slip.embed(), view=slip, ephemeral=True)


class SlipButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"vb:slip:(?P<mid>\d+)(?::(?P<side>win|loss|group))?"):
    """Buttons on a 'Bets open' post. Persistent: they keep working after the bot restarts."""

    def __init__(self, match_id: int, side: str | None = None, label: str = "Bet",
                 style: ButtonStyle = ButtonStyle.primary):
        super().__init__(discord.ui.Button(label=label, style=style,
                                           custom_id=f"vb:slip:{match_id}" + (f":{side}" if side else "")))
        self.match_id, self.side = match_id, side

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(int(match["mid"]), match["side"])

    async def callback(self, inter: discord.Interaction):
        m = db.get_match(self.match_id)
        if self.side == "group":
            return await open_group_slip(inter, m)
        await open_slip(inter, m, self.side)


class TopFragSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"vb:tf:(?P<mid>\d+)"):
    """The top-frag menu right on a 'Bets open' post: picking someone opens your slip with them chosen."""

    def __init__(self, match_id: int, options: list[SelectOption] | None = None):
        super().__init__(discord.ui.Select(custom_id=f"vb:tf:{match_id}", options=options or
                                           [SelectOption(label="(loading)", value="none")],
                                           placeholder="🎯 Bet on who top frags (highest ACS)"))
        self.match_id = match_id

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        made = cls(int(match["mid"]), item.options)
        made.item = item  # keep the values the person just picked
        return made

    async def callback(self, inter: discord.Interaction):
        await open_slip(inter, db.get_match(self.match_id), tf=self.item.values[0] if self.item.values else None)


def market_view(m) -> discord.ui.View:
    """The betting page right on the 'Bets open' post: every control opens your private slip with that pick
    already filled in (Discord only shows private messages after a click)."""
    mk = json.loads(m["markets"])
    v = discord.ui.View(timeout=None)
    v.add_item(SlipButton(m["id"], "win", f"Win ×{mk['win']['win']:.2f}", ButtonStyle.success))
    v.add_item(SlipButton(m["id"], "loss", f"Loss ×{mk['win']['loss']:.2f}", ButtonStyle.danger))
    if mk.get("topfrag"):  # Valorant: top frag menu + group bets (Overwatch is Win/Loss only)
        v.add_item(SlipButton(m["id"], "group", "👥 Group bet", ButtonStyle.secondary))
        v.add_item(TopFragSelect(m["id"], [
            SelectOption(label=f"{odds.option_label(o, mk)} · ×{o['odds']:.2f}"[:100], value=o["riot"][:100])
            for o in mk["topfrag"][:25]]))
    return v


# ---------- the panel (info only; betting happens on each 'Bets open' post, all automatic) ----------

def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="🎮 Valorant & Overwatch betting",
        description=(
            "**1.** Players link once: `/link Name#TAG` (Riot ID) and/or `/link-overwatch Name#1234` (BattleTag).\n"
            "**2.** When a linked player's game starts, a **Bets open** post appears here by itself.\n"
            "**3.** Anyone in the server can bet, playing or not: press **Win**, **Loss**, a top fragger or "
            "**👥 Group bet** on that post. Both bets are optional.\n"
            "**4.** Betting closes before the first round ends. After the game the bot pays out automatically.\n\n"
            f"💰 Everyone starts with **{db.STARTING_BALANCE:,} coins** and gets **{db.DAILY_AMOUNT:,} free coins every "
            "day** (at midnight UTC). Coins are play money."),
        color=RED)


class RetiredPanelButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"vb:panel:(?P<what>open|lock|settle|cancel|bet)"):
    """Old panels' buttons: betting is automatic now and happens on each 'Bets open' post."""

    def __init__(self, what: str = "open"):
        super().__init__(discord.ui.Button(label="Old button", custom_id=f"vb:panel:{what}"))
        self.what = what

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(match["what"])

    async def callback(self, inter: discord.Interaction):
        if self.what == "bet":
            return await with_match(inter, open_slip, statuses=("open",),
                                    none_msg="No betting is open right now. A **Bets open** post appears here "
                                             "when a linked player's game starts.")
        await inter.response.send_message(
            "Betting opens, closes and pays out automatically now; nobody can change a game's bets by hand. "
            "This panel is out of date: run `/panel` to post the new one (and delete this one).", ephemeral=True)
