"""All tunables come from env (ConfigMap/Secret in k8s)."""
import os


def _set(name: str, default: str) -> set[str]:
    return {s.strip() for s in os.environ.get(name, default).split(",") if s.strip()}


RCON_HOST = os.environ.get("RCON_HOST", "factorio-rcon")
RCON_PORT = int(os.environ.get("RCON_PORT", "27015"))
RCON_PASSWORD = os.environ.get("RCONPW", "")

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))

POLL_INTERVAL_S = float(os.environ.get("POLL_INTERVAL_S", "10"))
DOWN_AFTER_FAILURES = int(os.environ.get("DOWN_AFTER_FAILURES", "6"))

# Entity names whose destruction is "normal combat" (waves), not a breach.
DEFENSE_ENTITIES = _set(
    "DEFENSE_ENTITIES",
    "stone-wall,gate,gun-turret,laser-turret,flamethrower-turret,"
    "artillery-turret,radar,land-mine",
)
# Names ignored entirely (enemy-force kills of things nobody mourns).
IGNORED_ENTITIES = _set("IGNORED_ENTITIES", "cliff,fish")
# Prefix matches for the same (real names are tree-04, rock-huge, small-biter,
# biter-spawner, medium-worm-turret, …). Covers enemy friendly-fire on their
# own units/nests and destructible scenery.
IGNORED_PREFIXES = tuple(_set(
    "IGNORED_PREFIXES",
    "tree,dead-,dry-,doomed-,rock-,sand-rock,huge-rock,big-rock,"
    "small-biter,medium-biter,big-biter,behemoth-biter,"
    "small-spitter,medium-spitter,big-spitter,behemoth-spitter,"
    "biter-spawner,spitter-spawner,small-worm,medium-worm,big-worm,behemoth-worm",
))
CHARACTER_ENTITY = "character"

# A wave/incident closes after this many seconds without new losses.
QUIET_GAP_S = int(os.environ.get("QUIET_GAP_S", "120"))
# Defense losses in one wave that merit their own alert (vs digest-only).
WAVE_ALERT_THRESHOLD = int(os.environ.get("WAVE_ALERT_THRESHOLD", "25"))
# Auto-pause on breach only when nobody is online.
AUTO_PAUSE_ON_BREACH = os.environ.get("AUTO_PAUSE_ON_BREACH", "true").lower() == "true"

ALERT_JOINS = os.environ.get("ALERT_JOINS", "true").lower() == "true"

# Health alerts: sustained-condition thresholds (polls are POLL_INTERVAL_S apart).
UPS_ALERT_BELOW = float(os.environ.get("UPS_ALERT_BELOW", "55"))
UPS_ALERT_POLLS = int(os.environ.get("UPS_ALERT_POLLS", "6"))       # ~1 min
POWER_ALERT_POLLS = int(os.environ.get("POWER_ALERT_POLLS", "3"))   # ~30 s
DIGEST_HOUR_UTC = int(os.environ.get("DIGEST_HOUR_UTC", "15"))  # 8am Pacific

DATA_DIR = os.environ.get("DATA_DIR", "/data")          # factorio-data PVC
BACKUPS_DIR = os.environ.get("BACKUPS_DIR", "/backups")  # factorio-backups PVC
SAVES_DIR = os.path.join(DATA_DIR, "saves")
DB_PATH = os.path.join(DATA_DIR, "bridge", "bridge.db")
SAVE_NAME = os.environ.get("SAVE_NAME", "kodloki")

K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "factorio")
K8S_DEPLOYMENT = os.environ.get("K8S_DEPLOYMENT", "factorio")
