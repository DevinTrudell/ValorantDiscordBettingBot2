"""Discord buttons, menus and pop-up forms: the control panel and the private bet slip.
Betting only ever opens automatically, when a linked player's game starts.

Everything here talks to the bot through `interaction.client` (BetBot), so it has no import of bot.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import discord
from discord import ButtonStyle, SelectOption

import db
import icons
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

INCREMENTS = (10, 50, 100, 500)


def _coins(text: str, balance: int | None = None) -> int | None:
    """A typed amount: a whole number, or "all" / "all in" / "max" for everything you have."""
    t = text.replace(",", "").strip().lower()
    if t in ("all", "all in", "allin", "max") and balance is not None:
        return balance if balance > 0 else None
    try:
        value = int(t)
    except ValueError:
        return None
    return value if value > 0 else None


# ---------- the private betting screens: one card each (Discord's newer message layout) ----------
# Every screen is a Card that redraws itself in place after each click. The flow:
#   Win / Loss  ->  AmountView  ->  TopFragPickView (receipt + optional top frag, or ⏭ Skip)  ->  AmountView  ->  MyBetsView
#   ↩ Start over (most screens): refunds all your bets on the game  ->  StartOverView (pick Win / Loss again)

GREEN_EDGE, RED_EDGE = 0x23A55A, 0xF23F43
STEPS = (500, 100, 50, 10)  # the minus buttons
START_AMOUNT = 100          # a new slip starts here, so betting takes two clicks (the window is short)
T, Sep, Row = discord.ui.TextDisplay, discord.ui.Separator, discord.ui.ActionRow


def _emoji(e):
    return discord.PartialEmoji.from_str(e) if isinstance(e, str) and e else e or None


def _b(label: str, style: ButtonStyle = ButtonStyle.secondary, callback=None, disabled: bool = False, emoji=None):
    b = discord.ui.Button(label=label, style=style, disabled=disabled or callback is None, emoji=_emoji(emoji))
    if callback:
        b.callback = callback
    return b


def card(*items, color: int = GOLD, timeout: float | None = None) -> discord.ui.LayoutView:
    v = discord.ui.LayoutView(timeout=timeout)
    v.add_item(discord.ui.Container(*items, accent_colour=color))
    return v


def closed_card(text: str = "Your bet wasn't placed and no coins were taken. Betting closes before the first "
                            "round ends; catch the next one when a **Bets open** post appears.") -> discord.ui.LayoutView:
    return card(T(f"### 🔒 Betting has closed for this game\n-# {text}"), color=GREY)


def _ranks(m) -> dict[str, str]:
    sc = json.loads(m["scouting"] or "{}")
    return {p["riot_id"].casefold(): p.get("rank") for p in [sc.get("host") or {}, *sc.get("teammates", [])]
            if p.get("riot_id")}


def player_icons(m, mk: dict, riot: str) -> str:
    """Agent + rank icons for a top-frag option, like the recap ('👥' for the random teammates)."""
    if riot == odds.OTHER:
        return "👥"
    o = next((o for o in mk.get("topfrag", []) if o["riot"] == riot), {})
    return (icons.agent(o.get("agent")) + icons.rank(_ranks(m).get(riot.casefold()), fallback=False)) or "🎯"


def bet_icon(m, mk: dict, market: str, side: str) -> str:
    if market == "win":
        return "🟩" if side == "win" else "🟥"
    return player_icons(m, mk, side) if market == "topfrag" else "👥"


def bet_line(m, mk: dict, b) -> str:
    """'🟩 **Win** · 100 ➜ **202** · ×2.02'"""
    return (f"{bet_icon(m, mk, b['market'], b['side'])} **{odds.bet_label(mk, m['host_riot'], b['market'], b['side'])}**"
            f" · {b['amount']:,} ➜ **{int(b['amount'] * b['odds']):,}** · ×{b['odds']:.2f}")


def my_bets(match_id: int, user_id: int) -> list:
    return [b for b in db.bets_for_match(match_id) if b["user_id"] == user_id and b["status"] == "pending"]


def amount_rows(view, get, set_, user_id: int) -> list:
    """−500 −100 −50 −10 ↺ Reset / +10 +50 +100 +500 All in. A minus that would go below 0 is greyed out."""
    def change(n):
        async def cb(inter):
            set_(max(get() + n, 0))
            await view.show(inter)
        return cb

    async def reset(inter):
        set_(0)
        await view.show(inter)

    async def all_in(inter):
        set_(db.get_user(user_id)["balance"])
        await view.show(inter)
    amt = get()
    return [Row(*[_b(f"−{n}", callback=change(-n), disabled=amt < n) for n in STEPS],
                _b("↺ Reset", callback=reset, disabled=not amt)),
            Row(*[_b(f"+{n}", callback=change(n)) for n in INCREMENTS], _b("All in", ButtonStyle.danger, all_in))]


class AmountModal(discord.ui.Modal):
    """Type an exact amount (or "all") for a card with get/set of its amount."""

    def __init__(self, view, get, set_, user_id: int):
        super().__init__(title="How many coins?")
        self.view, self.set_, self.user_id = view, set_, user_id
        bal = db.get_user(user_id)["balance"]
        self.amount = discord.ui.TextInput(label=f"Coins to bet (you have {bal:,})"[:45],
                                           placeholder='e.g. 175, or "all"', max_length=9, required=True)
        self.add_item(self.amount)

    async def on_submit(self, inter: discord.Interaction):
        value = _coins(self.amount.value, db.get_user(self.user_id)["balance"])
        if value is None:
            return await self.view.show(inter, error='Type a whole number of coins, like 100, or "all".')
        self.set_(value)
        await self.view.show(inter)


async def _start_over(inter: discord.Interaction, match_id: int):
    """↩ Start over: cancel every bet you have on this game (full refund), then pick Win / Loss again."""
    try:
        refunded, _ = await inter.client.cancel_all_bets(inter.user, match_id)
    except ValueError as e:
        return await inter.response.edit_message(view=closed_card(str(e)))
    _release_group(match_id, inter.user.id)
    await inter.response.edit_message(view=StartOverView(match_id, inter.user.id, refunded))


class Card(discord.ui.LayoutView):
    """A private betting screen: one card, redrawn after every click. Only works while betting is open."""

    def __init__(self, match_id: int, user_id: int, timeout: float = 900):
        super().__init__(timeout=timeout)
        self.match_id, self.user_id = match_id, user_id
        self.error: str | None = None

    def match(self):
        return db.get_match(self.match_id)

    def markets(self) -> dict:
        return json.loads(self.match()["markets"])

    def balance(self) -> int:
        return db.get_user(self.user_id)["balance"]

    def accent(self) -> int:
        return GOLD

    def items(self) -> list:
        raise NotImplementedError

    def build(self):
        self.clear_items()
        self.add_item(discord.ui.Container(*self.items(), accent_colour=self.accent()))
        return self

    async def show(self, inter: discord.Interaction, error: str | None = None):
        self.error = error
        self.build()
        await inter.response.edit_message(view=self)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if betting_open(self.match()):
            return True
        self.stop()
        await inter.response.edit_message(view=closed_card())
        return False

    def start_over_button(self):
        return _b("↩ Start over", ButtonStyle.danger, lambda inter: _start_over(inter, self.match_id))


class AmountView(Card):
    """How many coins? Starts at 100: adjust with − / + / All in / Type, then ✅ Bet."""

    def __init__(self, match_id: int, market: str, side: str, user_id: int):
        super().__init__(match_id, user_id)
        self.market, self.side = market, side
        bal = self.balance()
        self.amount = min(START_AMOUNT, bal) if bal >= 10 else 0
        self._placing = False
        self.build()

    def price(self) -> float | None:
        mk = self.markets()
        if self.market == "win":
            return mk["win"].get(self.side)
        return next((o["odds"] for o in mk["topfrag"] if o["riot"] == self.side), None)

    def accent(self) -> int:
        return {"win": GREEN_EDGE, "loss": RED_EDGE}.get(self.side, GOLD) if self.market == "win" else GOLD

    def _set(self, v):
        self.amount = max(int(v), 0)

    def items(self) -> list:
        m, mk, bal, price = self.match(), self.markets(), self.balance(), self.price()
        self.shown = {(self.market, self.side): price} if price else {}
        icon = bet_icon(m, mk, self.market, self.side)
        if price is None:
            return [T("### That option isn't available any more\n-# The lineup changed. Pick again from the post.")]
        lines = [f"# {self.amount:,}  ➜  {int(self.amount * price):,}",
                 f"-# bet ➜ pays if right · balance **{bal:,}** · closes {_ts(m['lock_at'])}"]
        if self.amount > bal:
            lines.append(f"⚠️ That's more than your {bal:,} coins.")
        if self.error:
            lines.append(f"⚠️ {self.error}")

        async def typed(inter):
            await inter.response.send_modal(AmountModal(self, lambda: self.amount, self._set, self.user_id))
        return [T(f"### {icon} {odds.bet_label(mk, m['host_riot'], self.market, self.side)} · ×{price:.2f}"), Sep(),
                T("\n".join(lines)),
                *amount_rows(self, lambda: self.amount, self._set, self.user_id),
                Row(_b("✏️ Type", callback=typed),
                    _b(f"✅ Bet {self.amount:,}", ButtonStyle.success,
                       self._place if self.amount and self.amount <= bal else None),
                    self.start_over_button())]

    async def _place(self, inter: discord.Interaction):
        if self._placing or self.is_finished():
            return await inter.response.defer()
        self._placing = True
        try:
            placed, balance = inter.client.place_bets(inter.user.id, self.match_id,
                                                      [(self.market, self.side, self.amount)],
                                                      getattr(self, "shown", None))
        except ValueError as e:
            self._placing = False
            return await self.show(inter, error=str(e))
        self.stop()
        m = self.match()
        nxt = (TopFragPickView(self.match_id, self.user_id, placed=placed[0])
               if self.market == "win" and json.loads(m["markets"]).get("topfrag")
               else MyBetsView(self.match_id, self.user_id))
        await inter.response.edit_message(view=nxt)
        await inter.client.announce_bets(inter.user, m, placed, balance)


async def open_amount(inter: discord.Interaction, m, market: str, side: str):
    await inter.response.send_message(view=AmountView(m["id"], market, side, inter.user.id), ephemeral=True)


class TopFragPickView(Card):
    """The optional team top frag: one row per player (agent + rank icons, chance, odds button).
    After a Win/Loss bet it also shows that bet's receipt."""

    def __init__(self, match_id: int, user_id: int, placed: dict | None = None):
        super().__init__(match_id, user_id)
        self.placed = placed
        self.build()

    def accent(self) -> int:
        return GREEN_EDGE if self.placed else GOLD

    def items(self) -> list:
        m, mk = self.match(), self.markets()
        out = []
        if self.placed:
            out += [T(f"### ✅ Bet placed\n{bet_line(m, mk, self.placed)}\n-# Balance **{self.balance():,}**"), Sep()]
        out.append(T("### 🎯 Team top frag · optional\n-# Who gets the highest score on the team?"))
        ranks = _ranks(m)
        for o in mk.get("topfrag", [])[:5]:
            if o["riot"] == odds.OTHER:
                n = o.get("count") or 1
                about = f"{n} random player{'s' if n != 1 else ''}"
            else:
                about = " · ".join(x for x in (f"{o['agent']} main" if o.get("agent") else "",
                                               ranks.get(o["riot"].casefold()) or "") if x)
            about = (f"{about} · " if about else "") + f"{o['p'] * 100:.0f}% chance"
            out.append(discord.ui.Section(
                T(f"{player_icons(m, mk, o['riot'])} **{odds.option_name(o['riot'], mk)}**\n-# {about}"),
                accessory=_b(f"×{o['odds']:.2f}", ButtonStyle.primary, self._pick(o["riot"]))))
        out.append(Row(*([_b("⏭ Skip top frag", callback=self._skip)] if self.placed else []),
                       self.start_over_button()))
        return out

    async def _skip(self, inter: discord.Interaction):
        self.stop()
        await inter.response.edit_message(view=MyBetsView(self.match_id, self.user_id))

    def _pick(self, riot: str):
        async def cb(inter):
            await inter.response.edit_message(view=AmountView(self.match_id, "topfrag", riot, inter.user.id))
        return cb


