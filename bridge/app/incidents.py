"""Tolerance model: turn loss events into waves (noise) and breaches (real).

- defense entities lost -> "wave"; alert only if big, always counted.
- anything else lost    -> "breach"; alert immediately, auto-pause if empty.
- character lost        -> player death, info note.
Open incidents close after QUIET_GAP_S without new losses. History in SQLite.
"""
import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field

from . import config

log = logging.getLogger("incidents")


@dataclass
class Incident:
    kind: str                 # "wave" | "breach"
    surface: str
    started_at: float
    last_loss_at: float
    entities: dict[str, int] = field(default_factory=dict)
    alerted: bool = False

    @property
    def total(self) -> int:
        return sum(self.entities.values())


class IncidentEngine:
    """Consumes poller events; produces notification events for the bot."""

    def __init__(self, events_in: asyncio.Queue, notify: asyncio.Queue, poller):
        self.events_in = events_in
        self.notify = notify
        self.poller = poller
        self.open: dict[tuple[str, str], Incident] = {}  # (kind, surface) -> incident
        self.db = self._init_db()
        # Tolerance rules: entity -> (max destroyed, rolling window seconds).
        # Losses within the budget are digest-only, not breaches.
        self.tolerances: dict[str, tuple[int, int]] = {
            name: (mc, ws) for name, mc, ws in
            self.db.execute("SELECT name, max_count, window_s FROM tolerances")
        }
        self._tol_hits: dict[str, list[tuple[float, int]]] = {}

    def _init_db(self) -> sqlite3.Connection:
        import os
        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        db = sqlite3.connect(config.DB_PATH)
        db.execute(
            "CREATE TABLE IF NOT EXISTS incidents ("
            "id INTEGER PRIMARY KEY, kind TEXT, surface TEXT, started_at REAL,"
            "ended_at REAL, entities TEXT, total INTEGER, auto_paused INTEGER)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS notes ("
            "id INTEGER PRIMARY KEY, at REAL, kind TEXT, detail TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS resource_peaks ("
            "surface TEXT, name TEXT, peak REAL, alerted INTEGER DEFAULT 0,"
            "PRIMARY KEY (surface, name))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS tolerances ("
            "name TEXT PRIMARY KEY, max_count INTEGER, window_s INTEGER)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS resource_history ("
            "at REAL, surface TEXT, name TEXT, amount REAL)"
        )
        db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        db.commit()
        return db

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.db.commit()

    def note(self, kind: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO notes (at, kind, detail) VALUES (?,?,?)",
                        (time.time(), kind, detail))
        self.db.commit()

    # Entity names carrying the 'player-creation' flag, refreshed hourly from
    # game prototypes. Anything else the enemy "kills" (their own units, DLC
    # fauna, scenery) can't be a player loss — future-proof vs prefix lists.
    _player_creation: set[str] = set()

    def classify(self, name: str) -> str:
        if name in config.IGNORED_ENTITIES or name.startswith(config.IGNORED_PREFIXES):
            return "ignored"
        if name == config.CHARACTER_ENTITY:
            return "character"
        if self._player_creation and name not in self._player_creation:
            return "ignored"
        if name in config.DEFENSE_ENTITIES:
            return "defense"
        # Unknown player-creation names count as production: fail-alarming,
        # not fail-silent (matters as Space Age adds new building types).
        return "production"

    async def run(self) -> None:
        last_proto_refresh = 0.0
        while True:
            if time.time() - last_proto_refresh > 3600:
                try:
                    names = await self.poller.entity_names()
                    if names:
                        self._player_creation = set(names)
                    last_proto_refresh = time.time()
                except Exception:
                    log.exception("prototype refresh failed")
                    last_proto_refresh = time.time() - 3300  # retry in ~5 min
            try:
                event = await asyncio.wait_for(self.events_in.get(), timeout=15)
            except asyncio.TimeoutError:
                event = None
            if event is not None:
                await self._handle(event)
            await self._close_quiet()

    async def _handle(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "losses":
            await self._losses(event)
        elif kind in ("join", "leave"):
            self.note(kind, event["player"])
            if config.ALERT_JOINS:
                await self.notify.put({"kind": kind, "player": event["player"]})
        elif kind in ("server_down", "server_up"):
            self.note(kind)
            await self.notify.put({"kind": kind})
        elif kind in ("rocket", "research_done", "evolution",
                      "ups_low", "ups_ok", "power_low", "power_ok"):
            self.note(kind, json.dumps({k: v for k, v in event.items() if k != "kind"}))
            await self.notify.put(event)
        elif kind == "resources":
            await self._resources(event["surfaces"])

    async def _losses(self, event: dict) -> None:
        surface, deltas = event["surface"], event["deltas"]
        buckets: dict[str, dict[str, int]] = {}
        tol_context: dict[str, dict] = {}
        for name, count in deltas.items():
            cls = self.classify(name)
            if cls == "ignored":
                continue
            if cls == "character":
                self.note("death", json.dumps({"surface": surface, "count": count}))
                await self.notify.put({"kind": "death", "surface": surface, "count": count})
                continue
            if cls == "production":
                ok, ctx = self._tolerated(name, count, event["at"])
                if ok:
                    self.note("tolerated", json.dumps(
                        {"surface": surface, "name": name, "count": count}))
                    continue
                if ctx is not None:
                    tol_context[name] = ctx
            buckets.setdefault("wave" if cls == "defense" else "breach", {})[name] = count

        for inc_kind, entities in buckets.items():
            key = (inc_kind, surface)
            inc = self.open.get(key)
            if inc is None:
                inc = self.open[key] = Incident(
                    kind=inc_kind, surface=surface,
                    started_at=event["at"], last_loss_at=event["at"])
            inc.last_loss_at = event["at"]
            for name, count in entities.items():
                inc.entities[name] = inc.entities.get(name, 0) + count

            if inc.kind == "breach" and not inc.alerted:
                inc.alerted = True
                auto_paused = False
                if config.AUTO_PAUSE_ON_BREACH and event["players_online"] == 0:
                    try:
                        await self.poller.set_paused(True)
                        auto_paused = True
                    except Exception:
                        log.exception("auto-pause failed")
                await self.notify.put({
                    "kind": "breach", "surface": surface,
                    "entities": dict(inc.entities), "auto_paused": auto_paused,
                    "tolerance_context": dict(tol_context),
                })
            elif inc.kind == "wave" and not inc.alerted and inc.total >= config.WAVE_ALERT_THRESHOLD:
                inc.alerted = True
                await self.notify.put({
                    "kind": "big_wave", "surface": surface, "entities": dict(inc.entities),
                })

    async def _close_quiet(self) -> None:
        now = time.time()
        for key, inc in list(self.open.items()):
            if now - inc.last_loss_at < config.QUIET_GAP_S:
                continue
            del self.open[key]
            self.db.execute(
                "INSERT INTO incidents (kind, surface, started_at, ended_at, entities, total, auto_paused)"
                " VALUES (?,?,?,?,?,?,0)",
                (inc.kind, inc.surface, inc.started_at, inc.last_loss_at,
                 json.dumps(inc.entities), inc.total))
            self.db.commit()
            if inc.alerted:  # summarize alerts we opened; quiet waves go digest-only
                await self.notify.put({
                    "kind": f"{inc.kind}_closed", "surface": inc.surface,
                    "entities": dict(inc.entities),
                    "duration_s": int(inc.last_loss_at - inc.started_at),
                })

    # --- tolerance rules -------------------------------------------------
    def _tolerated(self, name: str, count: int, at: float) -> tuple[bool, dict | None]:
        """(within_budget, context). Context describes the rolling window so a
        budget-crossing alert can tell the whole story, not just this delta."""
        rule = self.tolerances.get(name)
        if rule is None:
            return False, None
        max_count, window_s = rule
        hits = [(t, c) for t, c in self._tol_hits.get(name, []) if at - t < window_s]
        hits.append((at, count))
        self._tol_hits[name] = hits
        window_total = sum(c for _, c in hits)
        context = {"window_total": window_total, "budget": max_count,
                   "window_min": window_s // 60}
        return window_total <= max_count, context

    def set_tolerance(self, name: str, max_count: int, window_s: int) -> None:
        self.tolerances[name] = (max_count, window_s)
        self.db.execute(
            "INSERT INTO tolerances (name, max_count, window_s) VALUES (?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET max_count=excluded.max_count,"
            " window_s=excluded.window_s", (name, max_count, window_s))
        self.db.commit()

    def remove_tolerance(self, name: str) -> bool:
        existed = self.tolerances.pop(name, None) is not None
        self.db.execute("DELETE FROM tolerances WHERE name=?", (name,))
        self.db.commit()
        return existed

    def tolerated_since(self, since: float) -> dict[str, int]:
        out: dict[str, int] = {}
        for (detail,) in self.db.execute(
                "SELECT detail FROM notes WHERE kind='tolerated' AND at >= ?", (since,)):
            d = json.loads(detail)
            out[d["name"]] = out.get(d["name"], 0) + d["count"]
        return out

    # --- tapped resources ------------------------------------------------
    latest_resources: dict | None = None
    _prev_resources: dict | None = None

    async def _resources(self, surfaces: dict) -> None:
        self._prev_resources = self.latest_resources
        now = time.time()
        self.latest_resources = {"at": now, "surfaces": surfaces}
        for sname, res in surfaces.items():
            for name, t in res.items():
                self.db.execute(
                    "INSERT INTO resource_history (at, surface, name, amount)"
                    " VALUES (?,?,?,?)", (now, sname, name, t.get("amount", 0)))
        self.db.execute("DELETE FROM resource_history WHERE at < ?", (now - 7 * 86400,))
        for sname, res in surfaces.items():
            for name, t in res.items():
                amount = t.get("amount", 0)
                row = self.db.execute(
                    "SELECT peak, alerted FROM resource_peaks WHERE surface=? AND name=?",
                    (sname, name)).fetchone()
                peak, alerted = row if row else (0.0, 0)
                peak = max(peak, amount)
                pct = amount / peak if peak else 1.0
                if t.get("infinite"):
                    pass  # display-only: oil bottoms out at 20% yield by design
                elif (not alerted and peak >= config.RESOURCE_MIN_PEAK
                        and pct < config.RESOURCE_ALERT_PCT):
                    alerted = 1
                    self.note("resource_low", json.dumps(
                        {"surface": sname, "name": name, "pct": pct}))
                    await self.notify.put({
                        "kind": "resource_low", "surface": sname, "name": name,
                        "pct": pct, "amount": amount, "peak": peak})
                elif alerted and pct > config.RESOURCE_ALERT_PCT * 1.5:
                    alerted = 0  # a fresh patch was tapped; re-arm the alert
                self.db.execute(
                    "INSERT INTO resource_peaks (surface, name, peak, alerted)"
                    " VALUES (?,?,?,?) ON CONFLICT(surface, name)"
                    " DO UPDATE SET peak=excluded.peak, alerted=excluded.alerted",
                    (sname, name, peak, alerted))
        self.db.commit()

    def resource_detail(self, name: str) -> list[dict]:
        """Per-surface detail for one resource, with drain rate + ETA derived
        from the two most recent scans (needs ~10 min of history)."""
        out = []
        if not self.latest_resources:
            return out
        cur_at = self.latest_resources["at"]
        for sname, res in self.latest_resources["surfaces"].items():
            t = res.get(name)
            if not t:
                continue
            row = self.db.execute(
                "SELECT peak FROM resource_peaks WHERE surface=? AND name=?",
                (sname, name)).fetchone()
            peak = row[0] if row else t.get("amount", 0)
            drain_per_min = eta_min = None
            prev = self._prev_resources
            if prev and cur_at - prev["at"] > 60:
                pt = (prev["surfaces"].get(sname) or {}).get(name)
                if pt:
                    delta = pt.get("amount", 0) - t.get("amount", 0)
                    drain_per_min = delta / ((cur_at - prev["at"]) / 60)
                    if drain_per_min > 0:
                        eta_min = t.get("amount", 0) / drain_per_min
            # 24h decomposition from scan history: consecutive negative deltas
            # are mining drain; positive deltas are newly tapped patches. This
            # is what makes "why doesn't the total drop?" self-explanatory —
            # expansion masks depletion in the aggregate.
            mined_24h = tapped_24h = 0.0
            prev_amt = None
            for (amt,) in self.db.execute(
                    "SELECT amount FROM resource_history WHERE surface=? AND name=?"
                    " AND at >= ? ORDER BY at", (sname, name, cur_at - 86400)):
                if prev_amt is not None:
                    d = amt - prev_amt
                    if d < 0:
                        mined_24h += -d
                    else:
                        tapped_24h += d
                prev_amt = amt
            out.append({
                "surface": sname, "amount": t.get("amount", 0), "peak": peak,
                "tiles": t.get("tiles", 0), "infinite": bool(t.get("infinite")),
                "drain_per_min": drain_per_min, "eta_min": eta_min,
                "mined_24h": mined_24h, "tapped_24h": tapped_24h})
        return out

    def resource_report(self) -> list[dict]:
        if not self.latest_resources:
            return []
        out = []
        for sname, res in self.latest_resources["surfaces"].items():
            for name, t in res.items():
                row = self.db.execute(
                    "SELECT peak FROM resource_peaks WHERE surface=? AND name=?",
                    (sname, name)).fetchone()
                peak = row[0] if row else t.get("amount", 0)
                out.append({
                    "surface": sname, "name": name, "amount": t.get("amount", 0),
                    "peak": peak, "infinite": bool(t.get("infinite")),
                    "tiles": t.get("tiles", 0)})
        out.sort(key=lambda r: (r["surface"], r["amount"] / r["peak"] if r["peak"] else 1))
        return out

    # --- reporting -------------------------------------------------------
    def digest(self, since: float) -> dict:
        cur = self.db.execute(
            "SELECT kind, COUNT(*), COALESCE(SUM(total),0), COALESCE(GROUP_CONCAT(entities, '|'),'')"
            " FROM incidents WHERE ended_at >= ? GROUP BY kind", (since,))
        out: dict = {"wave": {"count": 0, "lost": 0}, "breach": {"count": 0, "lost": 0},
                     "deaths": 0, "entities": {}}
        for kind, count, lost, blobs in cur.fetchall():
            out[kind] = {"count": count, "lost": lost}
            for blob in blobs.split("|"):
                if blob:
                    for name, n in json.loads(blob).items():
                        out["entities"][name] = out["entities"].get(name, 0) + n
        (deaths,) = self.db.execute(
            "SELECT COUNT(*) FROM notes WHERE kind='death' AND at >= ?", (since,)).fetchone()
        out["deaths"] = deaths
        return out

    def recent_incidents(self, limit: int = 8) -> list[dict]:
        cur = self.db.execute(
            "SELECT kind, surface, started_at, ended_at, entities, total"
            " FROM incidents ORDER BY ended_at DESC LIMIT ?", (limit,))
        return [
            {"kind": k, "surface": s, "started_at": a, "ended_at": b,
             "entities": json.loads(e), "total": t}
            for k, s, a, b, e, t in cur.fetchall()
        ]

    def last_breach_at(self) -> float | None:
        row = self.db.execute(
            "SELECT MAX(ended_at) FROM incidents WHERE kind='breach'").fetchone()
        return row[0] if row and row[0] else None
