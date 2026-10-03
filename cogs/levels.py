"""Levels cog: message XP, rank cards and the server leaderboard.

Every tunable (XP range, cooldown, level curve, texts) lives at the top of
this file so the command logic never has to change for a balance tweak.
"""

import logging
import random
import time

import discord
from discord import app_commands
from discord.abc import Messageable
from discord.ext import commands, tasks

from db import (
    add_balance,
    add_ignored_channel,
    add_ignored_role,
    add_xp_with_cooldown,
    clear_level_up_channel,
    clear_role_multipliers,
    ensure_guild_settings,
    get_guild_settings,
    get_leaderboard,
    get_rank_position,
    get_user,
    list_ignored_channels,
    list_ignored_roles,
    list_role_multipliers,
    mark_voice_join,
    remove_ignored_channel,
    remove_ignored_role,
    remove_role_multiplier,
    reset_xp,
    set_level,
    set_level_up_channel,
    set_role_multiplier,
    set_xp,
)
from cogs.economy import coins

# ------------------------------------------------------------------ settings
XP_MIN = 15                  # XP for a single message
XP_MAX = 25
XP_COOLDOWN = 60             # seconds between two XP gains for the same user
LEVEL_UP_COINS_PER_LEVEL = 1  # coins paid per level reached (0 = no reward)

# The starting values written to the database the first time a server is seen.
# After that everything is read back from the database, per server, and is
# managed from Discord with `/levels levelup`, `/levels ignorechannel` and
# `/levels ignorerole` - editing these three no longer moves a server that has
# been configured at least once.
DEFAULT_LEVEL_UP_CHANNEL_ID: int | None = 863349874505678850
DEFAULT_IGNORED_CHANNEL_IDS: frozenset[int] = frozenset()  # e.g. {123} mutes #spam
DEFAULT_IGNORED_ROLE_IDS: frozenset[int] = frozenset({866824826077446204})

# A settings read is reused for this many seconds, which keeps the check in
# front of every message a cache hit while still picking up changes that were
# made by another instance of the bot.
SETTINGS_CACHE_TTL = 60.0
LIST_FIELD_LIMIT = 9         # entries one field of `/levels settings` lists

# voice XP: sitting in a voice channel pays out once per interval
VOICE_XP_ENABLED = True
VOICE_XP_MIN = 15            # XP for a full interval of voice time
VOICE_XP_MAX = 25
VOICE_XP_INTERVAL = 60       # seconds in voice that pay out
VOICE_XP_IGNORE_MUTED = True            # muted/deafened members earn nothing
VOICE_XP_REQUIRE_OTHER_MEMBERS = False  # True = must not sit alone in a channel
VOICE_XP_IGNORE_AFK_CHANNEL = True

# XP multipliers: a role can earn more (or less) XP than everybody else
MULTIPLIER_MIN = 0.1            # /levels multiplier refuses anything below
MULTIPLIER_MAX = 10.0           # ... or above this
MULTIPLIER_STEP = 0.05          # how much one press of the slider moves it
MULTIPLIER_DECIMALS = 2         # what /levels multiplier stores and shows
MULTIPLIER_DEFAULT = 1.0        # a member with no booster role
MULTIPLIER_STACKS = True       # True = the boosters of a member add up
MULTIPLIER_LIST_LIMIT = 9       # roles one field of /levels settings lists

# user facing texts
LEVEL_UP_MESSAGE = "🎉 {mention} теперь имеет **{level}** уровень!"
LEVEL_UP_REWARD_MESSAGE = "{reward}"
LEADERBOARD_TITLE = "🏆 Таблица лидеров {guild} "
EMPTY_LEADERBOARD = "Таблица лидеров пока пуста"
PERMISSION_ERROR = "Необходимы **Manage Server** права для использования этой команды."
MEMBER_NOT_FOUND_ERROR = "Не смог найти такого пользователя."
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

MISSING_CHANNEL = "<#{channel_id}> (канал удалён)"

# how an id of an ignore list is shown in `/levels settings`
CHANNEL_TARGET = "<#{target_id}>"
ROLE_TARGET = "<@&{target_id}>"

LEVEL_UP_CHANNEL_SET = "✅ Канал повышений уровня: {channel}."
LEVEL_UP_CHANNEL_EVERYWHERE_ON = (" Туда идут **все** повышения, "
                                  "даже полученные в переписке.")
LEVEL_UP_CHANNEL_EVERYWHERE_OFF = (" Пока только те, что получены не в переписке"
                                   " (например за голос); остальные остаются там,"
                                   " где получен опыт.")
LEVEL_UP_CHANNEL_CLEARED = ("🗑️ Канал повышений уровня убран: повышения остаются"
                            " там, где получен опыт, а голос идёт в системный"
                            " канал сервера.")
LEVEL_UP_CHANNEL_IGNORED_WARNING = ("\n⚠️ {channel} в списке игнорируемых каналов"
                                    " - опыт там не начисляется, но повышения"
                                    " всё равно придут.")
