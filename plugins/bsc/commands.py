import asyncio
import discord
import json
import os
import re
import time

from core import Plugin, Status, PersistentReport, utils, Server, command, get_translation, Group, ServiceRegistry
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import tasks, commands
from fastapi import APIRouter, Request, Response
from services.bot import DCSServerBot
from typing import Literal

from .const import RANK_CODES, get_rank_for_xp
from .lua_parser import parse_lua_table

_ = get_translation(__name__.split('.')[1])

DEFAULT_STATS_DIR = r"D:\PlayerStats"
# Filename pattern for the per-theatre mission save file. The literal
# `pretense_*_2.0.lua` is produced by the mission script in DCS and must not be renamed here.
ZONE_FILE_PATTERN = "pretense_*_2.0.lua"

# Human-friendly labels for raw Pretense stat keys shown in /rank.
STAT_LABELS = {
    "SB": "Survival Multiplier",
}

# Sleekplan feedback platform (https://sleekplan.com). We receive new feedback / status changes /
# comments / votes via Sleekplan webhooks (see init_sleekplan_webhook); the REST API is read-broken
# (list GET /v1/post -> 500, single GET /v1/post/{id} -> 405), so webhooks are the only data source.
# Map Sleekplan status slugs/IDs -> (display label, embed colour); see _sleekplan_status_meta() for
# the lookup order and the fallback handling of unmapped values.
SLEEKPLAN_STATUS = {
    "open": ("Open", discord.Color.blurple()),
    "reviewing": ("Under Review", discord.Color.gold()),
    "under-review": ("Under Review", discord.Color.gold()),
    "under_review": ("Under Review", discord.Color.gold()),
    "planned": ("Planned", discord.Color.teal()),
    "in-progress": ("In Progress", discord.Color.from_rgb(26, 188, 156)),
    "in_progress": ("In Progress", discord.Color.from_rgb(26, 188, 156)),
    "merged": ("Merged", discord.Color.from_rgb(150, 180, 60)),
    "complete": ("Completed", discord.Color.green()),
    "completed": ("Completed", discord.Color.green()),
    "closed": ("Closed", discord.Color.from_rgb(1, 1, 1)),   # near-black (#010101; pure #000000 renders as Discord's default)
    # Custom BSC statuses. Sleekplan sends these as opaque hash IDs with no label in the payload,
    # so we hardcode them here (this is our private fork). Add new ones the same way.
    "s68dfa8421c534": ("Not planned currently", discord.Color.from_rgb(231, 76, 60)),   # red
    "s68e0189a9265c": ("Details required", discord.Color.from_rgb(232, 67, 147)),       # pink  (verify)
    "s68e3d0c4b414d": ("Votes required", discord.Color.from_rgb(232, 67, 147)),         # pink  (verify)
}

# Sleekplan custom statuses (and boards) arrive as opaque ID hashes, e.g. status "s68dfa8421c534"
# or board "t68df02a7ea223": a letter followed by hex digits, with no human-readable name in the
# payload. Known ones are hardcoded in SLEEKPLAN_STATUS above; unmapped hashes are shown as
# "Unknown" rather than printing the gibberish. Normal slugs (open, planned, closed, ...) contain
# non-hex letters so they never match this.
SLEEKPLAN_ID_RE = re.compile(r'^[a-z][0-9a-f]{10,}$')

# Title emoji per status for the status-change embed (falls back to 🔄 if a status isn't listed).
SLEEKPLAN_STATUS_EMOJI = {
    "open": "📥",
    "reviewing": "🔍",
    "under-review": "🔍",
    "under_review": "🔍",
    "planned": "⏳",
    "in-progress": "🚧",
    "in_progress": "🚧",
    "merged": "🔀",
    "complete": "✅",
    "completed": "✅",
    "closed": "❌",
    "s68dfa8421c534": "🚫",   # Not planned currently
    "s68e0189a9265c": "❓",   # Details required (verify)
    "s68e3d0c4b414d": "🗳️",   # Votes required (verify)
}


