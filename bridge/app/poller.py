"""10s RCON poll loop: server vitals + enemy kill-count deltas.

Emits plain-dict events to an asyncio queue; incidents.py turns them into
waves/breaches and discord_bot.py renders them. Per-surface from day 1 so the
Space Age flip needs no code changes here.
"""
import asyncio
import json
import logging
import time

from . import config
from .rcon import RconClient, RconError

log = logging.getLogger("poller")

# One silent-command, one JSON blob back. NB: /sc marks the save as
# command-used (achievements off) — documented trade-off.
POLL_LUA = (
    "/sc local e=game.forces['enemy'] local pf=game.forces['player'] "
    "local out={tick=game.tick,paused=game.tick_paused,rockets=pf.rockets_launched,"
    "players={},surfaces={}} "
    "for _,p in pairs(game.connected_players) do table.insert(out.players,p.name) end "
    "for _,s in pairs(game.surfaces) do "
    "local k=e.get_kill_count_statistics(s) "
    "out.surfaces[s.name]={evolution=e.get_evolution_factor(s),kills=k.input_counts} "
    "end rcon.print(helpers.table_to_json(out))"
)


class Poller:
    def __init__(self, events: asyncio.Queue):
        self.events = events
        self.rcon = RconClient(config.RCON_HOST, config.RCON_PORT, config.RCON_PASSWORD)
        self.up: bool | None = None  # None until first result
        self.snapshot: dict | None = None  # latest good poll
        self._fails = 0
        self._prev_kills: dict[str, dict[str, int]] | None = None
        self._prev_players: set[str] | None = None
        self._prev_tick: int | None = None
        self._prev_wall: float | None = None
        self.ups: float | None = None

    async def run(self) -> None:
        while True:
            try:
                raw = await asyncio.to_thread(self.rcon.command, POLL_LUA)
                self._handle(json.loads(raw))
                self._fails = 0
                if self.up is False:
                    await self.events.put({"kind": "server_up"})
                self.up = True
            except (OSError, RconError, json.JSONDecodeError) as exc:
                self._fails += 1
                log.warning("poll failed (%d): %s", self._fails, exc)
                if self._fails == config.DOWN_AFTER_FAILURES and self.up is not False:
                    self.up = False
                    self.ups = None
                    await self.events.put({"kind": "server_down"})
            await asyncio.sleep(config.POLL_INTERVAL_S)

    def _handle(self, data: dict) -> None:
        now = time.time()
        # Lua {} serializes as [] — normalize the empties.
        players = set(data.get("players") or [])
        surfaces = data.get("surfaces") or {}
        if isinstance(surfaces, list):
            surfaces = {}
        kills: dict[str, dict[str, int]] = {}
        for sname, sdata in surfaces.items():
            k = sdata.get("kills")
            kills[sname] = k if isinstance(k, dict) else {}

        # UPS from tick delta (only meaningful while unpaused).
        if self._prev_tick is not None and self._prev_wall is not None and not data["paused"]:
            dt = now - self._prev_wall
            if dt > 0:
                self.ups = max(0.0, (data["tick"] - self._prev_tick) / dt)
        elif data["paused"]:
            self.ups = None
        self._prev_tick, self._prev_wall = data["tick"], now

        # Kill-count deltas (first poll after (re)start is baseline only).
        if self._prev_kills is not None:
            for sname, counts in kills.items():
                prev = self._prev_kills.get(sname, {})
                deltas = {n: c - prev.get(n, 0) for n, c in counts.items() if c > prev.get(n, 0)}
                if deltas:
                    self.events.put_nowait({
                        "kind": "losses", "surface": sname, "deltas": deltas,
                        "players_online": len(players), "at": now,
                    })
        self._prev_kills = kills

        if self._prev_players is not None:
            for name in players - self._prev_players:
                self.events.put_nowait({"kind": "join", "player": name})
            for name in self._prev_players - players:
                self.events.put_nowait({"kind": "leave", "player": name})
        self._prev_players = players

        self.snapshot = {
            "at": now, "tick": data["tick"], "paused": data["paused"],
            "players": sorted(players), "rockets": data.get("rockets", 0),
            "evolution": {s: d.get("evolution", 0.0) for s, d in surfaces.items()},
            "ups": self.ups,
        }

    # --- control actions (used by the bot) -------------------------------
    async def cmd(self, command: str) -> str:
        return await asyncio.to_thread(self.rcon.command, command)

    async def set_paused(self, paused: bool) -> None:
        await self.cmd(f"/sc game.tick_paused={'true' if paused else 'false'}")

    async def save(self) -> str:
        return await self.cmd("/server-save")
