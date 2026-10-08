"""Live game data from the free "HomeAssistant Game Events" app in the Overwolf store (already approved by
Overwolf, so nothing has to be built or whitelisted).

That app forwards Overwolf's live game data as JSON to a webhook (the bot only uses Valorant's). It only accepts
http://<name>.local addresses, so it's pointed at http://<this-PC>.local:8787/api/ha/<key> (python ha_link.py
prints it). Every half-second it POSTs a batch like
    [{"type": "info",  "gameId": 21640, "data": {"match_info": {"roster_3": {...}}}},
     {"type": "event", "gameId": 21640, "data": {"events": [{"name": "match_start", "data": ""}]}}]
(it turns JSON-in-a-string values into real JSON, so numbers may arrive as numbers).

This module keeps the game state per sender (each friend's PC) and turns it into the same calls the bot's own
Overwolf app would have made: "pregame" / "roster" / "match_start" / "match_end".
"""

from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger("valbet.ha")

VALORANT = 21640  # Overwolf's game ID
AGENT_SELECT = "CharacterSelectPersistentLevel"
ORDER = {"roster": 0, "pregame": 1, "match_start": 2}


def _parse(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


class _Valorant:
    def __init__(self, emit):
        self.emit = emit
        self.owner = None
        self.me = None
        self.scene = None
        self.in_match = False
        self.pending = None
        self.timer: asyncio.Task | None = None
        self.reset()

    def reset(self, keep_mode: bool = False):
        mode = getattr(self, "mode", None) if keep_mode else None
        self.mode, self.map, self.match_id, self.outcome, self.score = mode, None, None, None, None
        self.roster, self.scoreboard = {}, {}

    def payload(self, event: str) -> dict:
        return {"event": event, "owner": self.owner, "me": self.me, "mode": self.mode, "map": self.map, "match_id": self.match_id,
                "outcome": self.outcome, "score": self.score,
                "roster": [r for r in self.roster.values() if isinstance(r, dict)],
                "scoreboard": [s for s in self.scoreboard.values() if isinstance(s, dict)]}

    def send_soon(self, event: str, delay: float = 3.0):
        """Roster entries arrive one by one: wait until they stop changing, then send the most important event."""
        if not self.pending or ORDER[event] >= ORDER[self.pending]:
            self.pending = event
        if self.timer:
            self.timer.cancel()

        async def later():
            await asyncio.sleep(delay)
            # Only the wait can be cut short by newer data: once sending, never cancel (that would kill
            # opening betting halfway through).
            self.timer = None
            ev, self.pending = self.pending, None
            await self.emit(self.payload(ev))
        self.timer = asyncio.create_task(later())

    def on_info(self, info: dict):
        me = info.get("me") or {}
        if isinstance(me, dict) and me.get("player_name"):
            self.me = str(me["player_name"])
        scene = (info.get("game_info") or {}).get("scene")
        if scene:
            if scene == AGENT_SELECT and self.scene != AGENT_SELECT:  # a new game: start clean
                self.reset(keep_mode=True)
                self.in_match = True
                self.send_soon("pregame")
            if scene == "MainMenu":
                self.in_match = False
            self.scene = scene
        for key, value in (info.get("match_info") or {}).items():
            if key.startswith("roster_"):
                parsed = _parse(value)
                if parsed != self.roster.get(key):  # what the game reports, to check team detection against
                    log.info("Roster %s: %s", key, {k: parsed.get(k) for k in ("name", "teammate", "local", "character")}
                             if isinstance(parsed, dict) else parsed)
                self.roster[key] = parsed
                if self.in_match:
                    self.send_soon("roster")
            elif key.startswith("scoreboard_"):
                self.scoreboard[key] = _parse(value)
            elif key == "game_mode":
                if value != self.mode:
                    log.info("Game mode: %s", value)
                self.mode = _parse(value)
            elif key == "map":
                self.map = value
            elif key == "match_id":
                self.match_id = value
            elif key == "match_outcome":
                self.outcome = value
            elif key == "score":
                self.score = _parse(value)

    def on_event(self, name: str):
        if name == "match_start":
            self.in_match = True
            self.outcome, self.score, self.scoreboard = None, None, {}  # no stale result from the last game
            self.send_soon("match_start", 5)
        elif name == "match_end":
            if self.timer:
                self.timer.cancel()
            self.pending = None

            async def later():
                await asyncio.sleep(4)  # let the final scoreboard arrive
                self.timer = None
                await self.emit(self.payload("match_end"))
                self.in_match = False
            self.timer = asyncio.create_task(later())


class Bridge:
    """One per bot. feed() takes each POSTed batch; `handler` is the bot's overwolf_event."""

    def __init__(self, handler):
        self.handler = handler
        self.state: dict[tuple[str, int], object] = {}

    async def _emit(self, payload: dict):
        try:
            result = await self.handler(payload)
            log.info("Overwolf (HA app) %s -> %s", payload.get("event"), result)
        except Exception:
            log.exception("Handling Overwolf event %s failed", payload.get("event"))

    def feed(self, sender: str, batch, owner: int | None = None) -> int:
        if not isinstance(batch, list):
            batch = [batch]
        used = 0
        for item in batch:
            if not isinstance(item, dict):
                continue
            game = item.get("gameId")
            try:
                game = int(game)
            except (TypeError, ValueError):
                continue
            if game != VALORANT:
                continue
            key = (sender, game)
            if key not in self.state:
                self.state[key] = _Valorant(self._emit)
            st = self.state[key]
            st.owner = owner
            data = item.get("data") or {}
            if item.get("type") == "info" and isinstance(data, dict):
                st.on_info(data)
                used += 1
            elif item.get("type") == "event" and isinstance(data, dict):
                for ev in data.get("events") or []:
                    if isinstance(ev, dict) and ev.get("name"):
                        st.on_event(str(ev["name"]))
                        used += 1
        return used
