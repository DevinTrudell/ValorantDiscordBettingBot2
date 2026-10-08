# Valorant Betting Bot

A Discord bot where friends bet **play-money coins** on someone's upcoming Valorant game, using
buttons and menus right in Discord. Stats come from the HenrikDev API (tracker.gg is an optional backup).
Bets settle on their own once the game shows up in the player's match history, and the bot then posts the
full scoreboard, including every opponent's rank, agent, K/D/A and Performance Score.

## Bets

| Bet | Options | How it's priced |
|---|---|---|
| Match result | Win / Loss | The group's overall win rate, plus team vs opponent rank and K/D if opponents are known |
| Top frag on the player's team (highest ACS; kills break an exact ACS tie; equal ACS and kills refunds) | The player, each listed teammate, or "someone else on the team" | Each player's ACS and K/D |

Both bets are optional and pay out separately. Odds include a 5% house edge. The player can't bet on
their own team losing.

**Top-frag options:** the player, their party (friends Discord shows in the party, plus whoever queued with them
last game and regular teammates, from HenrikDev/tracker.gg), each shown with their usual agent when known, and
**Other teammate(s)** for the randoms that fill the team (no option if it's a 5-stack). If a listed teammate isn't
in the game after all (e.g. they left the party), bets on them are refunded and the other top-frag bets are paid
at odds re-priced for the real lineup, like a bookmaker's adjustment for a withdrawn runner.

**👥 Group bets (Valorant):** pick a result **and** a top fragger together (e.g. "wins + Sam top frags"). Both
must be right, and it pays both odds multiplied. Everyone who picks the same combination shares a pool, and
once the pool is big enough (with at least `GROUP_MIN_PEOPLE` people, default 2) everyone in it gets a bonus
on their winnings: by default +10% from 500 coins, +20% from 1,000, +30% from 2,000 (`GROUP_TIERS`,
e.g. `500:10,1000:20,2000:30`). The bonus is set by the pool's size when betting closes. If the result is
wrong the bet is lost right away; if it's right it waits for the top frag; a draw or a top-frag tie refunds it.

### How the odds are worked out

- **The group's win rate** (`GROUP_WIN_RATE`, default 0.47). The odds were tested by replaying ~230 of the
  group's past games: a player's own win rate, streak and map record didn't predict results (matchmaking
  evens them out), so they're shown on the post but don't move the odds.
- **Opponents' ranks and K/D** when the Overwolf app names them (±8% at most).
- **Mode:** Swiftplay (first to 5) and Spike Rush (first to 4) leave more to luck than a full game, so the
  same edge gives odds closer to even.
- **Self-tuning:** every prediction is saved with the result. After 30 games the bot learns how far off it
  tends to be (e.g. too confident) and corrects for it. `/accuracy` shows how it's doing.
- **Top frag:** simulates the game thousands of times from each player's average ACS and how much it swings
  from game to game (from their last 10 games), so a steady player and a streaky one with the same average get
  different odds. Random teammates count as 213 ACS, the average measured in the group's games.
- **The bets themselves:** odds shift a little towards where the money goes (at most ±5%, set by
  `CROWD_WEIGHT`, default 4000 coins), ignoring the player's own bets. Every bet keeps the odds it was
  placed at; if the odds drop while your slip is open, you're asked to confirm. You can add to a bet but
  not also bet against it in the same game.

## Setup

1. **Discord app:** at https://discord.com/developers/applications, create an app, open **Bot**, and copy the token.
   Turn on **Presence Intent** and **Server Members Intent** (used to read players' Valorant status;
   set `PRESENCE_TRACKING=0` in `.env` to skip this).
   Under **OAuth2 → URL Generator**, tick the `bot` and `applications.commands` scopes and the
   *View Channels*, *Send Messages*, *Embed Links* and *Read Message History* permissions. Open the generated URL to invite the bot.
2. **Install:**
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env
   ```
   Put `DISCORD_TOKEN`, `HENRIK_API_KEY` and `GUILD_ID` in `.env` (`TRACKER_API_KEY` is an optional backup;
   `python check_tracker.py "YourName#TAG"` tests it).
4. **Run:** `python bot.py`. To run it 24/7 on a home server (e.g. a Proxmox container), see
   [deploy/DEPLOY.md](deploy/DEPLOY.md).
5. In your betting channel, run `/panel` once and pin the message it posts.

## Using it

1. Each player runs `/link Name#TAG` once (or a server manager uses `/link-for`).
2. Betting opens **only automatically**: when a linked player's Discord status (or the Overwolf app)
   shows a match starting. There's no way to open it by hand, so nobody can bet on a game that isn't happening.
3. Anyone in the server can bet, playing or not (no `/link` needed to bet). The **Bets open** post is the
   betting page: **Win**, **Loss**, a top-frag menu and **👥 Group bet**. Each opens your private bet slip
   with that pick already filled in.