class MyBetsView(Card):
    """Receipt for everything you have on this game: each bet, total staked, best case."""

    def __init__(self, match_id: int, user_id: int, title: str = "✅ You're in", note: str | None = None):
        super().__init__(match_id, user_id)
        self.title, self.note = title, note
        self.build()

    def accent(self) -> int:
        return GREEN_EDGE

    def items(self) -> list:
        m, mk = self.match(), self.markets()
        bets = my_bets(self.match_id, self.user_id)
        if not bets:
            return [T("### No bets on this game\n-# Pick Win or Loss on the post to bet.")]
        staked = sum(b["amount"] for b in bets)
        best = sum(int(b["amount"] * b["odds"]) for b in bets)
        out = [T(f"### {self.title}\n" + "\n".join(bet_line(m, mk, b) for b in bets)
                 + (f"\n-# {self.note}" if self.note else "")), Sep(),
               T(f"Staked **{staked:,}** · best case **{best:,}**\n"
                 f"-# Balance **{self.balance():,}** · closes {_ts(m['lock_at'])}")]

        async def to_cancel(inter):
            await inter.response.edit_message(view=CancelBetsView(self.match_id, self.user_id))
        out.append(Row(_b("✖ Cancel a bet", callback=to_cancel)))
        return out


class CancelBetsView(Card):
    """Your bets on this game, each with its own Cancel button (full refund until betting closes)."""

    def __init__(self, match_id: int, user_id: int):
        super().__init__(match_id, user_id, timeout=300)
        self.note: str | None = None
        self.build()

    def mine(self):
        return my_bets(self.match_id, self.user_id)

    def items(self) -> list:
        m, mk = self.match(), self.markets()
        bets = self.mine()
        out = [T(f"### ✖ Your bets\n-# Full refund until betting closes {_ts(m['lock_at'])}.")]
        for b in bets[:8]:
            out.append(discord.ui.Section(
                T(f"{bet_icon(m, mk, b['market'], b['side'])} **{odds.bet_label(mk, m['host_riot'], b['market'], b['side'])}**"
                  f"\n-# {b['amount']:,} at ×{b['odds']:.2f}"),
                accessory=_b("Cancel", ButtonStyle.danger, self._do(b["id"]))))
        if not bets:
            out.append(T("-# No bets left on this game."))
        if self.note:
            out.append(T(self.note))
        if bets:
            out.append(Row(self.start_over_button()))
        return out

    def _do(self, bet_id: int):
        async def cb(inter):
            try:
                bet, balance = await inter.client.cancel_bet(inter.user, self.match_id, bet_id)
                self.note = f"✅ Cancelled: **{bet['amount']:,}** coins refunded. Balance **{balance:,}**."
            except ValueError as e:
                self.note = f"⚠️ {e}"
            await self.show(inter)
        return cb


