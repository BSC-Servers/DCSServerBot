import discord
import json
import os

from core import Plugin, Status, PersistentReport, Channel, utils, Server, Report, get_translation, Group
from discord import app_commands
from discord.ext import tasks, commands
from discord.utils import MISSING
from services.bot import DCSServerBot
from typing import Literal

from .const import RANK_CODES, get_rank_for_xp

_ = get_translation(__name__.split('.')[1])


class Pretense(Plugin):

    def __init__(self, bot: DCSServerBot):
        super().__init__(bot)
        self.last_mtime = dict()

    async def cog_load(self) -> None:
        await super().cog_load()
        self.update_leaderboard.start()
        config = self.get_config()
        if config:
            interval = config.get('update_interval', 120)
            self.update_leaderboard.change_interval(seconds=interval)

    async def cog_unload(self) -> None:
        self.update_leaderboard.cancel()
        await super().cog_unload()

    # New command group "/mission"
    pretense = Group(name="pretense", description=_("Commands to manage Pretense missions"))

    @pretense.command(description=_('Display the Pretense stats'))
    @utils.app_has_role('DCS')
    @app_commands.guild_only()
    async def stats(self, interaction: discord.Interaction,
                    server: app_commands.Transform[Server, utils.ServerTransformer]):
        # noinspection PyUnresolvedReferences
        await interaction.response.defer()
        config = self.get_config(server) or {}
        json_file_path = config.get('json_file_path',
                                    os.path.join(await server.get_missions_dir(), 'Saves', "player_stats.json"))
        json_file_path = os.path.expandvars(utils.format_string(json_file_path, instance=server.instance))
        json_file_path = os.path.expandvars(json_file_path)
        try:
            file_data = await server.node.read_file(json_file_path)
        except FileNotFoundError:
            await interaction.followup.send(
                _("No {} found on this server! Is Pretense active?").format(os.path.basename(json_file_path)),
                ephemeral=True
            )
            return
        content = file_data.decode(encoding='utf-8')
        data = json.loads(content)
        report = Report(self.bot, self.plugin_name, "pretense.json")
        env = await report.render(data=data, server=server)
        try:
            file = discord.File(fp=env.buffer, filename=env.filename) if env.filename else MISSING
            msg = await interaction.original_response()
            await msg.edit(embed=env.embed, attachments=[file],
                           delete_after=self.bot.locals.get('message_autodelete'))
        finally:
            if env.buffer:
                env.buffer.close()

    @pretense.command(description=_('Reset Pretense progress'))
    @utils.app_has_role('DCS Admin')
    @app_commands.guild_only()
    async def reset(self, interaction: discord.Interaction,
                    server: app_commands.Transform[Server, utils.ServerTransformer(status=[
                        Status.STOPPED, Status.SHUTDOWN])], what: Literal['persistence', 'statistics', 'both']):
        if server.status not in [Status.STOPPED, Status.SHUTDOWN]:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(
                _("Server {} needs to be shut down to reset the Pretense progress!").format(server.display_name),
                ephemeral=True)
            return
        ephemeral = utils.get_ephemeral(interaction)
        if not await utils.yn_question(interaction, _("Do you really want to reset the Pretense progress?")):
            await interaction.followup.send(_("Aborted."), ephemeral=ephemeral)
        if what == 'persistence' or what == 'both':
            path = os.path.join(await server.get_missions_dir(), 'Saves', "pretense_*.json")
            await server.node.remove_file(path)
            await interaction.followup.send(_("Pretense persistence reset."), ephemeral=ephemeral)
        if what == 'statistics' or what == 'both':
            path = os.path.join(await server.get_missions_dir(), 'Saves', "player_stats*.json")
            await server.node.remove_file(path)
            await interaction.followup.send(_("Pretense statistics reset."), ephemeral=ephemeral)

    @tasks.loop(seconds=120)
    async def update_leaderboard(self):
        rank_roles = self.get_config().get('rank_roles', {}) or {}
        highest_ranks = dict() if rank_roles else None
        for server in self.bot.servers.values():
            try:
                if server.status != Status.RUNNING:
                    continue
                config = self.get_config(server)
                if not config:
                    continue
                json_file_path = config.get(
                    'json_file_path',
                    os.path.join(await server.get_missions_dir(), 'Saves', "player_stats.json")
                )
                json_file_path = os.path.expandvars(utils.format_string(json_file_path, instance=server.instance))
                json_file_path = os.path.expandvars(json_file_path)
                try:
                    file_data = await server.node.read_file(json_file_path)
                except FileNotFoundError:
                    continue
                content = file_data.decode(encoding='utf-8')
                data = json.loads(content)
                if highest_ranks is not None:
                    ranks = self._collect_player_ranks(data)
                    for ucid, rank in ranks.items():
                        if rank > highest_ranks.get(ucid, 0):
                            highest_ranks[ucid] = rank
                report = PersistentReport(self.bot, self.plugin_name, "pretense.json", embed_name="leaderboard",
                                          channel_id=config.get('channel', server.channels[Channel.STATUS]),
                                          server=server)
                await report.render(data=data, server=server)
            except Exception as ex:
                self.log.exception(ex)
        if highest_ranks is not None:
            await self._apply_rank_roles(highest_ranks, rank_roles)

    @update_leaderboard.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # ignore bot messages
        if message.author.bot:
            return
        if not message.attachments or not utils.check_roles(self.bot.roles['DCS Admin'], message.author):
            return
        server: Server = self.bot.get_server(message, admin_only=True)
        for attachment in message.attachments:
            if not (attachment.filename in ['player_stats.json', 'player_stats_v2.0.json'] or
                    (attachment.filename.startswith('pretense') and attachment.filename.endswith('.json'))):
                continue
            if not server:
                ctx = await self.bot.get_context(message)
                # check if there is a central admin channel configured
                admin_channel = self.bot.locals.get('channels', {}).get('admin')
                if not admin_channel or admin_channel != message.channel.id:
                    return
                try:
                    server = await utils.server_selection(self.bot, ctx,
                                                          title=_("To which server do you want to upload to?"))
                    if not server:
                        await ctx.send(_('Upload aborted.'))
                        return
                except Exception as ex:
                    self.log.exception(ex)
                    return
            try:
                filename = os.path.join(await server.get_missions_dir(), 'Saves', attachment.filename)
                await server.node.write_file(filename, attachment.url, overwrite=True)
                await message.channel.send(_('Pretense file {} uploaded.').format(attachment.filename))
            except Exception as ex:
                self.log.exception(ex)
                await message.channel.send(_('Pretense file {} could not be uploaded!').format(attachment.filename))
            finally:
                await message.delete()

    @staticmethod
    def _collect_player_ranks(data: dict) -> dict[str, int]:
        ranks = {}
        stats = data.get("stats", {})
        if not isinstance(stats, dict):
            return ranks
        for player, player_stats in stats.items():
            if not isinstance(player_stats, dict):
                continue
            xp = player_stats.get("XP")
            if xp is None:
                continue
            try:
                xp = int(xp)
            except (TypeError, ValueError):
                continue
            ucid = player if utils.is_ucid(player) else player_stats.get("ucid")
            if not utils.is_ucid(ucid):
                continue
            rank, _ = get_rank_for_xp(xp)
            if rank is None:
                continue
            if rank > ranks.get(ucid, 0):
                ranks[ucid] = rank
        return ranks

    async def _apply_rank_roles(self, ranks: dict[str, int], role_config: dict) -> None:
        if not ranks:
            return
        if not self.bot.guilds:
            return
        rank_roles: dict[int, discord.Role] = {}
        for rank_code, role_id in role_config.items():
            rank_code = str(rank_code).upper()
            level = RANK_CODES.get(rank_code)
            if not level:
                self.log.warning("Pretense: Unknown rank code %s in configuration.", rank_code)
                continue
            role = self.bot.get_role(role_id)
            if not role:
                self.log.warning("Pretense: Discord role %s for rank %s not found.", role_id, rank_code)
                continue
            rank_roles[level] = role
        if not rank_roles:
            return
        for ucid, level in ranks.items():
            member = self.bot.get_member_by_ucid(ucid, verified=True)
            if not member:
                continue
            desired_role = rank_roles.get(level)
            roles_to_remove = [role for lvl, role in rank_roles.items()
                               if role in member.roles and lvl != level]
            try:
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove)
                if desired_role and desired_role not in member.roles:
                    await member.add_roles(desired_role)
            except discord.Forbidden:
                await self.bot.audit('permission "Manage Roles" missing.', user=self.bot.member)
            except discord.HTTPException as ex:
                self.log.exception(ex)


async def setup(bot: DCSServerBot):
    await bot.add_cog(Pretense(bot))
