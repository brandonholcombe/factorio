# factorio

Private Factorio server for friends (up to ~15 concurrent) on the kodloki LKE
cluster (`tow-c1`), plus **factorio-bridge**: a monitoring/control sidecar
service with a Discord bot ("KL-Factorio-Overseer"), damage-tolerance alerts,
auto-pause, and save rollback.

## Connecting

In Factorio: **Multiplayer → Connect to address** →

```
factorio.kodloki.io:34197
```

Join password: in `k8s/factorio-secret.yaml` (gitignored, never committed).
No mod installs needed — the server's QoL mods auto-sync to clients on join.

## Architecture

```
friends ──UDP 34197──▶ games node public IP (172.234.239.122, hostPort)
                          │  factorio  (factoriotools/factorio:stable)
Discord ◀──gateway──  factorio-bridge (bholcombe/factorio-bridge)
                          │  └─ RCON poll (10s vitals, 5min resource scan)
                          └─ shared PVCs: factorio-data, factorio-backups
```

- **Why hostPort + node-IP DNS**: Linode NodeBalancers can't forward UDP, so
  `factorio.kodloki.io` is an A record on the games node itself (same pattern
  as palworld). If that node is ever recycled, update the DNS record.
- Server and bridge are pinned to the g6-standard-6 games node (toleration
  `dedicated=terraria` + instance-type affinity): the RWO block-storage
  volumes and the hostPort live there.
- **GitOps**: ArgoCD app `factorio` watches `k8s/` on GitHub.
  `argocd-application.yaml` and the Secret are hand-applied (the Application
  ignores the game Deployment's `/spec/replicas` so the bridge may scale it
  during rollbacks).
- Canonical repo: `github.com/brandonholcombe/factorio` (watched by ArgoCD).
  Mirror: `haxley.luckyenough.us/brandonw.h2o/factorio`.

## Game server

- Base game, vanilla map, `DLC_SPACE_AGE=false` (the image force-manages
  `mod-list.json` from that env — see Space Age below).
- QoL mods (auto-synced to clients): Squeak Through 2, Bottleneck Lite,
  Rate Calculator (+flib), Even Distribution, Milestones, Todo List.
- Settings live in the Secret as `server-settings.json`; an initContainer
  installs it (and the fixed RCON password) on every boot. Edit Secret →
  `kubectl apply` → `kubectl rollout restart deployment/factorio -n factorio`.
- **24/7 world**: `auto_pause: false` — the factory runs while nobody is on,
  under bridge watch (breach auto-pause, brownout/UPS/resource alerts).
  Freeze it deliberately with `/pause` in Discord. (If reverting to
  pause-when-empty, flip `auto_pause` AND the bridge's `GAME_AUTO_PAUSE`
  together.)
- **Server auto-update**: CronJob every 30 min compares the Docker Hub
  `stable` tag digest to the running pod and rolling-restarts on change
  (digest compare avoids restart loops while Docker Hub lags factorio.com).
  Steam auto-updates clients, so the server must track stable.
- **Mod auto-update**: `UPDATE_MODS_ON_START=true` with factorio.com creds
  (`USERNAME`/`TOKEN` in the Secret) — mods re-sync to the server version on
  every boot, keeping them in lockstep across game updates.
- **Backups**: CronJob every 6h copies `saves/*.zip` to the
  `factorio-backups` volume, keeping 28 snapshots (~1 week).

## Discord bot (#assembly-line)

Commands (`/help` in-channel lists these too):

| command | what it does |
|---|---|
| `/status` | run/idle/paused, players, UPS, evolution, rockets, last breach |
| `/report` | last-24h digest on demand |
| `/incidents` | recent attacks/breaches from the incident DB |
| `/production [item]` | top-10 rates last minute, or one item over 1m/10m/1h |
| `/research` | current tech + progress bar |
| `/resources` | tapped ore vs peak, worst first (see below) |
| `/military` | artillery shells crafted/fired/stock + kills by class |
| `/map [surface]` | current base map image (rendered from snapshots) |
| `/timelapse [surface]` | base-expansion GIF (6h frames, shared bounds) |
| `/update` | check for a server update now (auto-check: 4am Pacific) |
| `/tolerance add\|remove\|list` | budgets for expected losses (autocompleted) |
| `/saves` | saves on the server with ages (rollback targets) |
| `/save` | save the map now |
| `/pause` / `/resume` | freeze/resume the world (resume clears auto-pause) |
| `/rollback 5..25` | confirm-button restore of the closest autosave |