class StartOverView(Card):
    """After ↩ Start over: what was refunded, then Win / Loss to pick again."""

    def __init__(self, match_id: int, user_id: int, refunded: int):
        super().__init__(match_id, user_id)
        self.refunded = refunded
        self.build()

    def items(self) -> list:
        m, mk = self.match(), self.markets()
        done = (f"All your bets on this game were cancelled: **{self.refunded:,}** refunded." if self.refunded
                else "You had no bets on this game, so there was nothing to refund.")
        playing = self.user_id in {u for u, _ in db.game_players(m)}
        return [T(f"### ↩ Starting over\n{done}\n-# Balance **{self.balance():,}** · closes {_ts(m['lock_at'])}"),
                Sep(), T("### Pick again"),
                Row(*[_b(f"{odds.bet_label(mk, m['host_riot'], 'win', side)} ×{mk['win'][side]:.2f}", style,
                         None if playing and side == "loss" else self._pick(side))
                      for side, style in (("win", ButtonStyle.success), ("loss", ButtonStyle.danger))])]

    def _pick(self, side: str):
        async def cb(inter):
            await inter.response.edit_message(view=AmountView(self.match_id, "win", side, inter.user.id))
        return cb


def _tf_buttons(view: discord.ui.View, mk: dict, chosen: str | None, pick, rows=(1, 2), extra=None):
    """One button per top-frag option (5 per row), the chosen one highlighted; `extra` = (label, callback)."""
    items = [(f"{'✓ ' if chosen == o['riot'] else ''}{odds.option_label(o, mk)} ×{o['odds']:.2f}"[:80],
              ButtonStyle.primary if chosen == o["riot"] else ButtonStyle.secondary, pick(o["riot"]))
             for o in mk["topfrag"]]
    if extra:
        items.append((extra[0], ButtonStyle.primary if chosen is None else ButtonStyle.secondary, extra[1]))
    for i, (label, style, cb) in enumerate(items[:10]):
        view.add_item(_button(label, style, rows[0] if i < 5 else rows[1], cb))


