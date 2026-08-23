"""factorio-bridge entrypoint.

Runs the RCON poller + incident engine always; attaches the Discord bot when
DISCORD_BOT_TOKEN is set, otherwise runs headless (alerts go to the log) so
the bridge can be deployed and observed before the bot exists.
"""
import asyncio
import logging

from . import config
from .incidents import IncidentEngine
from .poller import Poller

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


async def headless_notify(notify: asyncio.Queue) -> None:
    while True:
        ev = await notify.get()
        log.info("ALERT (no discord token configured): %s", ev)


async def amain() -> None:
    if not config.RCON_PASSWORD:
        raise SystemExit("RCONPW is not set")
    events: asyncio.Queue = asyncio.Queue()
    notify: asyncio.Queue = asyncio.Queue()
    poller = Poller(events)
    engine = IncidentEngine(events, notify, poller)

    tasks = [poller.run(), poller.resource_loop(), engine.run()]
    if config.DISCORD_BOT_TOKEN and config.DISCORD_CHANNEL_ID:
        from .discord_bot import BridgeBot
        bot = BridgeBot(poller, engine, notify)
        tasks.append(bot.start(config.DISCORD_BOT_TOKEN))
        log.info("starting with discord bot")
    else:
        tasks.append(headless_notify(notify))
        log.warning("DISCORD_BOT_TOKEN/DISCORD_CHANNEL_ID unset — headless mode")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(amain())
