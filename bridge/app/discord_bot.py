"""Discord gateway bot: renders notify-queue events into the alert channel
and serves slash commands (/status /pause /resume /save /rollback)."""
import asyncio
import datetime as dt
import logging
import os
import time

import discord
from discord import app_commands

from . import config, rollback
from .incidents import IncidentEngine
from .poller import Poller

log = logging.getLogger("discord")


def _fmt_entities(entities: dict[str, int], limit: int = 8) -> str:
    items = sorted(entities.items(), key=lambda kv: -kv[1])
    body = ", ".join(f"{n} ×{c}" for n, c in items[:limit])
    extra = len(items) - limit
    return body + (f" (+{extra} more types)" if extra > 0 else "")


class BridgeBot(discord.Client):
    def __init__(self, poller: Poller, engine: IncidentEngine, notify: asyncio.Queue):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.poller = poller
        self.engine = engine
        self.notify = notify
        self.rollback = rollback.RollbackRunner()
        self.channel_ = None
        self._register_commands()

    async def on_ready(self):
        self.channel_ = self.get_channel(config.DISCORD_CHANNEL_ID)
        if self.channel_ is None:
            log.error("channel %s not found — check DISCORD_CHANNEL_ID and bot invite",
                      config.DISCORD_CHANNEL_ID)
            return
        guild = self.channel_.guild
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("ready; alerting to #%s in %s", self.channel_.name, guild.name)
        asyncio.create_task(self._notify_loop())
        asyncio.create_task(self._digest_loop())

    async def _send(self, text: str):
        if self.channel_ is not None:
            try:
                await self.channel_.send(text)
            except discord.DiscordException:
                log.exception("failed to send alert")

    # --- alerts ----------------------------------------------------------
    async def _notify_loop(self):
        while True:
            ev = await self.notify.get()
            kind = ev["kind"]
            in_rollback = self.rollback.lock.locked()
            if kind == "breach":
                msg = (f"🔴 **BREACH on {ev['surface']}** — destroyed: "
                       f"{_fmt_entities(ev['entities'])}")
                if ev["auto_paused"]:
                    msg += "\n⏸️ Nobody online — **world paused**. `/resume` when handled."
                await self._send(msg)
            elif kind == "breach_closed":
                await self._send(
                    f"🟠 Breach on {ev['surface']} over after {ev['duration_s']}s. "
                    f"Total lost: {_fmt_entities(ev['entities'])}")
            elif kind == "big_wave":
                await self._send(
                    f"⚔️ Heavy attack on {ev['surface']} — defenses lost: "
                    f"{_fmt_entities(ev['entities'])} (holding so far)")
            elif kind == "big_wave_closed":
                await self._send(
                    f"🛡️ Attack on {ev['surface']} repelled after {ev['duration_s']}s. "
                    f"Defense losses: {_fmt_entities(ev['entities'])}")
            elif kind == "wave_closed":
                pass  # digest-only
            elif kind == "death":
                await self._send(f"💀 A player died on {ev['surface']}.")
            elif kind == "join":
                await self._send(f"🟢 **{ev['player']}** joined.")
            elif kind == "leave":
                await self._send(f"⚪ **{ev['player']}** left.")
            elif kind == "server_down" and not in_rollback:
                await self._send("🚨 Server unreachable (RCON down for 60s+).")
            elif kind == "server_up" and not in_rollback:
                await self._send("✅ Server is back.")

    # --- daily digest ----------------------------------------------------
    async def _digest_loop(self):
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            target = now.replace(hour=config.DIGEST_HOUR_UTC, minute=0, second=0, microsecond=0)
            if target <= now:
                target += dt.timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                await self._send(self._digest_text())
            except Exception:
                log.exception("digest failed")

    def _digest_text(self) -> str:
        d = self.engine.digest(time.time() - 86400)
        snap = self.poller.snapshot or {}
        evo = snap.get("evolution", {})
        evo_s = ", ".join(f"{s}: {v:.0%}" for s, v in evo.items()) or "n/a"
        lines = [
            "📋 **Daily factory report**",
            f"• Waves absorbed: {d['wave']['count']} ({d['wave']['lost']} defense entities lost)",
            f"• Breaches: {d['breach']['count']} ({d['breach']['lost']} buildings lost)"
            + (" 🎉 clean day!" if d['breach']['count'] == 0 else ""),
            f"• Player deaths: {d['deaths']}",
            f"• Evolution: {evo_s} | Rockets launched: {snap.get('rockets', '?')}",
        ]
        last = self.engine.last_breach_at()
        if last:
            days = (time.time() - last) / 86400
            lines.append(f"• 🏭 {days:.0f} day(s) since last breach")
        return "\n".join(lines)

    # --- slash commands --------------------------------------------------
    def _register_commands(self):
        tree = self.tree

        def right_channel(itx: discord.Interaction) -> bool:
            return itx.channel_id == config.DISCORD_CHANNEL_ID

        @tree.command(name="status", description="Factorio server status")
        async def status(itx: discord.Interaction):
            if not right_channel(itx):
                return await itx.response.send_message("Use the factorio channel.", ephemeral=True)
            if self.poller.up is False or self.poller.snapshot is None:
                return await itx.response.send_message("🚨 Server looks **down** (RCON unreachable).")
            s = self.poller.snapshot
            age = int(time.time() - s["at"])
            players = ", ".join(s["players"]) or "nobody"
            ups = f"{s['ups']:.0f}" if s.get("ups") else "—"
            evo = ", ".join(f"{k}: {v:.0%}" for k, v in s["evolution"].items())
            state = "⏸️ paused" if s["paused"] else "▶️ running"
            lines = [
                f"{state} | 👥 {players} | UPS {ups} | 🚀 {s['rockets']} | 🧬 {evo}",
                f"(data {age}s old)",
            ]
            last = self.engine.last_breach_at()
            if last:
                lines.append(f"🏭 {(time.time() - last) / 86400:.1f} days since last breach")
            await itx.response.send_message("\n".join(lines))

        @tree.command(name="pause", description="Pause the game world")
        async def pause(itx: discord.Interaction):
            if not right_channel(itx):
                return await itx.response.send_message("Use the factorio channel.", ephemeral=True)
            await self.poller.set_paused(True)
            await itx.response.send_message(f"⏸️ Paused by {itx.user.display_name}.")

        @tree.command(name="resume", description="Resume the game world")
        async def resume(itx: discord.Interaction):
            if not right_channel(itx):
                return await itx.response.send_message("Use the factorio channel.", ephemeral=True)
            await self.poller.set_paused(False)
            await itx.response.send_message(f"▶️ Resumed by {itx.user.display_name}.")

        @tree.command(name="save", description="Save the game now")
        async def save(itx: discord.Interaction):
            if not right_channel(itx):
                return await itx.response.send_message("Use the factorio channel.", ephemeral=True)
            await self.poller.save()
            await itx.response.send_message("💾 Map saved.")

        @tree.command(name="rollback", description="Roll the world back (disconnects players!)")
        @app_commands.describe(minutes="How far back")
        @app_commands.choices(minutes=[
            app_commands.Choice(name=f"{m} minutes", value=m) for m in (5, 10, 15, 20, 25)])
        async def rollback_cmd(itx: discord.Interaction, minutes: app_commands.Choice[int]):
            if not right_channel(itx):
                return await itx.response.send_message("Use the factorio channel.", ephemeral=True)
            if self.rollback.lock.locked():
                return await itx.response.send_message("A rollback is already running.", ephemeral=True)
            picked = rollback.pick_save(minutes.value)
            if picked is None:
                return await itx.response.send_message("No saves found — cannot roll back.")
            path, mtime = picked
            age_min = (time.time() - mtime) / 60
            view = ConfirmRollback(self, path)
            await itx.response.send_message(
                f"⚠️ Roll back to `{os.path.basename(path)}` from **{age_min:.0f} min ago** "
                f"({dt.datetime.fromtimestamp(mtime).strftime('%H:%M:%S')} server time)?\n"
                "Everyone online will be disconnected; progress after that save is lost "
                "(current state is archived first).", view=view)


class ConfirmRollback(discord.ui.View):
    def __init__(self, bot: BridgeBot, save_path: str):
        super().__init__(timeout=60)
        self.bot = bot
        self.save_path = save_path

    @discord.ui.button(label="Roll back", style=discord.ButtonStyle.danger)
    async def confirm(self, itx: discord.Interaction, _button: discord.ui.Button):
        self.stop()
        await itx.response.edit_message(
            content=f"⏪ Rolling back (started by {itx.user.display_name})…", view=None)
        try:
            await self.bot.rollback.run(self.save_path, self.bot._send)
            await self.bot._send("✅ Rollback complete — server is up, rejoin away.")
            self.bot.engine.note("rollback", os.path.basename(self.save_path))
        except Exception as exc:
            log.exception("rollback failed")
            await self.bot._send(f"❌ Rollback FAILED: {exc}. Check the bridge logs; "
                                 "the pre-rollback archive on the backups volume is intact.")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, itx: discord.Interaction, _button: discord.ui.Button):
        self.stop()
        await itx.response.edit_message(content="Rollback cancelled.", view=None)