class QuickBetModal(discord.ui.Modal):
    """'How many coins?' straight from a button on the Bets open post: type an amount, the bet is placed."""

    def __init__(self, m, market: str, side: str, user_id: int):
        mk = json.loads(m["markets"])
        price = mk["win"].get(side) if market == "win" else \
            next((o["odds"] for o in mk["topfrag"] if o["riot"] == side), None)
        label = odds.bet_label(mk, m["host_riot"], market, side)
        super().__init__(title=f"{label} ×{price:.2f}"[:45] if price else label[:45])
        self.match_id, self.market, self.side, self.price = m["id"], market, side, price
        bal = db.get_user(user_id)["balance"]
        self.amount = discord.ui.TextInput(label=f"How many coins? (you have {bal:,})"[:45], placeholder="e.g. 100",
                                           max_length=9)
        self.add_item(self.amount)

    async def on_submit(self, inter: discord.Interaction):
        amount = _coins(self.amount.value)
        if amount is None:
            return await inter.response.send_message("⚠️ Type a whole number of coins, like 100.", ephemeral=True)
        m = db.get_match(self.match_id)
        if not betting_open(m):
            return await inter.response.send_message(CLOSED_MSG, ephemeral=True)
        try:
            placed, balance = inter.client.place_bets(inter.user.id, self.match_id, [(self.market, self.side, amount)],
                                                      {(self.market, self.side): self.price} if self.price else None)
        except ValueError as e:
            return await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
        mk, b = json.loads(m["markets"]), placed[0]
        await inter.response.send_message(embed=discord.Embed(
            title="✅ Bet placed",
            description=f"**{odds.bet_label(mk, m['host_riot'], b['market'], b['side'])}** · {b['amount']:,} at "
                        f"×{b['odds']:.2f} → pays **{int(b['amount'] * b['odds']):,}**\n\nBalance **{balance:,}**",
            color=0x2ECC71), ephemeral=True)
        await inter.client.announce_bets(inter.user, m, placed, balance)


