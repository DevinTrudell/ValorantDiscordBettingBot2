# Privacy policy

This Discord bot runs a play-money betting game on Valorant matches. Coins have no cash value: they can't be
bought, sold, exchanged or cashed out.

## What it stores

Data is kept **separately for each Discord server** the bot is in: coins, links and bets in one server never
affect another. The one exception is the **public leaderboard** (`/leaderboard scope:All servers`). It lists
the top balances of active players, from servers whose admins **opted in** with `/public-leaderboard
show:True`. It shows each player's Discord display name and coins. The server's name is shown only if its
admins also chose `show_server_name:True`. `/public-leaderboard show:False` takes a server off again.

- **Your Discord user ID**, with your coin balance, your bets and when you last used the bot.
- **The Riot ID you link** with `/link` (and a BattleTag, if linked).
- **Results of your games the bot watched**: win/loss, score, map and mode. They're used to price the odds.
- **Betting pages**: the players in a game, their public ranks and stats, and how the bets settled.

## What it reads

- **Your Discord game status**, only for people who have linked a Riot ID, and only the Valorant part. It
  opens betting when you start a game and settles it when the game ends.
- **Public Valorant data from HenrikDev** (a third-party Valorant API): ranks and match history for linked
  players and the other players in their games.
- **Optionally, the Overwolf app**, if you set it up with `/overwolf-link` (only in the owner's server): live
  details of your own games.

## What it does not do

- It doesn't read messages, and it doesn't store your Discord status history (except a short debugging log in
  the owner's own server, which rolls over at 5 MB).
- It doesn't sell or share your data with anyone. Lookups go to HenrikDev using your Riot ID only.

## Keeping and deleting data

- `/unlink` deletes your Riot ID and your game history in that server straight away.
- To delete everything (balance and bets too), ask the bot owner through the server where you use the bot.
- Database backups are kept for 14 days, then deleted.
- If the bot is removed from a server, open bets there are refunded. That server's data stays on file, so
  everything is restored if the bot is added back. The owner deletes it on request.

`/privacy` in Discord shows a short version of this page. See also the [Terms of Service](TERMS.md).

## Changes and contact

This policy may be updated; the latest version is always in this file. Questions or deletion requests: open
an issue at https://github.com/DevinTrudell/ValorantDiscordBettingBot2/issues, or ask the bot owner in a
server that uses the bot.
