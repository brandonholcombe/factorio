"""Base-expansion timelapse: periodic entity snapshots -> map-view-style frames.

Every SNAPSHOT_INTERVAL_S the collector has the game dump every player-force
entity (name,x,y) to script-output via helpers.write_file (no RCON size
limits), then stores it gzipped on the backups volume. Frames are rendered at
view time (PIL) so a growing base can be drawn with consistent bounds across
the whole timelapse.
"""
import asyncio
import glob
import gzip
import io
import logging
import os
import time

from PIL import Image, ImageDraw

from . import config

log = logging.getLogger("timelapse")

SNAPSHOT_LUA = (
    "/sc for _,s in pairs(game.surfaces) do local t={} local i=0 "
    "for _,e in pairs(s.find_entities_filtered{force='player'}) do i=i+1 "
    "t[i]=e.name..','..string.format('%.1f,%.1f',e.position.x,e.position.y) end "
    "helpers.write_file('bridge-snap-'..s.name..'.csv',table.concat(t,'\\n'),false) "
    "end rcon.print('snapped')"
)

# Map-view-ish palette by name keyword (first match wins).
PALETTE = [
    (("wall", "gate"), (222, 222, 222)),
    (("turret",), (205, 65, 65)),
    (("belt", "splitter", "loader"), (232, 190, 60)),
    (("pipe", "pump", "storage-tank"), (80, 170, 170)),
    (("rail", "train-stop", "signal"), (125, 125, 138)),
    (("substation", "pole"), (245, 245, 205)),
    (("roboport",), (80, 200, 220)),
    (("chest", "container", "warehouse"), (190, 150, 90)),
    (("assembling",), (95, 125, 205)),
    (("furnace",), (232, 122, 52)),
    (("drill", "pumpjack"), (165, 95, 205)),
    (("lab",), (232, 122, 205)),
    (("solar",), (52, 62, 95)),
    (("accumulator",), (142, 142, 152)),
    (("reactor", "turbine", "boiler", "engine", "heat"), (92, 202, 122)),
    (("inserter",), (182, 142, 42)),
    (("rocket-silo",), (240, 240, 120)),
]
DEFAULT_COLOR = (150, 150, 150)
BACKGROUND = (24, 24, 30)

_color_cache: dict[str, tuple] = {}


def color_for(name: str) -> tuple:
    c = _color_cache.get(name)
    if c is None:
        c = DEFAULT_COLOR
        for keys, col in PALETTE:
            if any(k in name for k in keys):
                c = col
                break
        _color_cache[name] = c
    return c


def frames_dir() -> str:
    d = os.path.join(config.BACKUPS_DIR, "timelapse")
    os.makedirs(d, exist_ok=True)
    return d


def list_snapshots(surface: str) -> list[str]:
    """Snapshot archives for a surface, oldest first."""
    return sorted(glob.glob(os.path.join(frames_dir(), f"*-{surface}.csv.gz")))


def load_points(path: str) -> list[tuple[str, float, float]]:
    out = []
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").rsplit(",", 2)
            if len(parts) == 3:
                try:
                    out.append((parts[0], float(parts[1]), float(parts[2])))
                except ValueError:
                    continue
    return out


def bounds_of(point_sets: list[list[tuple[str, float, float]]],
              pad: float = 16.0) -> tuple[float, float, float, float]:
    xs = [p[1] for pts in point_sets for p in pts]
    ys = [p[2] for pts in point_sets for p in pts]
    if not xs:
        return (-100, -100, 100, 100)
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def render(points: list[tuple[str, float, float]],
           bounds: tuple[float, float, float, float],
           max_px: int, label: str | None = None) -> Image.Image:
    x0, y0, x1, y1 = bounds
    w, h = max(x1 - x0, 1), max(y1 - y0, 1)
    scale = min(max_px / w, max_px / h, 3.0)
    img = Image.new("RGB", (max(int(w * scale), 16), max(int(h * scale), 16)), BACKGROUND)
    draw = ImageDraw.Draw(img)
    dot = max(1, round(scale))
    for name, x, y in points:
        px, py = (x - x0) * scale, (y - y0) * scale
        draw.rectangle((px, py, px + dot, py + dot), fill=color_for(name))
    if label:
        draw.text((8, 8), label, fill=(235, 235, 235))
    return img


class SnapshotCollector:
    def __init__(self, poller):
        self.poller = poller

    def latest_age_s(self) -> float:
        snaps = sorted(glob.glob(os.path.join(frames_dir(), "*.csv.gz")))
        if not snaps:
            return float("inf")
        return time.time() - os.path.getmtime(snaps[-1])

    async def snapshot_once(self) -> list[str]:
        """Trigger a dump and archive one gzipped snapshot per surface."""
        await self.poller.cmd(SNAPSHOT_LUA)
        await asyncio.sleep(3)  # let the game flush script-output
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        stored = []
        pattern = os.path.join(config.DATA_DIR, "script-output", "bridge-snap-*.csv")
        for path in glob.glob(pattern):
            surface = os.path.basename(path)[len("bridge-snap-"):-len(".csv")]
            dest = os.path.join(frames_dir(), f"{ts}-{surface}.csv.gz")
            with open(path, "rb") as src, gzip.open(dest, "wb") as dst:
                dst.write(src.read())
            stored.append(dest)
        log.info("snapshot stored: %s", [os.path.basename(s) for s in stored])
        return stored

    async def run(self) -> None:
        await asyncio.sleep(30)
        while True:
            # Snapshot on schedule, resuming sensibly after restarts.
            if self.latest_age_s() >= config.SNAPSHOT_INTERVAL_S:
                try:
                    await self.snapshot_once()
                except Exception:
                    log.exception("snapshot failed")
            await asyncio.sleep(300)


def build_map_png(surface: str, max_px: int) -> io.BytesIO | None:
    snaps = list_snapshots(surface)
    if not snaps:
        return None
    points = load_points(snaps[-1])
    img = render(points, bounds_of([points]), max_px)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def build_timelapse_gif(surface: str, max_frames: int, max_px: int,
                        frame_ms: int = 350) -> tuple[io.BytesIO, int] | None:
    snaps = list_snapshots(surface)
    if len(snaps) < 2:
        return None
    stride = max(1, -(-len(snaps) // max_frames))
    picked = snaps[::stride]
    if picked[-1] != snaps[-1]:
        picked.append(snaps[-1])
    # Two passes so we never hold every snapshot's points at once (a late-game
    # base times 100 frames would be gigabytes): pass 1 finds shared bounds
    # (the base visibly grows in frame), pass 2 renders one frame at a time.
    common = None
    for path in picked:
        b = bounds_of([load_points(path)])
        common = b if common is None else (
            min(common[0], b[0]), min(common[1], b[1]),
            max(common[2], b[2]), max(common[3], b[3]))
    frames = []
    for path in picked:
        stamp = os.path.basename(path).split("-")[0]
        label = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"
        img = render(load_points(path), common, max_px, label=label)
        frames.append(img.convert("P", palette=Image.ADAPTIVE, colors=64))
    frames.append(frames[-1])  # hold the final state a beat longer
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   duration=frame_ms, loop=0, optimize=True)
    buf.seek(0)
    return buf, len(picked)