CLOSED_MSG = ("🔒 Betting has closed for this game (it closes before the first round ends). Catch the next one when "
              "a **Bets open** post appears.")


class StakeModal(discord.ui.Modal):
    def __init__(self, slip: "SlipView", which: str):
        super().__init__(title="How many coins?")
        self.slip, self.which = slip, which
        bal = db.get_user(slip.user_id)["balance"]
        self.amount = discord.ui.TextInput(label=f"Coins to bet (you have {bal:,})"[:45], placeholder="e.g. 175",
                                           max_length=9)
        self.add_item(self.amount)

    async def on_submit(self, inter: discord.Interaction):
        value = _coins(self.amount.value, db.get_user(self.slip.user_id)["balance"])
        if value is None:
            self.slip.error = 'Type a whole number of coins, or "all".'
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

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        """Any click after betting has closed: say so plainly instead of silently doing nothing."""
        if betting_open(self.match()):
            return True
        self.stop()
        await inter.response.edit_message(
            embed=discord.Embed(title="🔒 Betting has closed for this game",
                                description="Your slip wasn't placed and no coins were taken. Bets close before the "
                                            "first round ends; catch the next one when the **Bets open** post appears.",
                                color=GREY),
            view=None)
        return False

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

        has_topfrag = bool(mk["topfrag"])
        if has_topfrag:  # one button per player (click again to un-pick)
            def pick_tf(riot):
                async def cb(inter):
                    self.tf = None if self.tf == riot else riot
                    await self.refresh(inter)
                return cb
            _tf_buttons(self, mk, self.tf, pick_tf)

        # Amounts are typed in: each button opens a "How many coins?" box.
        self.add_item(_button(f"Result amount: {self.result_stake:,} ✏️", ButtonStyle.secondary, 3,
                              self._custom("win"), disabled=not self.win))
        if has_topfrag:
            self.add_item(_button(f"Team top frag amount: {self.tf_stake:,} ✏️", ButtonStyle.secondary, 3,
                                  self._custom("tf"), disabled=not self.tf))

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
        e = discord.Embed(title="🎟️ Your bet slip",
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


# One group bet per betting page. While someone is setting it up, nobody else can start one:
# match_id -> (user_id, name, expires). The hold ends when they place it, close it, or after GROUP_HOLD.
GROUP_HOLD = 120
_group_setup: dict[int, tuple[int, str, datetime]] = {}


def group_pool_side(match_id: int) -> str | None:
    """The page's group bet (its combo pick), if someone has created it."""
    return next((side for (mkt, side) in db.group_pools(db.bets_for_match(match_id)) if mkt == "combo"), None)


def _release_group(match_id: int, user_id: int):
    held = _group_setup.get(match_id)
    if held and held[0] == user_id:
        _group_setup.pop(match_id, None)


class GroupSlipView(Card):
    """The page's one group bet: a result AND a top fragger (both must be right), paid at both odds multiplied.
    The first person picks it; everyone after joins the same pick. Once the pool is big enough, they all get a
    bonus. Steps: 1 result, 2 player, 3 coins, then Create/Join and an "are you sure?"."""

    def __init__(self, match_id: int, user_id: int):
        super().__init__(match_id, user_id, timeout=GROUP_HOLD)
        self.win: str | None = None
        self.tf: str | None = None
        bal = self.balance()
        self.amount = min(START_AMOUNT, bal) if bal >= 10 else 0
        self._placing = False
        self.joining = group_pool_side(match_id)
        self.confirming = False  # Create/Join asks "are you sure?" first, so it can't be hit by accident
        if self.joining:  # the group bet already exists: its pick is fixed, only the amount is yours
            self.win, self.tf = odds.split_combo(self.joining)
        self.build()

    async def on_timeout(self):
        _release_group(self.match_id, self.user_id)

    def stop(self):
        _release_group(self.match_id, self.user_id)
        super().stop()

    def side(self) -> str | None:
        return odds.combo_side(self.win, self.tf) if self.win and self.tf else None

    def _set(self, v):
        self.amount = max(int(v), 0)

    async def show(self, inter: discord.Interaction, error: str | None = None):
        self.confirming = False  # any change to the pick or amount asks again
        await super().show(inter, error)

    def items(self) -> list:
        m, mk, bal = self.match(), self.markets(), self.balance()
        if self.tf is not None and self.tf not in [o["riot"] for o in mk["topfrag"]]:
            self.tf = None
        is_player = self.user_id in {u for u, _ in db.game_players(m)}  # anyone playing: no betting on a loss
        locked = bool(self.joining)
        intro = ("Someone already created it, so the pick is set: just choose your coins." if locked else
                 f"You're creating this game's group bet (one per game; others can join yours). "
                 f"You have {GROUP_HOLD // 60} minutes.")
        out = [T("### 👥 Group bet\n-# Result **and** team top frag, both must hit. Everyone chips into one pool; "
                 f"a bigger pool earns a bonus.\n-# {intro}"), Sep()]

        def pick_win(side):
            async def cb(inter):
                self.win = side
                await self.show(inter)
            return cb

        def pick_tf(riot):
            async def cb(inter):
                self.tf = riot
                await self.show(inter)
            return cb
        out += [T("**1 · Result**"), Row(*[
            _b(f"{'✓ ' if self.win == side else ''}{label}",
               ButtonStyle.primary if self.win == side else style,
               None if locked else pick_win(side), disabled=locked or (is_player and side == "loss"))
            for side, label, style in (("win", "Win", ButtonStyle.success), ("loss", "Loss", ButtonStyle.danger))])]
        out += [T("**2 · Team top frag**"), Row(*[
            _b(f"{'✓ ' if self.tf == o['riot'] else ''}{odds.option_name(o['riot'], mk)}"[:80],
               ButtonStyle.primary if self.tf == o["riot"] else ButtonStyle.secondary,
               None if locked else pick_tf(o["riot"]), disabled=locked,
               emoji="👥" if o["riot"] == odds.OTHER else icons.agent(o.get("agent")) or None)
            for o in mk["topfrag"][:5]]), Sep()]

        self.shown = {}
        side = self.side()
        price = odds.combo_price(mk, side) if side else None
        coins = ["**3 · Coins**"]
        if side and price:
            self.shown[("combo", side)] = price
            pool = db.group_pools(db.bets_for_match(self.match_id)).get(("combo", side),
                                                                        {"total": 0, "people": 0, "bonus": 0})
            already_in = any(b["user_id"] == self.user_id and b["grp"] and b["side"] == side and b["status"] == "pending"
                             for b in db.bets_for_match(self.match_id))
            after, people = pool["total"] + self.amount, pool["people"] + (0 if already_in else 1)
            bonus = db.group_bonus(after, people)
            win_odds = mk["win"][self.win]
            tf_odds = next(o["odds"] for o in mk["topfrag"] if o["riot"] == self.tf)
            nt = db.next_tier(after)
            coins += [f"# {self.amount:,}  ➜  {int(self.amount * price * (1 + bonus / 100)):,}",
                      f"-# ×{win_odds:.2f} × ×{tf_odds:.2f} = **×{price:.2f}**"
                      + (f" · +{bonus}% pool bonus" if bonus else ""),
                      f"🪙 Pool **{pool['total']:,}** ➜ **{after:,}** with you · {people} "
                      f"{'person' if people == 1 else 'people'}"
                      + (f" · next bonus **+{nt[1]}%** at {nt[0]:,}" if nt else " · top bonus")
                      + (f" (needs {db.GROUP_MIN_PEOPLE}+ people)" if people < db.GROUP_MIN_PEOPLE else "")]
        else:
            coins.append("-# Pick a result and a player first.")
        if self.amount > bal:
            coins.append(f"⚠️ That's more than your {bal:,} coins.")
        if self.confirming:
            coins.append(f"**Are you sure?** Press **⚠️ Yes** to put **{self.amount:,} coins** into the group bet.")
        if self.error:
            coins.append(f"⚠️ {self.error}")
        out.append(T("\n".join(coins)))
        out += amount_rows(self, lambda: self.amount, self._set, self.user_id)

        async def typed(inter):
            await inter.response.send_modal(AmountModal(self, lambda: self.amount, self._set, self.user_id))

        async def back(inter):
            await self.show(inter)
        final = [_b("✏️ Type", callback=typed)]
        if my_bets(self.match_id, self.user_id):
            final.append(self.start_over_button())
        if self.confirming:
            final += [_b(f"⚠️ Yes, put in {self.amount:,}", ButtonStyle.danger, self._place),
                      _b("Back", callback=back)]
        else:
            final.append(_b("👥 Join" if locked else "👥 Create", ButtonStyle.success,
                            self._place if side and price and 0 < self.amount <= bal else None))
        out.append(Row(*final))
        return out

    async def _place(self, inter: discord.Interaction):
        if self._placing or self.is_finished():
            return await inter.response.defer()
        if not self.confirming:  # first click: ask to confirm
            self.error = None
            self.build_confirm()
            return await inter.response.edit_message(view=self)
        self._placing = True
        try:
            placed, balance = inter.client.place_bets(inter.user.id, self.match_id,
                                                      [("combo", self.side(), self.amount)],
                                                      getattr(self, "shown", None), group=True)
        except ValueError as e:
            self._placing = False
            return await self.show(inter, error=str(e))
        m = self.match()
        self.stop()
        pool = placed[0].get("pool") or {"bonus": 0}
        await inter.response.edit_message(view=MyBetsView(
            self.match_id, self.user_id, title="✅ You're in the group bet",
            note=(f"+{pool['bonus']}% pool bonus right now. " if pool["bonus"] else "")
            + "The bonus is set by how big the pool is when betting closes."))
        await inter.client.announce_bets(inter.user, m, placed, balance)

    def build_confirm(self):
        self.confirming = True
        self.clear_items()
        self.add_item(discord.ui.Container(*self.items(), accent_colour=self.accent()))


async def open_group_slip(inter: discord.Interaction, m):
    if not betting_open(m):
        return await inter.response.send_message(CLOSED_MSG, ephemeral=True)
    if not json.loads(m["markets"]).get("topfrag"):
        return await inter.response.send_message("Group bets need a top-frag pick, which this game doesn't have.",
                                                 ephemeral=True)
    if not group_pool_side(m["id"]):  # nobody has created it yet: only one person can be setting it up
        held = _group_setup.get(m["id"])
        if held and held[0] != inter.user.id and _now() < held[2]:
            return await inter.response.send_message(
                f"🔒 **{held[1]}** is setting up this game's group bet right now. Once it's created, the post's button "
                "changes to **👥 Join group bet** if you'd like to join (optional).", ephemeral=True)
        _group_setup[m["id"]] = (inter.user.id, getattr(inter.user, "display_name", "Someone"),
                                 _now() + timedelta(seconds=GROUP_HOLD))
    await inter.response.send_message(view=GroupSlipView(m["id"], inter.user.id), ephemeral=True)


async def open_slip(inter: discord.Interaction, m, win: str | None = None, tf: str | None = None):
    if not betting_open(m):
        return await inter.response.send_message(CLOSED_MSG, ephemeral=True)
    if inter.user.id in {u for u, _ in db.game_players(m)} and win == "loss":
        win = None
    slip = SlipView(m["id"], inter.user.id, win, tf)
    await inter.response.send_message(embed=slip.embed(), view=slip, ephemeral=True)


class SlipButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"vb:slip:(?P<mid>\d+)(?::(?P<side>win|loss|group|tf))?"):
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
        if self.side == "tf":
            if not betting_open(m):
                return await inter.response.send_message(CLOSED_MSG, ephemeral=True)
            if not has_result_bet(self.match_id, inter.user.id):
                return await inter.response.send_message(
                    "🏆 Bet on **Win** or **Loss** first, then pick the team top frag.", ephemeral=True)
            return await inter.response.send_message(view=TopFragPickView(self.match_id, inter.user.id),
                                                     ephemeral=True)
        if self.side in ("win", "loss"):  # straight to "how many coins?"
            if not betting_open(m):
                return await inter.response.send_message(CLOSED_MSG, ephemeral=True)
            if inter.user.id in {u for u, _ in db.game_players(m)} and self.side == "loss":
                return await inter.response.send_message("No betting against yourself 😉", ephemeral=True)
            return await open_amount(inter, m, "win", self.side)
        await open_slip(inter, m, self.side)


