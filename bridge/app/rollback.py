"""Rollback orchestration — the proven manual procedure, automated:

archive current saves -> scale factorio to 0 (graceful) -> copy chosen
autosave over the main save -> scale to 1 -> wait ready.

Requires the ArgoCD Application to ignore /spec/replicas (RespectIgnoreDifferences),
otherwise selfHeal fights the scale-down.
"""
import asyncio
import glob
import logging
import os
import shutil
import time

from . import config
from .k8s import K8s

log = logging.getLogger("rollback")

SELECTOR = "app=factorio"


def candidate_saves() -> list[tuple[str, float]]:
    """Autosaves plus the main save, newest first, as (path, mtime)."""
    paths = glob.glob(os.path.join(config.SAVES_DIR, "_autosave*.zip"))
    main = os.path.join(config.SAVES_DIR, f"{config.SAVE_NAME}.zip")
    if os.path.exists(main):
        paths.append(main)
    saves = [(p, os.path.getmtime(p)) for p in paths]
    saves.sort(key=lambda s: s[1], reverse=True)
    return saves


def pick_save(minutes_back: int) -> tuple[str, float] | None:
    """Save whose age is closest to the requested age, preferring older
    (rolling back less than asked defeats the purpose)."""
    target = time.time() - minutes_back * 60
    older = [s for s in candidate_saves() if s[1] <= target]
    if older:
        return older[0]  # newest of the older-than-target saves
    saves = candidate_saves()
    return saves[-1] if saves else None  # oldest we have, best effort


class RollbackRunner:
    def __init__(self):
        self.k8s = K8s(config.K8S_NAMESPACE)
        self.lock = asyncio.Lock()

    async def run(self, save_path: str, progress) -> None:
        """progress: async callable(str) for user-facing updates."""
        async with self.lock:
            ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            archive = os.path.join(config.BACKUPS_DIR, f"pre-rollback-{ts}")
            os.makedirs(archive, exist_ok=True)
            for path, _ in candidate_saves():
                await asyncio.to_thread(shutil.copy2, path, archive)
            await progress(f"Archived current saves to `{os.path.basename(archive)}`. "
                           "Stopping the server (players will be disconnected)…")

            await self.k8s.scale(config.K8S_DEPLOYMENT, 0)
            await self.k8s.wait_gone(SELECTOR)

            main = os.path.join(config.SAVES_DIR, f"{config.SAVE_NAME}.zip")
            await asyncio.to_thread(shutil.copyfile, save_path, main)
            os.utime(main)  # newest mtime so LOAD_LATEST_SAVE picks it
            log.info("installed %s as %s", save_path, main)
            await progress("Save swapped. Restarting the server…")

            await self.k8s.scale(config.K8S_DEPLOYMENT, 1)
            await self.k8s.wait_ready(SELECTOR)