LEVEL_UP_CHANNEL_NO_ACCESS = "Не могу писать в {channel}: не хватает {perms}."
LEVEL_UP_EVERYWHERE_NEEDS_CHANNEL = (
    " Флаг «все повышения туда» без канала не работает - он сброшен.")
LEVEL_UP_NONE = ("не задан (повышения остаются там, где получен опыт; голос идёт"
                 " в системный канал сервера)")

CHANNEL_IGNORED = ("🔇 {channel} больше не приносит опыт: ни за сообщения,"
                   " ни за голос.")
CHANNEL_ALREADY_IGNORED = "{channel} уже в списке игнорируемых каналов."
CHANNEL_UNIGNORED = "🔉 {channel} снова приносит опыт."
CHANNEL_NOT_IGNORED = "{channel} и так не в списке игнорируемых каналов."
CHANNEL_IS_LEVEL_UP = ("\n⚠️ {channel} сейчас канал повышений уровня - сообщения"
                       " о повышениях в нём останутся.")

ROLE_EVERYONE = ("Роль @everyone игнорировать нельзя: это отключило бы опыт"
                 " всему серверу.")
ROLE_IGNORED = "🔇 Участники с ролью **{role}** больше не получают опыт."
ROLE_ALREADY_IGNORED = "**{role}** уже в списке игнорируемых ролей."
ROLE_UNIGNORED = "🔉 Участники с ролью **{role}** снова получают опыт."
ROLE_NOT_IGNORED = "**{role}** и так не в списке игнорируемых ролей."

SETTINGS_TITLE = "⚙️ Настройки уровней **{guild}**"
SETTINGS_CHANNEL_FIELD = "Канал повышений"
SETTINGS_EVERYWHERE_FIELD = "Все повышения туда"
ROLE_NOT_IGNORED = "**{role}** и так не в списке игнорируемых ролей."

# XP multipliers
ROLE_MULTIPLIER_EVERYONE = ("Роль @everyone не может иметь множитель: это изменило"
                            " бы опыт всего сервера разом.")
MULTIPLIER_SET = "🚀 Участники с ролью **{role}** получают **{multiplier}×** опыта."
MULTIPLIER_UNCHANGED = "**{role}** и так даёт **{multiplier}×** опыта."
MULTIPLIER_CLEARED = "🔇 Участники с ролью **{role}** снова получают обычный опыт."
MULTIPLIER_NOT_CLEARED = "**{role}** и так не имел множителя."
MULTIPLIERS_ALL_CLEARED = ("🗑️ Убрал все роли с множителем: их было **{count}**."
                          " Теперь у всех обычный опыт.")
MULTIPLIERS_NOTHING_TO_CLEAR = "Ролей с множителем и так не было."
MULTIPLIER_OUT_OF_RANGE = ("Множитель должен быть от **{min}** до **{max}**, "
                          "получено **{value}**.")
MULTIPLIER_OFF_GRID = ("Множитель **{value}** не попадает в шаг **{step}**. "
                      "Ближайший подходящий: **{nearest}**.")
MULTIPLIER_IS_IGNORED = ("\n⚠️ Роль **{role}** сейчас в списке игнорируемых, "
                         "её участники не получают опыт вообще.")

# how a role with a multiplier is written in the settings field
MULTIPLIER_TARGET = "<@&{role_id}> — **{multiplier}×**"
MULTIPLIER_NO_ROLES = "нет (у всех 1×)"
SETTINGS_CHANNELS_FIELD = "Игнорируемые каналы"
SETTINGS_ROLES_FIELD = "Игнорируемые роли"
SETTINGS_MULTIPLIERS_FIELD = "Роли с множителем опыта"
SETTINGS_EMPTY = "ничего"
SETTINGS_MORE = "…и ещё **{count}**"
SETTINGS_YES = "да"
SETTINGS_NO = "нет"
SETTINGS_PRUNED = "Убрал удалённых: каналов {channels}, ролей {roles}."


# ------------------------------------------------------------------- errors
class LevelConfigError(commands.CommandError):
    """The requested channel or role cannot be used for level settings."""