4. Betting closes before the first round ends. After the game the bot pays out from the final score
   (and the full scoreboard from HenrikDev for top frag).

Nobody, including server admins, can open, close, settle or cancel a game's bets by hand: it's all automatic.
If a game's result never arrives (left early, remake, data source down), every bet still waiting is refunded
after `MATCH_TIMEOUT_HOURS`. The `/panel` message is just instructions; betting happens on each **Bets open** post.

Everyone who has used the bot gets 50 free coins automatically once a day (at midnight UTC, or as soon as the
bot starts if it was offline). It happens silently, with no message in any channel.

Commands: `/panel`, `/link`, `/bet` (opens your bet slip), `/match status`, `/balance`, `/give`, `/leaderboard`, `/mybets`, `/scout Name#TAG`, `/accuracy`,
`/unlink` (stop the bot following your games; coins are kept), and `/link-for @user Name#TAG`
(server managers: link a friend so they don't have to).

### Automatic betting

- **Discord status:** for players who ran `/link`, the bot reads their Valorant status, which shows
  the mode, map and live score once a match starts (e.g. "Swiftplay (Ascent) 3 - 1", their team first).
  - Betting opens by itself when the score first appears ("0 - 0"), in the `/panel` channel, with
    linked friends in the same Valorant party as teammates.
  - Betting closes after `AUTO_OPEN_WINDOW_SECONDS`, or as soon as the first round's result shows,
    whichever is first.
  - When the game ends, Win/Loss bets are paid straight away from the final score. Top-frag bets need
    the full scoreboard, so they wait for HenrikDev (or are refunded after `MATCH_TIMEOUT_HOURS`).
  - Custom games, Deathmatch, Skirmish (2v2) and the Range are skipped.
  - `PRESENCE_TRACKING` (default `1`): read Valorant status; `0` turns this and auto-open off.
  - `AUTO_OPEN` (default `1`): `0` turns off automatic opening, including from the Overwolf app.
  - `AUTO_OPEN_WINDOW_SECONDS` (default `75`): how long betting stays open after an automatic open.
- **Overwolf, without building anything (recommended):** install the free, Overwolf-approved
  **HomeAssistant Game Events** app from the Overwolf store. It forwards Overwolf's live Valorant data to a
  webhook. Run `python ha_link.py` (or open `ha-webhook-url.txt`) and paste the address it
  prints into the app's *Webhook URL* field (throttle 500 is fine). The bot must listen on the home network
  (`DASHBOARD_HOST=0.0.0.0`; it refuses anything from outside the home network). The app only accepts
  `http://<name>.local` addresses, so it works from PCs on the same home network as the bot.
  Betting opens at agent select with every teammate named, and bets settle from the final scoreboard.
- **Overwolf app (our own, needs Overwolf approval):** see [overwolf-app/README.md](overwolf-app/README.md). It runs while you play and
  tells the bot about your game: it opens betting at agent select with both teams, posts the enemy
  recap once the match loads, and settles every bet from the final scoreboard without waiting for
  tracker.gg. Only one player per match needs it. Run `python make_overwolf_config.py` to point it at the bot.

### Overwolf connection

The bot listens on port `DASHBOARD_PORT` (default `8787`, this PC only unless `DASHBOARD_HOST` is set) for
game data from the Overwolf app, and nothing else: there's no web page or remote control. Calls need a
private key (`DASHBOARD_TOKEN`; if empty the bot generates one and keeps it in its database) that
`python make_overwolf_config.py` copies into the app. If you change the port or key, re-run that, and if the
port isn't 8787, list it in the `externally_connectable` section of `overwolf-app/manifest.json`. If the
port is busy the bot still starts, just without the Overwolf connection.

`simulator.html` is a standalone page you can open in a browser to try the betting and odds without Discord or the bot.

## Notes

- **Opponents before the game:** HenrikDev and tracker.gg (like every Riot-approved API) have no
  live-match endpoint, so they can't see who's in your lobby until the game ends. The Overwolf app can: for games
  where one player has it, opponents and their ranks are added automatically once the match loads
  (the odds are re-priced if nobody has bet yet). Without it, you can add opponents when opening
  betting for better win odds. Either way, the post-game scoreboard identifies
  and scouts all opponents automatically.
- HenrikDev usually lists a match within a minute or two of it ending. Its free key allows ~30 requests a
  minute, shared by everything the bot does.
- If HenrikDev is down, the bot tries tracker.gg (if `TRACKER_API_KEY` is set), and otherwise opens betting
  with average stats. If no data source can settle a game, it's cancelled and every bet refunded after
  `MATCH_TIMEOUT_HOURS` (default 3).
- If no game turns up within that time, the match is cancelled and every bet is refunded.