Automatic posts: joins/leaves, player deaths, heavy-wave summaries, breach
alerts, server down/up, 8-hourly rocket summaries, research completions, evolution
threshold crossings (25/50/75/90%), sustained UPS < 55, brownouts,
tapped-resource low warnings, and a daily digest (8am Pacific).

### Damage tolerance model

The bridge polls enemy kill-count deltas (per surface) every 10s and
classifies destroyed entities:

- **defense** (walls/turrets/gates… — `DEFENSE_ENTITIES` in the ConfigMap):
  normal wave chatter. Digest-only unless one wave exceeds
  `WAVE_ALERT_THRESHOLD`.
- **production** (anything else): **breach** — immediate alert, and if nobody
  is online the bridge auto-pauses the world (`/resume` to clear).
- Unknown entity names alarm rather than stay silent (safe default for the
  Space Age flip); trees/rocks/biter friendly-fire are prefix-ignored.

### Rollback

`/rollback N` archives all current saves to the backups volume
(`pre-rollback-<ts>/`), scales the game to 0, copies the autosave closest to
N minutes old over `kodloki.zip`, scales back up. ~2 min of downtime;
disconnected players rejoin.

### Tapped-resource alerts

Every 5 min the bridge sums ore within reach of every mining drill
(tile-deduped), tracks the **peak** per resource per surface in SQLite, and
alerts once when a resource falls below `RESOURCE_ALERT_PCT` (20%) of peak —
re-arming when a new patch is tapped. Peak means "since tracking began";
originals aren't retroactively knowable. Infinite resources (oil) are
display-only.

## Repo layout

```
bridge/            bridge service (Python 3.12; deps: discord.py)
  app/main.py        wiring; headless mode if no bot token
  app/poller.py      RCON vitals loop, resource scan, control commands
  app/incidents.py   tolerance model, SQLite history, digests
  app/discord_bot.py bot commands + alert rendering
  app/rollback.py    save-swap orchestration
  app/k8s.py         minimal in-cluster API client (scale + pod watch)
  app/rcon.py        minimal Source-RCON client
k8s/               manifests (ArgoCD-synced except the two hand-applied ones)
  factorio.yaml            namespace, PVCs, game Deployment
  factorio-bridge.yaml     bridge Deployment, SA/RBAC, rcon Service, ConfigMap
  factorio-backup.yaml     6-hourly save backups
  factorio-updater.yaml    30-min server image auto-update
  factorio-secret.yaml     GITIGNORED: passwords, tokens, server-settings
  argocd-application.yaml  hand-applied Application (replicas ignored)
```

## Operations

```sh
export KUBECONFIG=~/.kube/linode-config

kubectl -n factorio logs deploy/factorio -c factorio     # game log
kubectl -n factorio logs deploy/factorio-bridge          # bridge/bot log
kubectl -n factorio exec deploy/factorio -c factorio -- rcon "/players online"

# Bridge image release (bump N, then update k8s/factorio-bridge.yaml):
cd bridge && docker buildx build --platform linux/amd64 \
  -t bholcombe/factorio-bridge:N --push .

# Run a backup / update check on demand (ArgoCD prunes these jobs after
# completion — expected):
kubectl -n factorio create job --from=cronjob/factorio-backup backup-now
```

Secrets in `k8s/factorio-secret.yaml` (gitignored): game join password +
full `server-settings.json`, factorio.com `USERNAME`/`TOKEN` (mod updates),
`RCONPW` (shared game↔bridge), `DISCORD_BOT_TOKEN`. Rotate the bot token at
discord.com/developers → Bot → Reset Token, paste, `kubectl apply`, restart
the bridge.

## Space Age (planned upgrade)

When everyone owns the DLC:
1. `DLC_SPACE_AGE: "true"` in `k8s/factorio.yaml` (existing save migrates;
   the reverse direction is lossy).
2. Append the DLC turret types to `DEFENSE_ENTITIES` in the bridge ConfigMap.
3. Commit, push; the bridge is already per-surface ("breach on Vulcanus").

## Known trade-offs

- Bridge polling uses RCON `/silent-command`, which permanently flags the
  save as command-used → achievements disabled (already limited by mods).
- Breach detection sees entities *destroyed*, not damaged-in-progress
  (damage events would need a client-synced mod — deliberately avoided).
- The node-IP DNS record needs a manual update if the games node is recycled.

## Later phases

- Raspberry Pi + LEDs/display + physical kill switch, consuming a bridge API.

- Grafana/Prometheus export from the bridge.
