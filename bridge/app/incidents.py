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
        db.commit()
        return db

    def note(self, kind: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO notes (at, kind, detail) VALUES (?,?,?)",
                        (time.time(), kind, detail))
        self.db.commit()

    @staticmethod
    def classify(name: str) -> str:
        if name in config.IGNORED_ENTITIES or name.startswith(config.IGNORED_PREFIXES):
            return "ignored"
        if name == config.CHARACTER_ENTITY:
            return "character"
        if name in config.DEFENSE_ENTITIES:
            return "defense"
        # Unknown names count as production: fail-alarming, not fail-silent
        # (matters when Space Age adds new entity types).
        return "production"

    async def run(self) -> None:
        while True:
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

    async def _losses(self, event: dict) -> None:
        surface, deltas = event["surface"], event["deltas"]
        buckets: dict[str, dict[str, int]] = {}
        for name, count in deltas.items():
            cls = self.classify(name)
            if cls == "ignored":
                continue
            if cls == "character":
                self.note("death", json.dumps({"surface": surface, "count": count}))
                await self.notify.put({"kind": "death", "surface": surface, "count": count})
                continue
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