class TopFragButton(discord.ui.DynamicItem[discord.ui.Button], template=r"vb:tfb:(?P<mid>\d+):(?P<i>\d+)"):
    """A player's top-frag button on a 'Bets open' post: asks how many coins, then places the bet."""

    def __init__(self, match_id: int, index: int, label: str = "Top frag", style=ButtonStyle.secondary, row=None):
        super().__init__(discord.ui.Button(label=label, style=style, row=row,
                                           custom_id=f"vb:tfb:{match_id}:{index}"))
        self.match_id, self.index = match_id, index

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(int(match["mid"]), int(match["i"]))

    async def callback(self, inter: discord.Interaction):
        m = db.get_match(self.match_id)
        if not betting_open(m):
            return await inter.response.send_message(CLOSED_MSG, ephemeral=True)
        options = json.loads(m["markets"]).get("topfrag", [])
        if not any(b["user_id"] == inter.user.id and b["market"] == "win" and b["status"] == "pending"
                   for b in db.bets_for_match(self.match_id)):
            return await inter.response.send_message(
                "🏆 Bet on **Win** or **Loss** first, then pick the team top frag.", ephemeral=True)
        if self.index >= len(options):
            return await inter.response.send_message("That option isn't available any more.", ephemeral=True)
        await open_amount(inter, m, "topfrag", options[self.index]["riot"])


class TopFragSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"vb:tf:(?P<mid>\d+)"):
    """The top-frag menu right on a 'Bets open' post: picking someone opens your slip with them chosen."""

    def __init__(self, match_id: int, options: list[SelectOption] | None = None):
        super().__init__(discord.ui.Select(custom_id=f"vb:tf:{match_id}", options=options or
                                           [SelectOption(label="(loading)", value="none")],
                                           placeholder="🎯 Bet on the team top frag"))
        self.match_id = match_id

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        made = cls(int(match["mid"]), item.options)
        made.item = item  # keep the values the person just picked
        return made

    async def callback(self, inter: discord.Interaction):
        await open_slip(inter, db.get_match(self.match_id), tf=self.item.values[0] if self.item.values else None)


class CancelBetsButton(discord.ui.DynamicItem[discord.ui.Button], template=r"vb:cancel:(?P<mid>\d+)"):
    """'↩️ Cancel my bets' on a Bets open post."""

    def __init__(self, match_id: int):
        super().__init__(discord.ui.Button(label="✖ My bets", style=ButtonStyle.secondary,
                                           custom_id=f"vb:cancel:{match_id}"))
        self.match_id = match_id

    @classmethod
    async def from_custom_id(cls, inter, item, match):
        return cls(int(match["mid"]))

    async def callback(self, inter: discord.Interaction):
        m = db.get_match(self.match_id)
        if not betting_open(m):
            return await inter.response.send_message("🔒 Betting has closed, so bets are locked in.", ephemeral=True)
        if not my_bets(self.match_id, inter.user.id):
            return await inter.response.send_message("You don't have any bets on this game.", ephemeral=True)
        await inter.response.send_message(view=CancelBetsView(self.match_id, inter.user.id), ephemeral=True)


