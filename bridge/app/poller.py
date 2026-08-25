"""10s RCON poll loop: server vitals + enemy kill-count deltas.

Emits plain-dict events to an asyncio queue; incidents.py turns them into
waves/breaches/notifications and discord_bot.py renders them. Per-surface
from day 1 so the Space Age flip needs no code changes here.
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
# low_power samples a fixed set of machines (find_entities_filtered order is
# deterministic) as a cheap brownout proxy.
POLL_LUA = (
    "/sc local e=game.forces['enemy'] local pf=game.forces['player'] "
    "local out={tick=game.tick,paused=game.tick_paused,rockets=pf.rockets_launched,"
    "players={},surfaces={},research=nil,research_progress=0,researched=0} "
    "for _,p in pairs(game.connected_players) do table.insert(out.players,p.name) end "
    "if pf.current_research then out.research=pf.current_research.name "
    "out.research_progress=pf.research_progress end "
    "local rc=0 for _,t in pairs(pf.technologies) do if t.researched then rc=rc+1 end end "
    "out.researched=rc "
    "for _,s in pairs(game.surfaces) do "
    "local k=e.get_kill_count_statistics(s) local low=0 "
    "for _,m in pairs(s.find_entities_filtered{force='player',"
    "type={'assembling-machine','furnace','inserter'},limit=60}) do "
    "if m.status==defines.entity_status.low_power then low=low+1 end end "
    "out.surfaces[s.name]={evolution=e.get_evolution_factor(s),kills=k.input_counts,"
    "low_power=low} end rcon.print(helpers.table_to_json(out))"
)

# On-demand production queries (used by /production).
PROD_TOP_LUA = (
    "/sc local pf=game.forces['player'] local out={} "
    "for _,s in pairs(game.surfaces) do "
    "local st=pf.get_item_production_statistics(s) "
    "for name,_ in pairs(st.input_counts) do "
    "local r=st.get_flow_count{name=name,category='input',"
    "precision_index=defines.flow_precision_index.one_minute} "
    "if r>0 then out[name]=(out[name] or 0)+r end end end "
    "rcon.print(helpers.table_to_json(out))"
)


def prod_item_lua(item: str) -> str:
    item = item.replace("'", "")
    return (
        "/sc local pf=game.forces['player'] "
        "local out={m1={0,0},m10={0,0},h1={0,0}} "
        "local px=defines.flow_precision_index "
        "for _,s in pairs(game.surfaces) do "
        "local st=pf.get_item_production_statistics(s) "
        "for key,pi in pairs({m1=px.one_minute,m10=px.ten_minutes,h1=px.one_hour}) do "
        f"out[key][1]=out[key][1]+st.get_flow_count{{name='{item}',category='input',precision_index=pi,count=true}} "
        f"out[key][2]=out[key][2]+st.get_flow_count{{name='{item}',category='output',precision_index=pi,count=true}} "
        "end end rcon.print(helpers.table_to_json(out))"
    )


# Slow scan: ore remaining under/near every mining drill, deduped by tile.
# Runs every RESOURCE_POLL_S (heavier than the vitals poll — full drill sweep).
RESOURCES_LUA = (
    "/sc local out={} for _,s in pairs(game.surfaces) do "
    "local acc={} local seen={} "
    "for _,d in pairs(s.find_entities_filtered{type='mining-drill',force='player'}) do "
    "local r=(d.prototype.mining_drill_radius or 0)+0.5 "
    "local area={{d.position.x-r,d.position.y-r},{d.position.x+r,d.position.y+r}} "
    "for _,res in pairs(s.find_entities_filtered{area=area,type='resource'}) do "
    "local pk=res.position.x..'_'..res.position.y "
    "if not seen[pk] then seen[pk]=true "
    "local t=acc[res.name] "
    "if not t then t={amount=0,tiles=0,infinite=res.prototype.infinite_resource or false} "
    "acc[res.name]=t end "
    "t.amount=t.amount+res.amount t.tiles=t.tiles+1 end end end "
    "out[s.name]=acc end rcon.print(helpers.table_to_json(out))"
)

# /military. Notable: "fired" can NOT come from consumption stats — artillery
# wagons fire off the books and breach-destroyed shells vanish uncounted — so
# shells expended = crafted minus a live world scan of shells in inventories.
MILITARY_LUA = (
    "/sc local pf=game.forces['player'] local ef=game.forces['enemy'] "
    "local px=defines.flow_precision_index "
    "local AMMO={'firearm-magazine','piercing-rounds-magazine',"
    "'uranium-rounds-magazine','rocket','explosive-rocket','grenade',"
    "'cluster-grenade','cannon-shell','explosive-cannon-shell',"
    "'flamethrower-ammo','artillery-shell'} "
    "local out={ammo={},kills={},kills_1h=0,losses={},shell_stock=0} "
    "for _,s in pairs(game.surfaces) do "
    "local ip=pf.get_item_production_statistics(s) "
    "for _,a in pairs(AMMO) do "
    "local t=out.ammo[a] or {made=0,used=0,made_1h=0,used_1h=0} "
    "t.made=t.made+(ip.input_counts[a] or 0) "
    "t.used=t.used+(ip.output_counts[a] or 0) "
    "t.made_1h=t.made_1h+ip.get_flow_count{name=a,category='input',precision_index=px.one_hour,count=true} "
    "t.used_1h=t.used_1h+ip.get_flow_count{name=a,category='output',precision_index=px.one_hour,count=true} "
    "out.ammo[a]=t end "
    "local k=pf.get_kill_count_statistics(s) "
    "for n,c in pairs(k.input_counts) do out.kills[n]=(out.kills[n] or 0)+c "
    "out.kills_1h=out.kills_1h+k.get_flow_count{name=n,category='input',precision_index=px.one_hour,count=true} end "
    "local el=ef.get_kill_count_statistics(s) "
    "for n,c in pairs(el.input_counts) do out.losses[n]=(out.losses[n] or 0)+c end "
    "for _,e in pairs(s.find_entities_filtered{type={'artillery-turret',"
    "'artillery-wagon','container','logistic-container','cargo-wagon','car','character'}}) do "
    "for i=1,7 do local inv=e.get_inventory(i) if inv then "
    "local ok,n=pcall(function() return inv.get_item_count('artillery-shell') end) "
    "if ok then out.shell_stock=out.shell_stock+n end end end end "
    "end rcon.print(helpers.table_to_json(out))"
)

# Prototype name lists for Discord autocomplete (cached, hourly refresh).
ENTITY_NAMES_LUA = (
    "/sc local out={} for n,p in pairs(prototypes.entity) do "
    "if p.has_flag('player-creation') then out[#out+1]=n end end "
    "rcon.print(helpers.table_to_json(out))"
)
ITEM_NAMES_LUA = (
    "/sc local out={} for n,_ in pairs(prototypes.item) do out[#out+1]=n end "
    "rcon.print(helpers.table_to_json(out))"
)

EVOLUTION_THRESHOLDS = (0.25, 0.5, 0.75, 0.9)


class Poller:
    def __init__(self, events: asyncio.Queue):
        self.events = events
        self.rcon = RconClient(config.RCON_HOST, config.RCON_PORT, config.RCON_PASSWORD)
        # One socket, several users (vitals loop, resource loop, bot commands):
        # serialize access or the framing interleaves.
        self._rcon_lock = asyncio.Lock()
        self.up: bool | None = None
        self.snapshot: dict | None = None
        self._fails = 0
        self._prev_kills: dict[str, dict[str, int]] | None = None
        self._prev_players: set[str] | None = None
        self._prev_tick: int | None = None
        self._prev_wall: float | None = None
        self._prev_rockets: int | None = None
        self._prev_researched: int | None = None
        self._prev_research_name: str | None = None
        self._prev_evolution: dict[str, float] = {}
        self._low_ups_polls = 0
        self._ups_alerted = False
        self._low_power_polls: dict[str, int] = {}
        self._power_alerted: set[str] = set()
        self.ups: float | None = None

    last_loop_at: float = 0.0

    async def run(self) -> None:
        while True:
            self.last_loop_at = time.time()
            try:
                raw = await self.cmd(POLL_LUA)
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
        players = set(data.get("players") or [])
        surfaces = data.get("surfaces") or {}
        if isinstance(surfaces, list):  # Lua {} serializes as []
            surfaces = {}
        kills: dict[str, dict[str, int]] = {}
        for sname, sdata in surfaces.items():
            k = sdata.get("kills")
            kills[sname] = k if isinstance(k, dict) else {}

        idle = config.GAME_AUTO_PAUSE and not players and not data["paused"]
        self._track_ups(data, now, idle)
        self._track_kills(kills, players, now)
        self._track_players(players)
        self._track_rockets(data)
        self._track_research(data)
        self._track_evolution(surfaces)
        self._track_power(surfaces, data["paused"] or idle)

        self.snapshot = {
            "at": now, "tick": data["tick"], "paused": data["paused"], "idle": idle,
            "players": sorted(players), "rockets": data.get("rockets", 0),
            "evolution": {s: d.get("evolution", 0.0) for s, d in surfaces.items()},
            "research": data.get("research"),
            "research_progress": data.get("research_progress", 0),
            "researched": data.get("researched", 0),
            "low_power": {s: d.get("low_power", 0) for s, d in surfaces.items()},
            "ups": self.ups,
        }

    def _track_ups(self, data: dict, now: float, idle: bool) -> None:
        effectively_paused = data["paused"] or idle
        if (self._prev_tick is not None and self._prev_wall is not None
                and not effectively_paused):
            dt = now - self._prev_wall
            if dt > 0:
                self.ups = max(0.0, (data["tick"] - self._prev_tick) / dt)
        elif effectively_paused:
            self.ups = None
        self._prev_tick, self._prev_wall = data["tick"], now

        if self.ups is None:
            self._low_ups_polls = 0
            return
        if self.ups < config.UPS_ALERT_BELOW:
            self._low_ups_polls += 1
            if self._low_ups_polls == config.UPS_ALERT_POLLS and not self._ups_alerted:
                self._ups_alerted = True
                self.events.put_nowait({"kind": "ups_low", "ups": self.ups})
        else:
            self._low_ups_polls = 0
            if self._ups_alerted and self.ups >= config.UPS_ALERT_BELOW + 3:
                self._ups_alerted = False
                self.events.put_nowait({"kind": "ups_ok", "ups": self.ups})

    def _track_kills(self, kills, players, now) -> None:
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

    def _track_players(self, players: set[str]) -> None:
        if self._prev_players is not None:
            for name in players - self._prev_players:
                self.events.put_nowait({"kind": "join", "player": name})
            for name in self._prev_players - players:
                self.events.put_nowait({"kind": "leave", "player": name})
        self._prev_players = players

    def _track_rockets(self, data: dict) -> None:
        total = data.get("rockets", 0)
        if self._prev_rockets is not None and total > self._prev_rockets:
            self.events.put_nowait({
                "kind": "rocket", "total": total, "delta": total - self._prev_rockets})
        self._prev_rockets = total

    def _track_research(self, data: dict) -> None:
        researched = data.get("researched", 0)
        if (self._prev_researched is not None and researched > self._prev_researched
                and self._prev_research_name):
            self.events.put_nowait({"kind": "research_done", "name": self._prev_research_name})
        self._prev_researched = researched
        self._prev_research_name = data.get("research") or self._prev_research_name

    def _track_evolution(self, surfaces: dict) -> None:
        for sname, sdata in surfaces.items():
            cur = sdata.get("evolution", 0.0)
            prev = self._prev_evolution.get(sname)
            if prev is not None:
                for t in EVOLUTION_THRESHOLDS:
                    if prev < t <= cur:
                        self.events.put_nowait({
                            "kind": "evolution", "surface": sname, "threshold": t, "value": cur})
            self._prev_evolution[sname] = cur

    def _track_power(self, surfaces: dict, paused: bool) -> None:
        if paused:
            return
        for sname, sdata in surfaces.items():
            low = sdata.get("low_power", 0)
            if low > 0:
                n = self._low_power_polls.get(sname, 0) + 1
                self._low_power_polls[sname] = n
                if n == config.POWER_ALERT_POLLS and sname not in self._power_alerted:
                    self._power_alerted.add(sname)
                    self.events.put_nowait({"kind": "power_low", "surface": sname, "count": low})
            else:
                self._low_power_polls[sname] = 0
                if sname in self._power_alerted:
                    self._power_alerted.discard(sname)
                    self.events.put_nowait({"kind": "power_ok", "surface": sname})

    async def resource_loop(self) -> None:
        """Slow loop: tapped-resource totals -> one 'resources' event per scan."""
        await asyncio.sleep(20)  # let the vitals poll establish the connection
        while True:
            try:
                raw = await self.cmd(RESOURCES_LUA)
                data = json.loads(raw)
                if isinstance(data, dict):
                    surfaces = {s: (v if isinstance(v, dict) else {})
                                for s, v in data.items()}
                    await self.events.put({"kind": "resources", "surfaces": surfaces})
            except (OSError, RconError, json.JSONDecodeError) as exc:
                log.warning("resource scan failed: %s", exc)
            await asyncio.sleep(config.RESOURCE_POLL_S)

    # --- control / query actions (used by the bot) -----------------------
    async def cmd(self, command: str) -> str:
        async with self._rcon_lock:
            return await asyncio.to_thread(self.rcon.command, command)

    async def set_paused(self, paused: bool) -> None:
        await self.cmd(f"/sc game.tick_paused={'true' if paused else 'false'}")

    async def save(self) -> str:
        return await self.cmd("/server-save")

    _name_cache: dict[str, tuple[float, list[str]]] = {}

    async def _names(self, key: str, lua: str) -> list[str]:
        cached = self._name_cache.get(key)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        try:
            names = sorted(json.loads(await self.cmd(lua)))
        except (OSError, RconError, json.JSONDecodeError):
            return cached[1] if cached else []
        self._name_cache[key] = (time.time(), names)
        return names

    async def entity_names(self) -> list[str]:
        return await self._names("entity", ENTITY_NAMES_LUA)

    async def item_names(self) -> list[str]:
        return await self._names("item", ITEM_NAMES_LUA)

    async def military(self) -> dict:
        return json.loads(await self.cmd(MILITARY_LUA))

    async def production_top(self) -> dict[str, float]:
        return json.loads(await self.cmd(PROD_TOP_LUA))

    async def production_item(self, item: str) -> dict:
        return json.loads(await self.cmd(prod_item_lua(item)))