# --------------------------------------------------------------- one server's
# level settings, read from the database and kept for a short while
class GuildLevelSettings:
    """The level settings of one server.

    A small plain object instead of the database row, so the checks in front of
    every message never have to know how the values were stored. The empty
    defaults are what an unconfigured server gets: announcements stay where the
    XP happened and nothing is ignored.
    """

    __slots__ = ("level_up_channel_id", "level_up_everywhere",
                 "ignored_channels", "ignored_roles", "role_multipliers")

    def __init__(self, level_up_channel_id: int | None = None,
                 level_up_everywhere: bool = False,
                 ignored_channels: frozenset[int] = frozenset(),
                 ignored_roles: frozenset[int] = frozenset(),
                 role_multipliers: dict[int, float] | None = None) -> None:
        self.level_up_channel_id = level_up_channel_id
        self.level_up_everywhere = level_up_everywhere
        self.ignored_channels = frozenset(ignored_channels)
        self.ignored_roles = frozenset(ignored_roles)
        self.role_multipliers = dict(role_multipliers or {})

    def blocks_channel(self, channel_id: int | None) -> bool:
        """True when no XP may be awarded in this channel."""
        return channel_id is not None and channel_id in self.ignored_channels

    def blocks_member(self, member: discord.Member) -> bool:
        """True when one of the roles of `member` is on the ignore list."""
        return bool(self.ignored_roles.intersection(
            role.id for role in member.roles))

    def multiplier_for(self, member: discord.Member) -> float:
        """The XP multiplier of `member`, 1.0 when they hold no special role.

        Only the best of a member's roles counts by default: a member with two
        1.5x roles would otherwise earn 2.25x, and a staff member handing out
        roles could hand out an unbounded bonus by accident. Set
        MULTIPLIER_STACKS to make the bonuses of the roles add up instead.
        """
        values = [self.role_multipliers[role.id]
                  for role in member.roles if role.id in self.role_multipliers]
        if not values:
            return MULTIPLIER_DEFAULT
        if MULTIPLIER_STACKS:
            # 1x per member plus the bonus of each of their roles on top of it.
            # A penalty role has a negative bonus, so several of them could add
            # up past 1x and leave a member earning nothing (or a negative
            # multiplier); the same floor a single role is held to keeps the
            # total at least MULTIPLIER_MIN.
            total = MULTIPLIER_DEFAULT + sum(value - 1.0 for value in values)
            return max(MULTIPLIER_MIN, total)
        return max(values)


# --------------------------------------------------------- level curve maths
def xp_to_next_level(level: int) -> int:
    """XP that has to be earned inside `level` to reach `level` + 1."""
    return 2 * level * level + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    """Total XP needed to reach `level` from zero."""
    return sum(xp_to_next_level(i) for i in range(level))


def level_from_xp(xp: int) -> int:
    """Highest level reachable with `xp` total XP."""
    level = 0
    remaining = xp
    while remaining >= xp_to_next_level(level):
        remaining -= xp_to_next_level(level)
        level += 1
    return level


def progress_bar(current: int, needed: int, length: int = 10) -> str:
    """Text bar such as ``▰▰▰▱▱▱▱▱▱▱`` for `current` out of `needed`."""
    filled = 0 if needed <= 0 else round(current / needed * length)
    filled = max(0, min(length, int(filled)))
    return "▰" * filled + "▱" * (length - filled)


# ------------------------------------------------------- level up rewards
def level_up_reward(level: int) -> int:
    """Coins paid for reaching `level` (level 1 -> 1 coin, level 2 -> 2 coins)."""
    return max(0, level) * LEVEL_UP_COINS_PER_LEVEL


