import asyncio
import time

from core import Coalition, EventListener, Plugin, Server, ServiceRegistry, Status, event, get_translation, utils
from discord.ext import tasks
from services.bot import BotService
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import BSC

_ = get_translation(__name__.split('.')[1])


class BSCListener(EventListener["BSC"]):
    """Automatically restarts a server whose FPS stays below a configured minimum (fps_restart)."""

    def __init__(self, plugin: Plugin):
        super().__init__(plugin)
        # server name -> monotonic time of the first perfmon reading below the FPS threshold
        self._below_since: dict[str, float] = {}
        # server name -> monotonic time of the last perfmon event (or of the moment we started watching)
        self._last_perfmon: dict[str, float] = {}
        utils.safe_start(self.fps_watchdog)

    async def shutdown(self) -> None:
        await utils.safe_cancel(self.fps_watchdog)

    @event(name="registerDCSServer")
    async def registerDCSServer(self, server: Server, _data: dict) -> None:
        self._below_since.pop(server.name, None)
        self._last_perfmon[server.name] = time.monotonic()

    @event(name="perfmon")
    async def perfmon(self, server: Server, data: dict) -> None:
        self._last_perfmon[server.name] = time.monotonic()
        config = (self.get_config(server) or {}).get('fps_restart')
        if not isinstance(config, dict):
            return
        fps = float(data['fps'])
        min_fps = config.get('min', 10)
        if fps >= min_fps:
            # hysteresis: only a clearly healthy reading ends a low-FPS streak - a single blip
            # barely above the minimum must not reset the accumulated period
            if fps >= min_fps * 1.2:
                self._below_since.pop(server.name, None)
            return
        now = time.monotonic()
        first = self._below_since.setdefault(server.name, now)
        # perfmon fires once per 3600 sim frames, so at low FPS the events arrive slower than
        # once a minute - measure the period in wall-clock time instead of counting events.
        if (now - first) < config.get('period', 5) * 60:
            return
        self._below_since.pop(server.name, None)
        asyncio.create_task(self.restart_server(server, config, fps))

    @tasks.loop(minutes=1)
    async def fps_watchdog(self):
        """Catches servers whose sim is so slow (or frozen) that perfmon events stop arriving.

        perfmon fires once per 3600 sim frames, so a server at the FPS minimum reports every
        3600/min seconds. A RUNNING, unpaused server that stays silent for twice that long must
        be far below the minimum - the exact situation fps_restart exists for, but one where the
        event-driven path above never gets a reading to act on. Relies on the monitoring plugin
        being active for every server (it is the source of perfmon).
        """
        for server in list(self.bot.servers.values()):
            try:
                config = (self.get_config(server) or {}).get('fps_restart')
                if not isinstance(config, dict) or server.is_remote:
                    continue
                if server.status != Status.RUNNING:
                    # no sim frames are expected while loading / paused / stopped
                    self._last_perfmon.pop(server.name, None)
                    self._below_since.pop(server.name, None)
                    continue
                now = time.monotonic()
                last = self._last_perfmon.setdefault(server.name, now)
                min_fps = config.get('min', 10)
                silence = max(2 * 3600 / min_fps, config.get('period', 5) * 60)
                if (now - last) < silence or server.restart_pending:
                    continue
                self._last_perfmon[server.name] = now  # re-arm instead of refiring every minute
                self._below_since.pop(server.name, None)
                fps = round(3600 / (now - last), 2)
                self.log.warning(f"BSC: no FPS report from server {server.name} for {int(now - last)}s, "
                                 f"the sim is frozen or below {fps} FPS.")
                asyncio.create_task(self.restart_server(server, config, fps))
            except Exception as ex:
                self.log.error(f"BSC: error in fps_watchdog for server {server.name}: {ex}", exc_info=True)

    async def restart_server(self, server: Server, config: dict, fps: float) -> None:
        fps = round(fps, 2)
        if server.maintenance:
            self.log.info(f"BSC: FPS of server {server.name} is {fps}, "
                          f"not restarting (maintenance mode).")
            return
        if server.restart_pending:
            return
        if server.is_populated() and not config.get('populated', True):
            self.log.info(f"BSC: FPS of server {server.name} is {fps}, "
                          f"not restarting (players are online).")
            return
        server.restart_pending = True
        try:
            if server.is_populated():
                await server.sendPopupMessage(
                    Coalition.ALL,
                    config.get('message', _('Server is being restarted due to low performance.'))
                )
            alert = _("Server {server} FPS ({fps}) has been below {min_fps} for more than {period} minutes. "
                      "The server is being restarted.").format(
                server=server.name, fps=fps, min_fps=config.get('min', 10), period=config.get('period', 5))
            self.log.warning(alert)
            if config.get('mentioning', True):
                asyncio.create_task(ServiceRegistry.get(BotService).alert(
                    title=_("Server Performance Low!"), message=alert, server=server))
            await self.bot.audit(f"Server restarted due to low FPS ({fps}).", server=server)
            if config.get('shutdown', False):
                await server.shutdown()
                await server.startup()
            else:
                await server.restart(modify_mission=config.get('run_extensions', True))
        except Exception as ex:
            self.log.error(f"BSC: error while restarting server {server.name} due to low FPS: {ex}",
                           exc_info=True)
        finally:
            server.restart_pending = False
