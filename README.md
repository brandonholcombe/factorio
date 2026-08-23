# factorio

Private Factorio server for friends (up to ~15 concurrent) on the kodloki LKE
cluster (`tow-c1`), following the palworld game-server pattern.

## Connecting

In Factorio: **Multiplayer → Connect to address** →

```
factorio.kodloki.io:34197
```

Join password: in `k8s/factorio-secret.yaml` (gitignored, never committed).

## How it runs

- `factoriotools/factorio:stable` on the g6-standard-6 games node, pinned there
  (toleration + nodeAffinity) because the game is exposed via **hostPort
  34197/udp** — Linode NodeBalancers can't forward UDP, so
  `factorio.kodloki.io` is an A record on the node's public IP
  (`172.234.239.122`), not the ingress LoadBalancer. If the games node is ever
  recycled, update the DNS record.
- Base game (no Space Age). The image enables the DLC mods by default — its
  entrypoint rewrites `mod-list.json` on every boot from the `DLC_SPACE_AGE`
  env var, so the Deployment sets `DLC_SPACE_AGE=false`. To flip Space Age
  later: set it to `"true"`, commit, restart — the existing save migrates
  (that direction is supported; disabling again is lossy).
- Settings live in the Secret as `server-settings.json`; an initContainer
  re-installs it on every boot. To change settings: edit the Secret, apply,
  `kubectl rollout restart deployment/factorio -n factorio`.
- GitOps: ArgoCD watches `k8s/` on GitHub (`argocd-application.yaml` applied
  once by hand; the Secret is applied by hand).

## Mods (QoL only)

Installed on the `factorio-data` PVC under `/factorio/mods/` (clients
auto-download them on join): Squeak Through 2, Bottleneck Lite,
Rate Calculator (+flib), Even Distribution, Milestones, Todo List — the
newest Factorio-2.0-compatible releases.

**Mod auto-update** (recommended before the 2.1-stable jump): the image can
update mods on every boot, but needs factorio.com credentials or the server
refuses to start. Add `USERNAME` and `TOKEN` (from factorio.com/profile, or
`service-username`/`service-token` in the local `player-data.json`) to the
`stringData` of `k8s/factorio-secret.yaml`, apply it, then set
`UPDATE_MODS_ON_START: "true"` env in the Deployment with
`valueFrom: secretKeyRef` for `USERNAME`/`TOKEN`. Until then, mods are pinned
at the installed versions; if a server update ever breaks them, the pod
crash-loops until the mods are updated or removed.

## Auto-update (every 30 min)

`factorio-updater` CronJob compares the Docker Hub `stable` tag digest to the
running pod's image digest and does a `rollout restart` when they differ.
Rationale: Steam auto-updates clients, so a stale server locks everyone out.
The restart saves the map first (SIGTERM save + 5-min autosaves); players
reconnect after ~1 minute.

## Backups (every 6 h)

`factorio-backup` CronJob copies `/factorio/saves/*.zip` to the
`factorio-backups` PVC under a UTC-timestamp directory, keeping the 28 newest
(~1 week). Restore = copy a snapshot's zip back into `/factorio/saves/` (e.g.
via `kubectl cp` or a one-shot pod mounting both PVCs) and restart with
`LOAD_LATEST_SAVE` semantics in mind (it loads the newest mtime).

Run either job on demand:

```sh
kubectl create job --from=cronjob/factorio-backup  backup-now  -n factorio
kubectl create job --from=cronjob/factorio-updater update-now -n factorio
```

## Bridge: monitoring, Discord alerts + control

`bridge/` (image `bholcombe/factorio-bridge`) polls RCON every 10s and turns
enemy kill-count deltas into a tolerance model: **defense** entities lost
(walls/turrets, list in the `factorio-bridge-config` ConfigMap) are normal
waves — digest-only unless a wave exceeds the alert threshold; anything else
destroyed is a **breach** — immediate alert, and auto-pause if nobody is
online. Incident history lives in SQLite on the game volume
(`/factorio/bridge/bridge.db`).

Discord (gateway bot, alerts + commands in one configured channel):
`/status`, `/pause`, `/resume`, `/save`, `/rollback 5|10|15|20|25` (button
confirmation; archives current saves to the backups volume, scales the game
to 0, swaps in the closest autosave, scales back — requires the Application's
`ignoreDifferences` on `/spec/replicas`). Daily digest at `DIGEST_HOUR_UTC`.

Setup: create a Discord app at discord.com/developers → Bot → Reset Token →
paste into `DISCORD_BOT_TOKEN` in the gitignored secret; invite via
OAuth2 URL generator with scopes `bot` + `applications.commands` and
Send Messages/Embed Links permissions; enable Discord Dev Mode, right-click
the alert channel → Copy ID → set `DISCORD_CHANNEL_ID` in the ConfigMap.
Without a token the bridge runs headless (alerts to pod logs).

Known trade-off: stat polling uses `/silent-command`, which flags the save as
command-used (achievements disabled — already limited by mods anyway).

## Admin / RCON

RCON listens cluster-internally on TCP 27015; the image generates the password
into `/factorio/config/rconpw`. From the pod:

```sh
kubectl exec -n factorio deploy/factorio -- rcon /players online
```