def has_result_bet(match_id: int, user_id: int) -> bool:
    return any(b["user_id"] == user_id and b["market"] == "win" and b["status"] == "pending"
               for b in db.bets_for_match(match_id))


def market_rows(m) -> list:
    """The buttons on the 'Bets open' card: Win / Loss, then Team top frag, Group bet and My bets.
    Persistent: they keep working after the bot restarts."""
    mk = json.loads(m["markets"])
    extra = []
    if mk.get("topfrag"):
        extra.append(SlipButton(m["id"], "tf", "🎯 Team top frag", ButtonStyle.secondary))
        joinable = group_pool_side(m["id"])  # optional: nobody is in it unless they press this themselves
        extra.append(SlipButton(m["id"], "group", "👥 Join group bet" if joinable else "👥 Group bet",
                                ButtonStyle.success if joinable else ButtonStyle.secondary))
    extra.append(CancelBetsButton(m["id"]))
    label = lambda side: f"{odds.bet_label(mk, m['host_riot'], 'win', side)} ×{mk['win'][side]:.2f}"
    return [Row(SlipButton(m["id"], "win", label("win"), ButtonStyle.success),
                SlipButton(m["id"], "loss", label("loss"), ButtonStyle.danger)),
            Row(*extra)]


def market_view(m) -> discord.ui.View:
    """(Old-style posts, from before the card layout) The buttons under an embed 'Bets open' post."""
    mk = json.loads(m["markets"])
    v = discord.ui.View(timeout=None)
    v.add_item(SlipButton(m["id"], "win", f"Win ×{mk['win']['win']:.2f}", ButtonStyle.success))
    v.add_item(SlipButton(m["id"], "loss", f"Loss ×{mk['win']['loss']:.2f}", ButtonStyle.danger))
    if mk.get("topfrag"):  # top frag + group bets
        v.add_item(SlipButton(m["id"], "tf", "🎯 Team top frag", ButtonStyle.secondary))
        joinable = group_pool_side(m["id"])  # optional: nobody is in it unless they press this themselves
        v.add_item(SlipButton(m["id"], "group", "👥 Join group bet" if joinable else "👥 Create group bet",
                              ButtonStyle.success if joinable else ButtonStyle.secondary))
    v.add_item(CancelBetsButton(m["id"]))
    return v


# ---------- the panel (info only; betting happens on each 'Bets open' post, all automatic) ----------

def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="🎮 Valorant & Overwatch betting" if db.OVERWATCH else "🎮 Valorant betting",
        description=(
            ("**1.** Players link once: `/link Name#TAG` (Riot ID) and/or `/link-overwatch Name#1234` (BattleTag; "
             "Overwatch bets are on your whole session).\n" if db.OVERWATCH else
             "**1.** Players link once: `/link Name#TAG` (Riot ID).\n") +
            "**2.** When a linked player's game starts, a **Bets open** post appears here by itself.\n"
            "**3.** Anyone in the server can bet, playing or not: press **Win**, **Loss**, a top fragger or "
            "**👥 Group bet** on that post. Both bets are optional.\n"
            "**4.** Betting closes before the first round ends. After the game the bot pays out automatically.\n\n"
            f"💰 Everyone starts with **{db.STARTING_BALANCE:,} coins** and gets **{db.DAILY_AMOUNT:,} free coins every "
            "day** (at midnight UTC). Coins are play money.\n\n"
            "📖 Type **/info** for everything in detail."),
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