class Levels(commands.Cog):
    """Message XP, voice XP, levels, ranks and the leaderboard."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.log = logging.getLogger(__name__)
        # guild id -> (settings, monotonic time of the read)
        self._settings_cache: dict[int, tuple[GuildLevelSettings, float]] = {}
        self.voice_xp_task: tasks.Loop | None = None
        if VOICE_XP_ENABLED:
            self.voice_xp_task = tasks.loop(seconds=VOICE_XP_INTERVAL)(self.voice_xp_tick)
            self.voice_xp_task.error(self._voice_xp_error)

    async def cog_load(self) -> None:
        if self.voice_xp_task is not None:
            self.voice_xp_task.start()

    async def cog_unload(self) -> None:
        if self.voice_xp_task is not None:
            self.voice_xp_task.cancel()

    async def _voice_xp_error(self, error: BaseException) -> None:
        # the task re-raises after this handler, so the tick itself must not throw
        self.log.error("Voice XP task stopped: %r", error, exc_info=error)

    # ------------------------------------------------------------- settings
    async def settings_for(self, guild_id: int) -> GuildLevelSettings:
        """The settings of a server, taken from the cache while it is fresh."""
        cached = self._settings_cache.get(guild_id)
        if cached is not None and time.monotonic() - cached[1] < SETTINGS_CACHE_TTL:
            return cached[0]
        settings = await self.load_settings(guild_id)
        self._settings_cache[guild_id] = (settings, time.monotonic())
        return settings

    async def load_settings(self, guild_id: int) -> GuildLevelSettings:
        """Read the settings of a server, writing the defaults on first use."""
        await ensure_guild_settings(guild_id, DEFAULT_LEVEL_UP_CHANNEL_ID,
                                    DEFAULT_IGNORED_ROLE_IDS,
                                    DEFAULT_IGNORED_CHANNEL_IDS)
        row = await get_guild_settings(guild_id)
        return GuildLevelSettings(
            level_up_channel_id=row["level_up_channel_id"] if row else None,
            level_up_everywhere=bool(row["level_up_everywhere"]) if row else False,
            ignored_channels=await list_ignored_channels(guild_id),
            ignored_roles=await list_ignored_roles(guild_id),
            role_multipliers=await list_role_multipliers(guild_id),
        )

    def forget_settings(self, guild_id: int) -> None:
        """Drop the cached settings so the next read sees a fresh write."""
        self._settings_cache.pop(guild_id, None)

    # ---------------------------------------------------------- XP multipliers
    @staticmethod
    def xp_for(amount: int, member: discord.Member,
               settings: GuildLevelSettings) -> int:
        """`amount` XP after the multiplier of the roles of `member`.

        Rounded to a whole number of XP (the users table stores integers) and
        never below 1, so even a tiny multiplier still pays out something
        instead of silently swallowing the message.
        """
        multiplier = settings.multiplier_for(member)
        if multiplier == MULTIPLIER_DEFAULT:
            return amount
        return max(1, round(amount * multiplier))

    @staticmethod
    def normalise_multiplier(value: float) -> float:
        """A multiplier snapped to the grid the commands offer.

        Rounding here rather than on the way in keeps a stored multiplier and
        the one `/levels multiplier` reports from ever disagreeing.
        """
        return round(round(value / MULTIPLIER_STEP) * MULTIPLIER_STEP,
                     MULTIPLIER_DECIMALS)

    # ------------------------------------------------------------ XP gaining
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        settings = await self.settings_for(message.guild.id)
        if settings.blocks_channel(message.channel.id):
            return
        if settings.blocks_member(message.author):
            return

        amount = self.xp_for(random.randint(XP_MIN, XP_MAX), message.author,
                             settings)
        awarded, xp, stored_level = await add_xp_with_cooldown(
            message.guild.id, message.author.id, amount, XP_COOLDOWN)
        if not awarded:
            return

        new_level = level_from_xp(xp)
        if new_level <= stored_level:
            return
        await set_level(message.guild.id, message.author.id, new_level)
        await self.announce_level_up(message.guild, message.author, new_level,
                                     message.channel, settings)

    async def announce_level_up(self, guild: discord.Guild, member: discord.Member,
                                level: int,
                                channel: Messageable | None = None,
                                settings: GuildLevelSettings | None = None) -> None:
        """Pay the level reward and post the level up message, if any.

        `channel` is where the XP was earned; the settings decide whether the
        message stays there or goes to the configured announcement channel.
        """
        reward = level_up_reward(level)
        if reward:
            await add_balance(guild.id, member.id, reward)
        if settings is None:
            settings = await self.settings_for(guild.id)
        target = self.level_up_channel(guild, settings, channel)
        if target is None:
            return
        message = LEVEL_UP_MESSAGE.format(mention=member.mention, level=level)
        if reward:
            # the reward is positive, so the + is added here and not in coins()
            message += "\n" + LEVEL_UP_REWARD_MESSAGE.format(
                reward=f"+{coins(reward)}")
        try:
            await target.send(message)
        except discord.HTTPException:
            pass  # missing permissions or deleted channel: XP is already saved

    @staticmethod
    def level_up_channel(guild: discord.Guild, settings: GuildLevelSettings,
                         earned_in: Messageable | None = None) -> Messageable | None:
        """Where a level up message has to go.

        Without the "everywhere" switch only the level ups that have no channel
        of their own (the voice ones) are moved, so chatting keeps its own
        announcements. The configured channel wins over the system channel, and
        an id of a channel that is gone simply falls back.
        """
        if earned_in is not None and not settings.level_up_everywhere:
            return earned_in
        if settings.level_up_channel_id is not None:
            channel = guild.get_channel(settings.level_up_channel_id)
            if isinstance(channel, Messageable):
                return channel
        return guild.system_channel

    # --------------------------------------------------------------- voice XP
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        """Start the voice timer whenever a member joins or switches channel."""
        if not VOICE_XP_ENABLED or member.bot or after.channel is None:
            return
        if before.channel == after.channel:
            return  # mute/deafen toggles must not restart the minute
        settings = await self.settings_for(member.guild.id)
        if not self.voice_channel_allowed(member.guild, after.channel, settings):
            return
        await mark_voice_join(member.guild.id, member.id)

    async def voice_xp_tick(self) -> None:
        """Hand out one interval of voice XP for every guild."""
        if not self.bot.is_ready():
            return  # ignored while the gateway/cache is not ready yet
        for guild in list(self.bot.guilds):
            try:
                await self.award_voice_xp(guild)
            except Exception:  # one broken guild must not stop the whole loop
                self.log.exception("Voice XP tick failed for guild %s", guild.id)

    async def award_voice_xp(self, guild: discord.Guild) -> None:
        """Pay every eligible member of every voice channel of `guild`."""
        settings = await self.settings_for(guild.id)
        for channel in guild.voice_channels:
            if not self.voice_channel_allowed(guild, channel, settings):
                continue
            members = [m for m in channel.members
                       if not m.bot and self.voice_eligible(m, settings)]
            if VOICE_XP_REQUIRE_OTHER_MEMBERS and len(members) < 2:
                continue
            for member in members:
                amount = self.xp_for(
                    random.randint(VOICE_XP_MIN, VOICE_XP_MAX),
                    member, settings)
                awarded, xp, stored_level = await add_xp_with_cooldown(
                    guild.id, member.id, amount, VOICE_XP_INTERVAL,
                    column="last_voice_xp")
                if not awarded:
                    continue  # joined less than one interval ago, or paid already
                new_level = level_from_xp(xp)
                if new_level > stored_level:
                    await set_level(guild.id, member.id, new_level)
                    await self.announce_level_up(guild, member, new_level,
                                                 settings=settings)

    @staticmethod
    def voice_channel_allowed(guild: discord.Guild,
                              channel: discord.VoiceChannel,
                              settings: GuildLevelSettings) -> bool:
        if settings.blocks_channel(channel.id):
            return False
        if VOICE_XP_IGNORE_AFK_CHANNEL and channel == guild.afk_channel:
            return False
        return True

    @staticmethod
    def voice_eligible(member: discord.Member,
                       settings: GuildLevelSettings) -> bool:
        """True while the member sits in voice and counts as unmuted."""
        state = member.voice
        if state is None or state.channel is None or state.afk:
            return False
        if settings.blocks_member(member):
            return False
        if VOICE_XP_IGNORE_MUTED and (state.mute or state.deaf
                                      or state.self_mute or state.self_deaf):
            return False
        return True

    # -------------------------------------------------------------- commands
    @app_commands.command(
        name="rank", description="Show the level and XP of a member")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction,
                   member: discord.Member | None = None) -> None:
        """Show the level, XP and leaderboard position of a member.

        Leave `member` empty to see your own rank.
        """
        member = member or interaction.user
        row = await get_user(interaction.guild.id, member.id)
        xp = row["xp"]
        level = level_from_xp(xp)
        current = xp - total_xp_for_level(level)
        needed = xp_to_next_level(level)
        position = await get_rank_position(interaction.guild.id, member.id)

        embed = discord.Embed(color=discord.Color.blurple())
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.add_field(name="Rank", value=f"#{position}")
        embed.add_field(name="Level", value=str(level))
        embed.add_field(name="XP", value=f"{xp} total")
        embed.add_field(
            name="Progress",
            value=f"{progress_bar(current, needed)} {current}/{needed} XP",
            inline=False,
        )
        embed.set_footer(text=f"{needed - current} XP to level {level + 1}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="leaderboard", description="Show the XP leaderboard of this server")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        """Show the ten members with the most XP."""
        rows = await get_leaderboard(interaction.guild.id, 10)
        if not rows:
            await interaction.response.send_message(EMPTY_LEADERBOARD)
            return

        lines = []
        for index, row in enumerate(rows, start=1):
            member = interaction.guild.get_member(row["user_id"])
            name = member.display_name if member else f"Unknown member ({row['user_id']})"
            marker = MEDALS.get(index, f"**{index}.**")
            lines.append(f"{marker} {name} — level {level_from_xp(row['xp'])}, {row['xp']} XP")

        embed = discord.Embed(
            title=LEADERBOARD_TITLE.format(guild=interaction.guild.name),
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------- staff only commands
    levels = app_commands.Group(
        name="levels", description="Manage the XP and levels of this server",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True))

    @levels.command(name="addxp", description="Add XP to a member")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levels_addxp(self, interaction: discord.Interaction,
                           member: discord.Member, amount: int) -> None:
        """Add `amount` XP to `member`. A negative amount removes XP."""
        row = await get_user(interaction.guild.id, member.id)
        new_xp = max(0, row["xp"] + amount)
        new_level = level_from_xp(new_xp)
        await set_xp(interaction.guild.id, member.id, new_xp, new_level)
        await interaction.response.send_message(
            f"{member.mention} now has **{new_xp} XP** (level **{new_level}**).")

    @levels.command(name="setxp",
                    description="Overwrite the total XP of a member")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levels_setxp(self, interaction: discord.Interaction,
                           member: discord.Member, amount: int) -> None:
        """Set the total XP of `member` to `amount`."""
        new_xp = max(0, amount)
        new_level = level_from_xp(new_xp)
        await set_xp(interaction.guild.id, member.id, new_xp, new_level)
        await interaction.response.send_message(
            f"{member.mention} is now at **{new_xp} XP** (level **{new_level}**).")

    @levels.command(name="resetxp",
                    description="Reset a member back to level 0")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levels_resetxp(self, interaction: discord.Interaction,
                             member: discord.Member) -> None:
        """Delete every XP of `member`."""
        await reset_xp(interaction.guild.id, member.id)
        await interaction.response.send_message(
            f"{member.mention} is back to **level 0**.")

    # ------------------------------------------------------- level settings
    @levels.command(name="levelup",
                    description="Choose where the level up messages are posted")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Channel for the level up messages, empty to reset it",
        everywhere="Post every level up there, not only the voice ones",
    )
    async def levels_levelup(self, interaction: discord.Interaction,
                             channel: discord.TextChannel | discord.Thread
                             | None = None,
                             everywhere: bool = False) -> None:
        """Store `channel` as the level up channel, empty resets the setting."""
        if channel is None:
            await set_level_up_channel(interaction.guild.id, None)
            self.forget_settings(interaction.guild.id)
            text = LEVEL_UP_CHANNEL_CLEARED
            if everywhere:
                # the flag only means something together with a channel
                text += LEVEL_UP_EVERYWHERE_NEEDS_CHANNEL
            await interaction.response.send_message(text, ephemeral=True)
            return

        self.check_writable_channel(interaction, channel)
        await set_level_up_channel(interaction.guild.id, channel.id, everywhere)
        self.forget_settings(interaction.guild.id)
        text = LEVEL_UP_CHANNEL_SET.format(channel=channel.mention)
        text += (LEVEL_UP_CHANNEL_EVERYWHERE_ON if everywhere
                 else LEVEL_UP_CHANNEL_EVERYWHERE_OFF)
        settings = await self.settings_for(interaction.guild.id)
        if settings.blocks_channel(channel.id):
            text += LEVEL_UP_CHANNEL_IGNORED_WARNING.format(
                channel=channel.mention)
        await interaction.response.send_message(text, ephemeral=True)

    @levels.command(name="ignorechannel",
                    description="Stop XP from being earned in a channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Text, voice or thread channel to mute")
    async def levels_ignorechannel(
            self, interaction: discord.Interaction,
            channel: discord.TextChannel | discord.VoiceChannel
            | discord.Thread) -> None:
        """Put `channel` on the ignore list so nothing earns XP there."""
        added = await add_ignored_channel(interaction.guild.id, channel.id)
        self.forget_settings(interaction.guild.id)
        settings = await self.settings_for(interaction.guild.id)
        name = channel.mention
        if not added:
            await interaction.response.send_message(
                CHANNEL_ALREADY_IGNORED.format(channel=name), ephemeral=True)
            return
        text = CHANNEL_IGNORED.format(channel=name)
        if channel.id == settings.level_up_channel_id:
            text += CHANNEL_IS_LEVEL_UP.format(channel=name)
        await interaction.response.send_message(text, ephemeral=True)

    @levels.command(name="unignorechannel",
                    description="Let a channel earn XP again")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Channel to take off the ignore list")
    async def levels_unignorechannel(
            self, interaction: discord.Interaction,
            channel: discord.TextChannel | discord.VoiceChannel
            | discord.Thread) -> None:
        """Take `channel` off the ignore list."""
        removed = await remove_ignored_channel(interaction.guild.id, channel.id)
        self.forget_settings(interaction.guild.id)
        text = (CHANNEL_UNIGNORED if removed else CHANNEL_NOT_IGNORED)
        await interaction.response.send_message(
            text.format(channel=channel.mention), ephemeral=True)

    @levels.command(name="ignorerole",
                    description="Stop XP from being earned by a role")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(role="Role whose members should stop earning XP")
    async def levels_ignorerole(self, interaction: discord.Interaction,
                                role: discord.Role) -> None:
        """Put `role` on the ignore list so its members earn nothing."""
        if role.is_default():
            raise LevelConfigError(ROLE_EVERYONE)
        added = await add_ignored_role(interaction.guild.id, role.id)
        self.forget_settings(interaction.guild.id)
        text = (ROLE_IGNORED if added else ROLE_ALREADY_IGNORED)
        await interaction.response.send_message(
            text.format(role=role.name), ephemeral=True)

    @levels.command(name="unignorerole",
                    description="Let a role earn XP again")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(role="Role to take off the ignore list")
    async def levels_unignorerole(self, interaction: discord.Interaction,
                                  role: discord.Role) -> None:
        """Take `role` off the ignore list."""
        removed = await remove_ignored_role(interaction.guild.id, role.id)
        self.forget_settings(interaction.guild.id)
        text = (ROLE_UNIGNORED if removed else ROLE_NOT_IGNORED)
        await interaction.response.send_message(
            text.format(role=role.name), ephemeral=True)

    @levels.command(name="multiplier",
                    description="Give a role a multiplied amount of XP")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(role="Role that should earn a different amount of XP",
                           multiplier="How much XP it earns, e.g. 1.25 for 1.25x")
    async def levels_multiplier(self, interaction: discord.Interaction,
                                role: discord.Role, multiplier: float) -> None:
        """Make the holders of `role` earn `multiplier` times the XP."""
        value = await self.check_multiplier(interaction, multiplier)
        if value is None:
            return
        if role.is_default():
            raise LevelConfigError(ROLE_MULTIPLIER_EVERYONE)

        changed = await set_role_multiplier(interaction.guild.id, role.id, value)
        self.forget_settings(interaction.guild.id)
        text = MULTIPLIER_SET if changed else MULTIPLIER_UNCHANGED
        text = text.format(role=role.name, multiplier=self.show_multiplier(value))
        if role.id in await list_ignored_roles(interaction.guild.id):
            # the ignore list is checked first, so a booster nobody can reach
            # would look broken
            text += MULTIPLIER_IS_IGNORED.format(role=role.name)
        await interaction.response.send_message(text, ephemeral=True)

    @levels.command(name="clearmultiplier",
                    description="Take the XP multiplier off a role")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(role="Role to put back on the normal amount of XP")
    async def levels_clearmultiplier(self, interaction: discord.Interaction,
                                     role: discord.Role) -> None:
        """Put the holders of `role` back on 1x."""
        removed = await remove_role_multiplier(interaction.guild.id, role.id)
        self.forget_settings(interaction.guild.id)
        text = (MULTIPLIER_CLEARED if removed else MULTIPLIER_NOT_CLEARED)
        await interaction.response.send_message(
            text.format(role=role.name), ephemeral=True)

    @levels.command(name="clearmultipliers",
                    description="Put every role back on the normal amount of XP")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levels_clearmultipliers(self,
                                      interaction: discord.Interaction) -> None:
        """Drop every multiplier of this server at once."""
        removed = await clear_role_multipliers(interaction.guild.id)
        self.forget_settings(interaction.guild.id)
        text = (MULTIPLIERS_ALL_CLEARED.format(count=removed) if removed
                else MULTIPLIERS_NOTHING_TO_CLEAR)
        await interaction.response.send_message(text, ephemeral=True)

    async def check_multiplier(self, interaction: discord.Interaction,
                               value: float) -> float | None:
        """The multiplier to store, or None once the answer was sent.

        Refusing here rather than in the database keeps one bad number from
        turning into a settings row nobody can fix from Discord.
        """
        step = self.show_multiplier(MULTIPLIER_STEP)
        if not MULTIPLIER_MIN <= value <= MULTIPLIER_MAX:
            await interaction.response.send_message(MULTIPLIER_OUT_OF_RANGE.format(
                min=MULTIPLIER_MIN, max=MULTIPLIER_MAX,
                value=self.show_multiplier(value)), ephemeral=True)
            return None
        snapped = self.normalise_multiplier(value)
        if snapped != value:
            await interaction.response.send_message(MULTIPLIER_OFF_GRID.format(
                value=self.show_multiplier(value), step=step,
                nearest=self.show_multiplier(snapped)), ephemeral=True)
            return None
        return snapped

    @staticmethod
    def show_multiplier(value: float) -> str:
        """A multiplier as it is shown to staff: 1.25 and not 1.2500000001."""
        text = f"{round(value, MULTIPLIER_DECIMALS):.{MULTIPLIER_DECIMALS}f}"
        return text.rstrip("0").rstrip(".") or "0"



    @levels.command(name="settings",
                    description="Show the level settings of this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levels_settings(self, interaction: discord.Interaction) -> None:
        """Show the announcement channel and both ignore lists of this server."""
        # answer straight from the database
        self.forget_settings(interaction.guild.id)
        settings = await self.settings_for(interaction.guild.id)
        channels, roles = await self.prune_missing(interaction.guild, settings)
        if channels or roles:
            self.forget_settings(interaction.guild.id)
            settings = await self.settings_for(interaction.guild.id)

        embed = discord.Embed(
            title=SETTINGS_TITLE.format(guild=interaction.guild.name),
            color=discord.Color.blurple())
        embed.add_field(name=SETTINGS_CHANNEL_FIELD,
                        value=await self.channel_field(
                            interaction.guild, settings.level_up_channel_id),
                        inline=False)
        embed.add_field(name=SETTINGS_EVERYWHERE_FIELD,
                        value=SETTINGS_YES if settings.level_up_everywhere
                        else SETTINGS_NO)
        embed.add_field(name=SETTINGS_CHANNELS_FIELD,
                        value=self.render_list(settings.ignored_channels,
                                               CHANNEL_TARGET),
                        inline=False)
        embed.add_field(name=SETTINGS_ROLES_FIELD,
                        value=self.render_list(settings.ignored_roles,
                                               ROLE_TARGET),
                        inline=False)
        embed.add_field(name=SETTINGS_MULTIPLIERS_FIELD,
                        value=self.render_multipliers(settings.role_multipliers),
                        inline=False)
        if channels or roles:
            embed.set_footer(text=SETTINGS_PRUNED.format(channels=channels,
                                                         roles=roles))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------------------------------------------------- view helpers
    @staticmethod
    def render_list(target_ids: frozenset[int], template: str,
                    limit: int = LIST_FIELD_LIMIT) -> str:
        """One embed field listing `target_ids`, trimmed to `limit` entries."""
        if not target_ids:
            return SETTINGS_EMPTY
        lines = [template.format(target_id=target_id)
                 for target_id in sorted(target_ids)[:limit]]
        rest = len(target_ids) - limit
        if rest > 0:
            lines.append(SETTINGS_MORE.format(count=rest))
        return "\n".join(lines)

    def render_multipliers(self, multipliers: dict[int, float],
                           limit: int = MULTIPLIER_LIST_LIMIT) -> str:
        """One embed field listing the roles with an XP multiplier.

        Sorted by multiplier, best first: the roles worth looking at are the
        ones on top, and the order stays the same between two runs.
        """
        if not multipliers:
            return MULTIPLIER_NO_ROLES
        ordered = sorted(multipliers.items(), key=lambda item: (-item[1],
                                                                 item[0]))
        lines = [MULTIPLIER_TARGET.format(
            role_id=role_id, multiplier=self.show_multiplier(value))
            for role_id, value in ordered[:limit]]
        rest = len(ordered) - limit
        if rest > 0:
            lines.append(SETTINGS_MORE.format(count=rest))
        return "\n".join(lines)

    async def channel_field(self, guild: discord.Guild,
                            channel_id: int | None) -> str:
        """Name the stored announcement channel, or say that there is none."""
        if channel_id is None:
            return LEVEL_UP_NONE
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.HTTPException:
                # the channel is gone: stop pointing at it instead of showing a
                # dead id forever
                await clear_level_up_channel(guild.id, channel_id)
                self.forget_settings(guild.id)
                return MISSING_CHANNEL.format(channel_id=channel_id)
        return channel.mention

    @staticmethod
    def check_writable_channel(interaction: discord.Interaction,
                               channel) -> None:
        """Refuse a channel the bot cannot post in before storing its id."""
        me = interaction.guild.me
        if me is None:
            return  # nothing to compare against while the member cache is cold
        perms = channel.permissions_for(me)
        missing = []
        if not perms.view_channel:
            missing.append("*View Channel*")
        if isinstance(channel, discord.Thread):
            if not perms.send_messages_in_threads:
                missing.append("*Send Messages in Threads*")
        elif not perms.send_messages:
            missing.append("*Send Messages*")
        if missing:
            raise LevelConfigError(LEVEL_UP_CHANNEL_NO_ACCESS.format(
                channel=channel.mention, perms=", ".join(missing)))

    @staticmethod
    async def channel_exists(guild: discord.Guild, channel_id: int) -> bool:
        """True when the id belongs to a channel of this server."""
        if guild.get_channel(channel_id) is not None:
            return True
        try:
            await guild.fetch_channel(channel_id)  # fills the cache on success
        except discord.HTTPException:
            return False
        return True

    async def prune_missing(self, guild: discord.Guild,
                            settings: GuildLevelSettings) -> tuple[int, int]:
        """Drop ids of channels and roles this server does not have any more."""
        channels = 0
        for channel_id in sorted(settings.ignored_channels):
            if not await self.channel_exists(guild, channel_id):
                await remove_ignored_channel(guild.id, channel_id)
                channels += 1
        roles = 0
        for role_id in sorted(settings.ignored_roles):
            if guild.get_role(role_id) is None:
                await remove_ignored_role(guild.id, role_id)
                roles += 1
        for role_id in sorted(settings.role_multipliers):
            if guild.get_role(role_id) is None:
                await remove_role_multiplier(guild.id, role_id)
                roles += 1
        if channels or roles:
            self.log.info("Dropped %s channel(s) and %s role(s) that no longer "
                          "exist from the ignore lists of guild %s",
                          channels, roles, guild.id)
        return channels, roles

    # ------------------------------------------------------------- clean ups
    @commands.Cog.listener()
    async def on_guild_channel_delete(self,
                                      channel: discord.abc.GuildChannel) -> None:
        """Forget a deleted channel instead of leaving a dangling id behind."""
        removed = await remove_ignored_channel(channel.guild.id, channel.id)
        cleared = await clear_level_up_channel(channel.guild.id, channel.id)
        if removed or cleared:
            self.forget_settings(channel.guild.id)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        """Same for a deleted thread, which is not reported as a channel."""
        if await remove_ignored_channel(thread.guild.id, thread.id):
            self.forget_settings(thread.guild.id)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        """Forget a deleted role from the ignore list and the multipliers."""
        ignored = await remove_ignored_role(role.guild.id, role.id)
        multiplied = await remove_role_multiplier(role.guild.id, role.id)
        if ignored or multiplied:
            self.forget_settings(role.guild.id)

    # ---------------------------------------------------------------- errors
    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError) -> None:
        """Handles the application command errors the tree forwards to the cog."""
        original: BaseException = error
        for _ in range(5):
            inner = getattr(original, "original", None)
            if inner is None:
                break
            original = inner

        if isinstance(original, (app_commands.CheckFailure, commands.MissingPermissions)):
            text = PERMISSION_ERROR
        elif isinstance(original, commands.MemberNotFound):
            text = MEMBER_NOT_FOUND_ERROR
        elif isinstance(original, commands.CommandError):
            text = str(original)
        else:
            raise error

        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Levels(bot))