class BSC(Plugin):

    def __init__(self, bot: DCSServerBot):
        super().__init__(bot)
        # Persist highest known ranks across leaderboard updates to avoid flapping role assignments
        # when servers are restarted or their player stats files temporarily disappear.
        self.highest_ranks: dict[str, int] = {}
        # Cache of parsed per-UCID stats keyed by ucid -> (mtime, stats_dict).
        # With ~12k JSON files in the global stats dir, re-reading every cycle is wasteful;
        # we re-read only files whose mtime changed.
        self._stats_cache: dict[str, tuple[float, dict]] = {}
        # (channel_id, entry_index) -> last fire monotonic time, for per-keyword-set cooldowns
        self._autoresponder_last_fired: dict[tuple[int, int], float] = {}
        # Sleekplan webhook receiver state (FastAPI app + router we register on the WebService).
        self._sleekplan_app = None
        self._sleekplan_router: APIRouter | None = None
        self._sleekplan_webhook_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        await super().cog_load()
        self.update_leaderboard.start()
        config = self.get_config()
        if config:
            interval = config.get('update_interval', 120)
            self.update_leaderboard.change_interval(seconds=interval)
            sleekplan = config.get('sleekplan') or {}
            # Sleekplan pushes events to us in real time; register a webhook route on the bot's
            # WebService once it is up.
            if sleekplan.get('channel') and (sleekplan.get('webhook') or {}).get('secret'):
                self._sleekplan_webhook_task = asyncio.create_task(self.init_sleekplan_webhook())

    async def cog_unload(self) -> None:
        self.update_leaderboard.cancel()
        if self._sleekplan_webhook_task:
            self._sleekplan_webhook_task.cancel()
        # Remove our webhook route(s) from the shared FastAPI app to avoid duplicates on reload.
        if self._sleekplan_app is not None and self._sleekplan_router is not None:
            for route in self._sleekplan_router.routes:
                try:
                    self._sleekplan_app.routes.remove(route)
                except ValueError:
                    pass
            self._sleekplan_router = None
        await super().cog_unload()

    bsc = Group(name="bsc", description=_("BSC commands"))

    def _get_stats_dir(self) -> str:
        config = self.get_config() or {}
        return os.path.expandvars(config.get('stats_dir', DEFAULT_STATS_DIR))

    def _read_player_files(self, stats_dir: str) -> dict[str, dict]:
        """Read all per-UCID JSON files. Returns {ucid: {"stats": dict, "career": dict}}."""
        result: dict[str, dict] = {}
        if not os.path.isdir(stats_dir):
            self.log.warning("BSC: stats_dir %s does not exist.", stats_dir)
            return result
        seen: set[str] = set()
        try:
            it = os.scandir(stats_dir)
        except OSError as ex:
            self.log.warning("BSC: cannot scan stats_dir %s: %s", stats_dir, ex)
            return result
        with it:
            for entry in it:
                if not entry.name.endswith('.json'):
                    continue
                ucid = entry.name[:-5]
                if not utils.is_ucid(ucid):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                seen.add(ucid)
                cached = self._stats_cache.get(ucid)
                if cached and cached[0] == mtime:
                    result[ucid] = cached[1]
                    continue
                try:
                    with open(entry.path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(data, dict):
                    continue
                stats = data.get('stats')
                if not isinstance(stats, dict):
                    continue
                career = data.get('career')
                if not isinstance(career, dict):
                    career = {}
                payload = {"stats": stats, "career": career}
                self._stats_cache[ucid] = (mtime, payload)
                result[ucid] = payload
        # Drop UCIDs whose files have disappeared
        for ucid in list(self._stats_cache):
            if ucid not in seen:
                del self._stats_cache[ucid]
        return result

    def _read_player_stats(self, stats_dir: str) -> dict[str, dict]:
        """Backward-compatible wrapper returning {ucid: stats_dict} from the shared cache."""
        return {ucid: data["stats"] for ucid, data in self._read_player_files(stats_dir).items()}

    async def _resolve_zone_file(self, server: Server) -> str | None:
        """Return the path of the active pretense_<theatre>_2.0.lua save file for `server`, or None."""
        config = self.get_config(server) or {}
        configured = config.get('zone_file_path')
        if configured:
            return os.path.expandvars(utils.format_string(configured, instance=server.instance))
        saves_dir = os.path.join(await server.get_missions_dir(), 'Saves')
        try:
            _, files = await server.node.list_directory(saves_dir, pattern=ZONE_FILE_PATTERN)
        except Exception as ex:
            self.log.debug("BSC: failed to list %s: %s", saves_dir, ex)
            return None
        return files[0] if files else None

    async def _load_zones(self, server: Server) -> dict:
        """Load the `zones` table from the latest pretense_<theatre>_2.0.lua for this server."""
        zone_file = await self._resolve_zone_file(server)
        if not zone_file:
            return {}
        try:
            file_data = await server.node.read_file(zone_file)
        except FileNotFoundError:
            return {}
        try:
            parsed = parse_lua_table(file_data.decode(encoding='utf-8'))
        except Exception as ex:
            self.log.warning("BSC: could not parse %s: %s", zone_file, ex)
            return {}
        if not isinstance(parsed, dict):
            return {}
        zones = parsed.get('zones') or {}
        return zones if isinstance(zones, dict) else {}

    @command(description=_('Display BSC rank and XP for a pilot'))
    @utils.app_has_role('DCS')
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction,
                   user: discord.Member | None = None):
        # noinspection PyUnresolvedReferences
        await interaction.response.defer()
        target = user or interaction.user
        ucid = await self.bot.get_ucid_by_member(target)
        if not ucid:
            await interaction.followup.send(
                _("{} has no linked DCS account.").format(target.display_name),
                ephemeral=True,
            )
            return

        loop = asyncio.get_running_loop()
        stats_dir = self._get_stats_dir()
        all_data = await loop.run_in_executor(None, self._read_player_files, stats_dir)
        player_data = all_data.get(ucid)
        if not player_data:
            await interaction.followup.send(
                _("No BSC stats found for {}.").format(target.display_name),
                ephemeral=True,
            )
            return
        player_stats = player_data["stats"]
        player_career = player_data.get("career") or {}

        xp = int(player_stats.get("XP", 0) or 0)
        rank_level, rank_data = get_rank_for_xp(xp)
        # Prefer the Discord role mention (highest configured at-or-below the player's level);
        # fall back to the rank code + name if no role is configured for that rank.
        rank_label = f"`{rank_data['code']}` {rank_data['name']}" if rank_data else _("Unranked")
        if rank_level:
            role_config = (self.get_config() or {}).get('rank_roles') or {}
            applicable = []
            for code, role_id in role_config.items():
                lvl = RANK_CODES.get(str(code).upper())
                if lvl and lvl <= rank_level:
                    applicable.append((lvl, role_id))
            if applicable:
                applicable.sort(reverse=True)
                rank_label = f"<@&{applicable[0][1]}>"

        # Compute leaderboard position from the same dataset (cache-warm after the update loop)
        scored: list[tuple[str, int]] = []
        for u, pd in all_data.items():
            try:
                px = int(pd["stats"].get("XP") or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            scored.append((u, px))
        scored.sort(key=lambda item: item[1], reverse=True)
        total = len(scored)
        place = next((i for i, (u, _x) in enumerate(scored, start=1) if u == ucid), None)

        embed = discord.Embed(
            title=_("BSC Rank"),
            description=_("for {}").format(target.mention),
            color=discord.Color.gold(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name=_("XP"), value=f"**{xp:,}**", inline=True)
        embed.add_field(name=_("Rank"), value=rank_label, inline=True)
        if place is not None and total:
            embed.add_field(name=_("Place"), value=f"**#{place}** / {total}", inline=True)
        hidden_keys = {"XP", "SB_tmp"}
        extras = {k: v for k, v in player_stats.items()
                  if k not in hidden_keys and isinstance(v, (int, float))}
        for key in sorted(extras):
            value = extras[key]
            formatted = f"{value:,}" if isinstance(value, int) else f"{value:,.2f}"
            embed.add_field(name=STAT_LABELS.get(key, key), value=formatted, inline=True)

        career_fields = [
            ("missionsCompleted", _("Missions Completed")),
            ("suppliesDelivered", _("Supplies Delivered")),
        ]
        career_values = [
            (label, player_career.get(key))
            for key, label in career_fields
            if isinstance(player_career.get(key), (int, float)) and player_career.get(key)
        ]
        if career_values:
            embed.add_field(name="​", value="​", inline=False)
            embed.add_field(name=_("▬▬▬▬▬▬▬ Service Record ▬▬▬▬▬▬▬"), value="​", inline=False)
            for label, value in career_values:
                formatted = f"{value:,}" if isinstance(value, int) else f"{value:,.2f}"
                embed.add_field(name=label, value=formatted, inline=True)
        embed.set_footer(text=_("Updated"))
        embed.timestamp = discord.utils.utcnow()
        await interaction.followup.send(embed=embed,
                                        allowed_mentions=discord.AllowedMentions.none())

    @command(description=_('Show the global BSC leaderboard (top 50 pilots)'))
    @utils.app_has_role('DCS')
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        # noinspection PyUnresolvedReferences
        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        stats_dir = self._get_stats_dir()
        stats = await loop.run_in_executor(None, self._read_player_stats, stats_dir)

        scored: list[tuple[str, int]] = []
        for ucid, player_stats in stats.items():
            xp = player_stats.get("XP")
            if xp is None:
                continue
            try:
                xp = int(xp)
            except (TypeError, ValueError):
                continue
            if xp <= 0:
                continue
            scored.append((ucid, xp))
        scored.sort(key=lambda item: item[1], reverse=True)
        scored = scored[:50]

        if not scored:
            await interaction.followup.send(_("No BSC pilots found."), ephemeral=True)
            return

        medals = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}  # 🥇 🥈 🥉
        pilot_col: list[str] = []
        xp_col: list[str] = []
        rank_col: list[str] = []
        for idx, (ucid, xp) in enumerate(scored, start=1):
            name = await self._resolve_pilot_name(ucid, max_len=20)
            _rank, rank_data = get_rank_for_xp(xp)
            rank_code = rank_data["code"] if rank_data else "—"
            if idx <= 3:
                pilot_col.append(f"{medals[idx]} **{name}**")
                xp_col.append(f"**{xp:,}**")
            else:
                pilot_col.append(f"**#{idx:<2}** {name}")
                xp_col.append(f"{xp:,}")
            rank_col.append(rank_code)

        embed = discord.Embed(
            title=_("BSC Leaderboard"),
            color=discord.Color.gold(),
        )
        # Top 10 as the first chunk, then split the rest so each Pilot field stays under
        # Discord's 1024-char limit (mentions are ~22 chars each, so the 40 remaining
        # entries need to be split further when many pilots are mention-linked).
        chunk_sizes = [10, 20, 20]
        start = 0
        for size in chunk_sizes:
            if start >= len(pilot_col):
                break
            end = min(start + size, len(pilot_col))
            if start == 0:
                section = _("Top {}").format(end)
            else:
                section = _("Place {}-{}").format(start + 1, end)
                # spacer to keep the previous chunk's last row from butting against the section header
                embed.add_field(name="​", value="​", inline=False)
            embed.add_field(name=section, value="​", inline=False)
            embed.add_field(name=_("Pilot"), value="\n".join(pilot_col[start:end]), inline=True)
            embed.add_field(name=_("XP"), value="\n".join(xp_col[start:end]), inline=True)
            embed.add_field(name=_("Rank"), value="\n".join(rank_col[start:end]), inline=True)
            start = end
        embed.set_image(
            url="https://media.discordapp.net/attachments/1218982642163253398/1404223677981266034/SOP.png"
        )
        embed.set_footer(text=_("Updated"))
        embed.timestamp = discord.utils.utcnow()
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _resolve_pilot_name(self, ucid: str, max_len: int = 22) -> str:
        """Resolve a UCID to a short display string for embed rendering.

        Returns a Discord mention (<@id>) when a linked Member is found so the
        leaderboard links to their profile. Callers must suppress allowed_mentions
        if they don't want the mention to actually ping.
        """
        result = await self.bot.get_member_or_name_by_ucid(ucid)
        if isinstance(result, discord.Member):
            return result.mention
        if isinstance(result, str) and result:
            raw = result
        else:
            return f"`{ucid[:8]}…`"
        if len(raw) > max_len:
            raw = raw[:max_len - 1] + "…"
        return discord.utils.escape_markdown(raw)

    @bsc.command(description=_('Reset BSC progress'))
    @utils.app_has_role('DCS Admin')
    @app_commands.guild_only()
    async def reset(self, interaction: discord.Interaction,
                    what: Literal['persistence', 'statistics', 'roles', 'all'],
                    server: app_commands.Transform[
                        Server, utils.ServerTransformer(status=[Status.STOPPED, Status.SHUTDOWN])
                    ] | None = None):
        if what in ['persistence', 'all'] and not server:
            await interaction.response.send_message(
                _("Please specify a server to reset persistence."),
                ephemeral=True
            )
            return
        if server and server.status not in [Status.STOPPED, Status.SHUTDOWN]:
            # noinspection PyUnresolvedReferences
            await interaction.response.send_message(
                _("Server {} needs to be shut down to reset the BSC progress!").format(server.display_name),
                ephemeral=True)
            return
        ephemeral = utils.get_ephemeral(interaction)
        if what in ['statistics', 'all']:
            confirm = _("Do you really want to reset the BSC progress? "
                        "This will erase player statistics for ALL servers (global stats directory).")
        else:
            confirm = _("Do you really want to reset the BSC progress?")
        if not await utils.yn_question(interaction, confirm):
            await interaction.followup.send(_("Aborted."), ephemeral=ephemeral)
            return
        if what == 'persistence' or what == 'all':
            saves_dir = os.path.join(await server.get_missions_dir(), 'Saves')
            await server.node.remove_file(os.path.join(saves_dir, "pretense_*.lua"))
            await server.node.remove_file(os.path.join(saves_dir, "pretense_*.json"))
            await interaction.followup.send(_("BSC persistence reset."), ephemeral=ephemeral)
        if what == 'statistics' or what == 'all':
            stats_dir = self._get_stats_dir()
            await self.node.remove_file(os.path.join(stats_dir, "*.json"))
            self._stats_cache.clear()
            await interaction.followup.send(
                _("BSC statistics reset (global directory {}).").format(stats_dir),
                ephemeral=ephemeral
            )
        if what == 'roles' or what == 'all':
            rank_roles = self.get_config().get('rank_roles', {}) or {}
            if not rank_roles:
                await interaction.followup.send(_("No BSC rank roles configured."), ephemeral=ephemeral)
                return
            roles_to_clear = []
            for role_id in rank_roles.values():
                role = self.bot.get_role(role_id)
                if not role:
                    self.log.warning("BSC: Discord role %s not found.", role_id)
                    continue
                roles_to_clear.append(role)
            if not roles_to_clear:
                await interaction.followup.send(_("No BSC rank roles found to reset."), ephemeral=ephemeral)
                return
            members_to_roles = {}
            for role in roles_to_clear:
                for member in role.members:
                    members_to_roles.setdefault(member, []).append(role)
            for member, roles in members_to_roles.items():
                try:
                    await member.remove_roles(*roles)
                except discord.Forbidden:
                    await self.bot.audit('permission "Manage Roles" missing.', user=self.bot.member)
                    break
                except discord.HTTPException as ex:
                    self.log.exception(ex)
            self.highest_ranks.clear()
            await interaction.followup.send(_("BSC rank roles reset."), ephemeral=ephemeral)

    @tasks.loop(seconds=120)
    async def update_leaderboard(self):
        rank_roles = self.get_config().get('rank_roles', {}) or {}
        if rank_roles:
            highest_ranks = self.highest_ranks
        else:
            self.highest_ranks = {}
            highest_ranks = None

        # Stats are global — read once per cycle, reuse across servers.
        loop = asyncio.get_running_loop()
        stats_dir = self._get_stats_dir()
        try:
            stats = await loop.run_in_executor(None, self._read_player_stats, stats_dir)
        except Exception as ex:
            self.log.exception(ex)
            stats = {}

        if highest_ranks is not None and stats:
            ranks = self._collect_player_ranks({"stats": stats})
            for ucid, rank in ranks.items():
                if rank > highest_ranks.get(ucid, 0):
                    highest_ranks[ucid] = rank

        for server in self.bot.servers.values():
            try:
                if server.status != Status.RUNNING:
                    continue
                config = self.get_config(server)
                if not config:
                    continue
                channel_id = config.get('channel')
                if not channel_id:
                    continue
                zones = await self._load_zones(server)
                if not zones and not stats:
                    continue
                data = {"stats": stats, "zones": zones}
                report = PersistentReport(self.bot, self.plugin_name, "bsc.json", embed_name="leaderboard",
                                          channel_id=channel_id,
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
            if not (attachment.filename.startswith('pretense') and
                    (attachment.filename.endswith('.json') or attachment.filename.endswith('.lua'))):
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
                await message.channel.send(_('BSC file {} uploaded.').format(attachment.filename))
            except Exception as ex:
                self.log.exception(ex)
                await message.channel.send(_('BSC file {} could not be uploaded!').format(attachment.filename))
            finally:
                await message.delete()

    @commands.Cog.listener(name="on_message")
    async def autoresponder(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content:
            return
        entries = (self.get_config() or {}).get('autoresponder') or []
        if not entries:
            return
        now = time.monotonic()
        for idx, entry in enumerate(entries):
            keywords = entry.get('keywords') or []
            if not keywords:
                continue
            pattern = r'\b(?:' + '|'.join(re.escape(kw) for kw in keywords) + r')\b'
            if not re.search(pattern, message.content, re.IGNORECASE):
                continue
            cooldown = int(entry.get('cooldown', 60))
            key = (message.channel.id, idx)
            if now - self._autoresponder_last_fired.get(key, 0.0) < cooldown:
                return
            embed = self._build_autoresponder_embed(entry.get('embed') or {})
            if not embed:
                return
            try:
                await message.channel.send(
                    embed=embed,
                    reference=message,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                self._autoresponder_last_fired[key] = now
            except discord.HTTPException as ex:
                self.log.warning("BSC autoresponder send failed: %s", ex)
            return

    @staticmethod
    def _build_autoresponder_embed(cfg: dict) -> discord.Embed | None:
        title = cfg.get('title')
        description = cfg.get('description')
        if not title and not description:
            return None
        color_value = cfg.get('color', 'blue')
        if isinstance(color_value, int):
            color = discord.Color(color_value)
        else:
            factory = getattr(discord.Color, str(color_value), None)
            color = factory() if callable(factory) else discord.Color.blue()
        embed = discord.Embed(color=color)
        if title:
            embed.title = title
        if cfg.get('url'):
            embed.url = cfg['url']
        if description:
            embed.description = description
        if cfg.get('thumbnail'):
            embed.set_thumbnail(url=cfg['thumbnail'])
        if cfg.get('image'):
            embed.set_image(url=cfg['image'])
        if cfg.get('footer'):
            embed.set_footer(text=cfg['footer'])
        return embed

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
                self.log.warning("BSC: Unknown rank code %s in configuration.", rank_code)
                continue
            role = self.bot.get_role(role_id)
            if not role:
                self.log.warning("BSC: Discord role %s for rank %s not found.", role_id, rank_code)
                continue
            rank_roles[level] = role
        if not rank_roles:
            return
        for ucid, level in ranks.items():
            member = self.bot.get_member_by_ucid(ucid, verified=True)
            if not member:
                continue
            # Find the highest configured role at or below the player's level
            applicable = [lvl for lvl in rank_roles if lvl <= level]
            desired_level = max(applicable) if applicable else None
            desired_role = rank_roles.get(desired_level) if desired_level else None
            roles_to_remove = [role for lvl, role in rank_roles.items()
                               if role in member.roles and lvl != desired_level and role != desired_role]
            if not roles_to_remove and (not desired_role or desired_role in member.roles):
                # Member already has the correct role and no other rank roles to clean up.
                continue
            try:
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove)
                if desired_role and desired_role not in member.roles:
                    await member.add_roles(desired_role)
            except discord.Forbidden:
                await self.bot.audit('permission "Manage Roles" missing.', user=self.bot.member)
            except discord.HTTPException as ex:
                self.log.exception(ex)

    # ---------------------------------------------------------------------------
    # Sleekplan feedback integration
    #
    # Sleekplan POSTs webhook events to a route we register on the bot's WebService. We diff each
    # event against a small persisted ledger (bsc_sleekplan.json: per-post status, highest vote
    # milestone, title and last-known vote tallies) and announce new posts, status changes, vote
    # milestones and comments as embeds in the configured channel. A post is recorded silently the
    # first time we see it, so we never retro-announce a status/milestone it already had.
    # ---------------------------------------------------------------------------
    def _sleekplan_state_path(self) -> str:
        return os.path.join(self.node.config_dir, 'plugins', 'bsc_sleekplan.json')

    def _load_sleekplan_state(self) -> dict:
        try:
            with open(self._sleekplan_state_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get('items'), dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"seeded": False, "items": {}}

    def _save_sleekplan_state(self, state: dict) -> None:
        path = self._sleekplan_state_path()
        try:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f)
            os.replace(tmp, path)
        except OSError as ex:
            self.log.warning("BSC/Sleekplan: could not save state file %s: %s", path, ex)

    @staticmethod
    def _sleekplan_item_id(item: dict) -> str:
        return str(item.get('feedback_id') or item.get('id') or '')

    @staticmethod
    def _sleekplan_status_emoji(status) -> str:
        """Title emoji for a status-change embed, keyed on the NEW status (🔄 fallback)."""
        return SLEEKPLAN_STATUS_EMOJI.get(str(status or '').strip().lower(), '\U0001F504')

    @staticmethod
    def _sleekplan_status_meta(status) -> tuple[str, discord.Color]:
        slug = str(status or '').strip().lower()
        if not slug:
            return _("Unknown"), discord.Color.blurple()
        if slug in SLEEKPLAN_STATUS:
            return SLEEKPLAN_STATUS[slug]
        # An unmapped opaque Sleekplan ID hash (e.g. a newly-added custom status): show a
        # placeholder instead of the gibberish. Add it to SLEEKPLAN_STATUS to give it a label.
        if SLEEKPLAN_ID_RE.match(slug):
            return _("Unknown"), discord.Color.blurple()
        return slug.replace('-', ' ').replace('_', ' ').title(), discord.Color.blurple()

    @staticmethod
    def _sleekplan_post_url(item: dict, cfg: dict) -> str | None:
        base = (cfg.get('board_url') or '').rstrip('/')
        fid = item.get('feedback_id') or item.get('id')
        if base and fid:
            return f"{base}/feedback/{fid}"
        return item.get('url') or item.get('link')

    @staticmethod
    def _sleekplan_title(prefix: str, text) -> str:
        """Compose an embed title as 'prefix text', capped at Discord's 256-char title limit."""
        title = f"{prefix} {str(text or '').strip()}".strip()
        return title if len(title) <= 256 else title[:255].rstrip() + '…'

    @staticmethod
    def _parse_sleekplan_dt(value) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _apply_sleekplan_author(embed: discord.Embed, user, suffix: str = '') -> None:
        if not isinstance(user, dict):
            return
        name = user.get('data_name') or user.get('name')
        if not name:
            return
        # The author line renders as "<avatar> <name>" above the title; `suffix` lets callers
        # append e.g. "commented" -> "<avatar> Jane commented".
        label = f"{name} {suffix}".strip() if suffix else str(name)
        # Discord rejects a non-URL icon_url with HTTP 400. Only set it when we actually
        # have a well-formed http(s) URL; otherwise omit it (a user may have no avatar).
        img = str(user.get('data_img') or user.get('img') or '').strip()
        if img.startswith(('http://', 'https://')):
            embed.set_author(name=label, icon_url=img)
        else:
            embed.set_author(name=label)

    # ---------------------------------------------------------------------------
    # Webhook receiver. Sleekplan POSTs events to a route we register on the bot's WebService.
    # Handled actions: item.create (new post), item.update (edit/status change), vote.create
    # (a vote), item.delete (removal), comment.create (new comment). The request carries a shared
    # secret as the `?key=` query param; the body is {product_id, action, data, timestamp}. For
    # item.*/comment.create the post/comment object is `data` itself; for vote.create the post is
    # nested under data['feedback_item'] (see _handle_sleekplan_event).
    # ---------------------------------------------------------------------------
    async def init_sleekplan_webhook(self):
        from services.webservice import WebService

        # Give the WebService up to ~10s to come up (e.g. on a master switch).
        web_service = None
        for _ in range(10):
            web_service = ServiceRegistry.get(WebService)
            if web_service and web_service.is_running():
                break
            await asyncio.sleep(1)
        else:
            self.log.error("BSC/Sleekplan: WebService is not running - webhook NOT registered. "
                           "Enable it via config/services/webservice.yaml.")
            return
        app = web_service.app
        if not app:
            self.log.error("BSC/Sleekplan: WebService has no FastAPI app - webhook NOT registered.")
            return
        cfg = (self.get_config() or {}).get('sleekplan') or {}
        path = (cfg.get('webhook') or {}).get('path') or '/sleekplan'
        if not path.startswith('/'):
            path = '/' + path
        self._sleekplan_app = app
        self._sleekplan_router = APIRouter()
        self._sleekplan_router.add_api_route(path, self._sleekplan_webhook, methods=["POST"],
                                             include_in_schema=False)
        app.include_router(self._sleekplan_router)
        self.log.info("BSC/Sleekplan: webhook receiver registered at POST %s", path)

    async def _sleekplan_webhook(self, request: Request):
        cfg = (self.get_config() or {}).get('sleekplan') or {}
        secret = (cfg.get('webhook') or {}).get('secret')
        # Verify the shared secret passed by Sleekplan as ?key=...
        if secret and request.query_params.get('key') != str(secret):
            self.log.warning("BSC/Sleekplan: webhook called with a missing/invalid key from %s.",
                             request.client.host if request.client else "?")
            return Response(status_code=403)
        try:
            payload = await request.json()
        except Exception:
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)
        # Optional: ignore events for other products if a product_id filter is configured.
        # (We still accept payloads that omit product_id.) Logged under debug so a mismatch
        # configured by accident doesn't silently swallow every event.
        product_id = cfg.get('product_id')
        if product_id and str(payload.get('product_id')) not in (str(product_id), 'None'):
            if cfg.get('debug'):
                self.log.info("BSC/Sleekplan: ignoring event for product_id=%s (configured %s).",
                              payload.get('product_id'), product_id)
            return Response(status_code=200)
        channel = self.bot.get_channel(int(cfg['channel'])) if cfg.get('channel') else None
        if not channel:
            self.log.warning("BSC/Sleekplan: webhook channel %s not found.", cfg.get('channel'))
            return Response(status_code=200)  # ack so Sleekplan doesn't retry
        try:
            await self._handle_sleekplan_event(channel, payload, cfg)
        except Exception as ex:
            self.log.exception(ex)
        # Always 2xx-acknowledge receipt, even on our own errors, to stop Sleekplan retrying.
        return Response(status_code=200)

    async def _handle_sleekplan_event(self, channel: discord.abc.Messageable, payload: dict, cfg: dict) -> None:
        action = str(payload.get('action') or '')
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        events = set(cfg.get('events') or ['post', 'comment', 'status'])
        if cfg.get('debug'):
            self.log.info("BSC/Sleekplan: webhook action=%s data=%s", action, json.dumps(data)[:500])
        # The post object lives directly in `data` for item.* events, but vote.create wraps it
        # under data['feedback_item']. Resolve to the post object for either shape.
        item = data.get('feedback_item') if isinstance(data.get('feedback_item'), dict) else data
        if action == 'item.create':
            self._remember_sleekplan_status(item)
            if 'post' in events:
                await self._announce_sleekplan_post(channel, item, cfg)
        elif action == 'item.update':
            # item.update fires on content/status edits; only the status diff is announced here.
            await self._on_sleekplan_item_update(channel, item, cfg, events)
        elif action == 'vote.create':
            # Votes arrive as their own event (NOT item.update), with the post under feedback_item.
            await self._on_sleekplan_vote(channel, item, cfg, events)
        elif action == 'item.delete':
            # Drop the post from our ledger so deleted posts don't accumulate as dead entries.
            self._forget_sleekplan_item(item)
        elif action == 'comment.create':
            if 'comment' in events:
                await self._announce_sleekplan_comment(channel, data, cfg)

    @staticmethod
    def _sleekplan_vote_count(data: dict) -> int:
        """Total votes cast (upvotes + downvotes) - the metric vote milestones are measured against.
        Sleekplan's total_sum is already up+down, so we use it as the fallback when up/down aren't
        both present."""
        def _int(key):
            try:
                return int(data.get(key))
            except (TypeError, ValueError):
                return None
        up, down = _int('total_up'), _int('total_down')
        if up is not None and down is not None:
            return up + down
        return _int('total_sum') or 0

    @staticmethod
    def _sleekplan_up_down(item: dict):
        """Return (upvotes, downvotes) as ints if both are present in the payload, else None."""
        def _int(key):
            try:
                return int(item.get(key))
            except (TypeError, ValueError):
                return None
        up, down = _int('total_up'), _int('total_down')
        return (up, down) if up is not None and down is not None else None

    def _add_sleekplan_vote_fields(self, embed: discord.Embed, item: dict) -> None:
        """Add separate Upvotes / Downvotes columns, but only when the payload carries them."""
        ud = self._sleekplan_up_down(item)
        if ud is not None:
            embed.add_field(name=_("Upvotes"), value=str(ud[0]), inline=True)
            embed.add_field(name=_("Downvotes"), value=str(ud[1]), inline=True)

    def _add_sleekplan_status_field(self, embed: discord.Embed, item: dict) -> None:
        """Add a Status column, but only when the payload carries a status (parents of comment
        events may not)."""
        if item.get('status'):
            label, _color = self._sleekplan_status_meta(item.get('status'))
            embed.add_field(name=_("Status"), value=label, inline=True)

    def _sleekplan_vote_milestones(self, cfg: dict | None = None) -> list[int]:
        if cfg is None:
            cfg = (self.get_config() or {}).get('sleekplan') or {}
        raw = cfg.get('vote_milestones')
        if raw is None:
            raw = [5, 10, 15, 20, 25]
        out = set()
        for v in raw:
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n > 0:
                out.add(n)
        return sorted(out)

    @staticmethod
    def _highest_milestone_reached(votes: int, thresholds: list[int]) -> int:
        reached = [t for t in thresholds if votes >= t]
        return max(reached) if reached else 0

    def _remember_sleekplan_status(self, item: dict) -> None:
        """Seed the baseline (status + highest vote milestone) for a freshly created post so we
        never retro-announce a status change or vote milestone the post already had."""
        if self._sleekplan_item_id(item):
            self._apply_sleekplan_item_state(item)

    def _forget_sleekplan_item(self, item: dict) -> None:
        """Drop a deleted post from the state ledger (item.delete) so it doesn't linger."""
        fid = self._sleekplan_item_id(item)
        if not fid:
            return
        state = self._load_sleekplan_state()
        if (state.get('items') or {}).pop(fid, None) is not None:
            self._save_sleekplan_state(state)

    def _sleekplan_stored_item(self, fid) -> dict:
        """Look up a post's cached info (title/status/total_up/total_down) from the ledger,
        recorded when we last saw it via a create/status/vote event."""
        if not fid:
            return {}
        state = self._load_sleekplan_state()
        return (state.get('items') or {}).get(str(fid)) or {}

    def _apply_sleekplan_item_state(self, item: dict, cfg: dict | None = None) -> dict | None:
        """Merge the current status + vote milestone for a post into the state file (without
        clobbering other stored fields) and report what changed. Returns a dict with keys
        known / old_status / new_status / old_milestone / new_milestone, or None if no post id."""
        fid = self._sleekplan_item_id(item)
        if not fid:
            return None
        new_status = str(item.get('status') or '')
        thresholds = self._sleekplan_vote_milestones(cfg)
        new_milestone = self._highest_milestone_reached(self._sleekplan_vote_count(item), thresholds)
        state = self._load_sleekplan_state()
        items = state.setdefault('items', {})
        existing = items.get(fid)
        known = existing is not None        # have we recorded this post before?
        old_status = (existing or {}).get('status')
        old_milestone = (existing or {}).get('votes_milestone')
        merged = dict(existing or {})
        merged['status'] = new_status
        # The stored milestone is monotonic (max ever seen) so a vote being removed and re-added
        # near a threshold doesn't re-announce the same milestone.
        merged['votes_milestone'] = max(old_milestone or 0, new_milestone)
        # Remember the post title + vote tallies so comment events (whose payload carries neither)
        # can still show them, falling back to this last-known snapshot.
        if item.get('title'):
            merged['title'] = str(item.get('title'))
        ud = self._sleekplan_up_down(item)
        if ud is not None:
            merged['total_up'], merged['total_down'] = ud
        items[fid] = merged
        state['seeded'] = True
        self._save_sleekplan_state(state)
        return {"known": known, "old_status": old_status, "new_status": new_status,
                "old_milestone": old_milestone, "new_milestone": new_milestone}

    async def _on_sleekplan_item_update(self, channel: discord.abc.Messageable, item: dict,
                                        cfg: dict, events: set) -> None:
        diff = self._apply_sleekplan_item_state(item, cfg)
        if not diff:
            return
        # Status change: only announce a change we can describe (we must know the previous status).
        # NB: we deliberately do NOT check vote milestones here. item.update carries the post's
        # current vote count, but those votes may have been cast long ago (or before the webhook
        # existed) and simply revealed now - announcing a milestone on a status change would be
        # wrong/misleading. The new count is still recorded above (silently) as the baseline, so a
        # genuine future vote.create that crosses a higher threshold will announce correctly.
        if ('status' in events and diff['old_status'] is not None
                and diff['new_status'] and diff['new_status'] != diff['old_status']):
            await self._announce_sleekplan_status(channel, item, cfg)

    async def _on_sleekplan_vote(self, channel: discord.abc.Messageable, item: dict,
                                 cfg: dict, events: set) -> None:
        diff = self._apply_sleekplan_item_state(item, cfg)
        if diff:
            await self._maybe_announce_sleekplan_milestone(channel, item, cfg, events, diff)

    async def _maybe_announce_sleekplan_milestone(self, channel: discord.abc.Messageable, item: dict,
                                                  cfg: dict, events: set, diff: dict) -> None:
        # Only announce a genuinely newly-reached threshold for a post we already had a milestone
        # baseline for (otherwise we'd retro-announce a pre-existing vote count).
        if ('votes' in events and diff['known'] and diff['old_milestone'] is not None
                and diff['new_milestone'] > diff['old_milestone']):
            await self._announce_sleekplan_votes(channel, item, diff['new_milestone'], cfg)

    def _build_sleekplan_post_embed(self, item: dict, cfg: dict) -> discord.Embed:
        label, color = self._sleekplan_status_meta(item.get('status'))
        embed = discord.Embed(
            title=item.get('title') or _("New feedback"),
            url=self._sleekplan_post_url(item, cfg),
            color=color,
        )
        desc = str(item.get('description') or '').strip()
        if desc:
            embed.description = desc[:1024]
        self._apply_sleekplan_author(embed, item.get('user'))
        # NB: the webhook payload's `type` is an opaque Sleekplan board ID (e.g. "t68df02a7ea223"),
        # not a readable board/category name, so we don't surface it. The clickable title links to
        # the post if a board context is needed.
        embed.add_field(name=_("Status"), value=label, inline=True)
        embed.set_footer(text="BSC Feedback Hub")
        embed.timestamp = self._parse_sleekplan_dt(item.get('created')) or discord.utils.utcnow()
        return embed

    async def _announce_sleekplan_post(self, channel: discord.abc.Messageable, item: dict, cfg: dict) -> None:
        embed = self._build_sleekplan_post_embed(item, cfg)
        embed.title = self._sleekplan_title("\U0001F195", embed.title)  # 🆕
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _announce_sleekplan_status(self, channel: discord.abc.Messageable, item: dict,
                                         cfg: dict) -> None:
        new_label, color = self._sleekplan_status_meta(item.get('status'))
        embed = discord.Embed(
            title=self._sleekplan_title(self._sleekplan_status_emoji(item.get('status')),
                                        item.get('title') or _('Feedback')),
            url=self._sleekplan_post_url(item, cfg),
            color=color,
            description=_("Status changed to: **{new}**").format(new=new_label),
        )
        self._apply_sleekplan_author(embed, item.get('user'))
        self._add_sleekplan_vote_fields(embed, item)
        embed.set_footer(text="BSC Feedback Hub")
        embed.timestamp = discord.utils.utcnow()
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _announce_sleekplan_votes(self, channel: discord.abc.Messageable, item: dict,
                                        milestone: int, cfg: dict) -> None:
        embed = discord.Embed(
            title=self._sleekplan_title("\U0001F525", item.get('title') or _('Feedback')),  # 🔥
            url=self._sleekplan_post_url(item, cfg),
            color=discord.Color.orange(),
            description=_("Reached **{n} votes**!").format(n=milestone),
        )
        self._apply_sleekplan_author(embed, item.get('user'))
        self._add_sleekplan_status_field(embed, item)
        self._add_sleekplan_vote_fields(embed, item)
        embed.set_footer(text="BSC Feedback Hub")
        embed.timestamp = discord.utils.utcnow()
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _announce_sleekplan_comment(self, channel: discord.abc.Messageable, data: dict, cfg: dict) -> None:
        """Announce a single comment delivered via the comment.create webhook."""
        text = str(data.get('value') or data.get('comment') or data.get('text') or '').strip()
        # The parent post may be nested under 'feedback'/'post' or referenced by id/title on `data`.
        parent = data.get('feedback') if isinstance(data.get('feedback'), dict) else (
            data.get('post') if isinstance(data.get('post'), dict) else {})
        fid = self._sleekplan_item_id(parent) or str(data.get('feedback_id') or data.get('post_id') or '')
        # The comment payload carries no post title/status/votes, so fall back to the snapshot we
        # cached for the post when we last saw it via a create/status/vote event (payload wins).
        post = {**self._sleekplan_stored_item(fid), **parent}
        title = (post.get('title') or data.get('feedback_title') or data.get('title') or _('Feedback'))
        url = self._sleekplan_post_url({'feedback_id': fid} if fid else parent, cfg)
        embed = discord.Embed(
            title=self._sleekplan_title("\U0001F4AC", title),  # 💬
            url=url,
            color=discord.Color.blue(),
            description=text[:1024] or _("New comment"),
        )
        self._apply_sleekplan_author(embed, data.get('user'), suffix=_("commented"))
        self._add_sleekplan_status_field(embed, post)
        self._add_sleekplan_vote_fields(embed, post)
        embed.set_footer(text="BSC Feedback Hub")
        embed.timestamp = self._parse_sleekplan_dt(data.get('created')) or discord.utils.utcnow()
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: DCSServerBot):
    await bot.add_cog(BSC(bot))
