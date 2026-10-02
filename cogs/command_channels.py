"""Command channels cog: the bot only answers commands in chosen channels.

Every server keeps an allow list of channels in the database (see db.py). A
command that arrives anywhere else is refused with a message only the person
who typed it can see, so the channel stays quiet. Members holding the bypass
permission (Administrator by default), the owner of the bot and the
`/botchannels` commands themselves always get through.

The rule is enforced in front of both kinds of commands:
- slash commands through the global check of the slash tree;
- the `-` prefix commands through a global check of the bot.

An empty allow list means the rule is off and the bot answers everywhere, so a
server that never chose a channel keeps working exactly as before.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from db import (
    add_command_channel,
    clear_command_channels,
    list_command_channels,
    remove_command_channel,
)

# ------------------------------------------------------------------ settings
MANAGEMENT_GROUP = "botchannels"     # never gated, it configures the rule
BYPASS_PERMISSION = "administrator"  # members holding it skip the rule
LIST_FIELD_LIMIT = 9                 # channels one field of /botchannels lists
ERROR_DELETE_AFTER = 10.0            # seconds a prefix warning stays visible

# how a channel of the allow list is written in a message
CHANNEL_TARGET = "<#{target_id}>"

# user facing texts
GATE_TITLE = "💬 Каналы для команд **{guild}**"
GATE_FIELD = "Каналы, где бот отвечает"
GATE_OFF = "✅ Правило выключено — бот отвечает на команды во всех каналах."
GATE_OFF_FIELD = "правило выключено, бот отвечает везде"
SETTINGS_EMPTY = "ничего"
SETTINGS_MORE = "…и ещё **{count}**"

CHANNEL_ADDED = "✅ {channel} добавлен: там бот отвечает на команды."
CHANNEL_ALREADY_ADDED = "{channel} уже есть в списке."
CHANNEL_REMOVED = "🔇 {channel} убран: там бот больше не отвечает."
CHANNEL_NOT_REMOVED = "{channel} и так не было в списке."
CHANNELS_CLEARED = ("🗑️ Список очищен: бот снова отвечает везде."
                     " Каналов убрано: **{count}**.")
CHANNELS_NOTHING_TO_CLEAR = "Список и так был пуст."

WRONG_CHANNEL = "🚫 Здесь меня вызывать нельзя.\nЭти команды работают только в: {channels}"
DM_SENTINEL = "Ответил тебе в личку, чтобы не засорять канал."


# ------------------------------------------------------------------- errors
class ChannelNotAllowed(commands.CommandError):
    """A command was used in a channel the bot does not answer in.

    Carries the text that is shown privately to the author. Raised by the
    global check of the prefix commands and caught by `on_command_error`.
    """


# ------------------------------------------------------- one server's command
# channels, read from the database
class CommandChannelSettings:
    """The channels of one server the bot is allowed to answer in.

    An empty allow list means the rule is off: every channel is then allowed.
    """

    __slots__ = ("allowed_channels",)

    def __init__(self, allowed_channels: frozenset[int] = frozenset()) -> None:
        self.allowed_channels = frozenset(allowed_channels)

    @property
    def enabled(self) -> bool:
        """True once the server picked at least one channel."""
        return bool(self.allowed_channels)

    def allows(self, channel_id: int | None,
               parent_id: int | None = None) -> bool:
        """True when the bot may answer in this channel.

        A thread counts as its parent channel, so a thread of an allowed
        channel is allowed as well.
        """
        if not self.enabled:
            return True
        if channel_id is not None and channel_id in self.allowed_channels:
            return True
        return parent_id is not None and parent_id in self.allowed_channels


def root_command_name(interaction: discord.Interaction) -> str | None:
    """The top level slash command of an interaction, e.g. `botchannels`."""
    command = interaction.command
    if command is None:
        return None
    parent = getattr(command, "root_parent", None)
    return parent.name if parent is not None else command.name


class CommandChannels(commands.Cog):
    """Keeps the bot quiet outside the channels a server chose."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.log = logging.getLogger(__name__)

    async def cog_load(self) -> None:
        """Installs the global check in front of every command.

        Both are taken off again in `cog_unload`, so a reload leaves the bot
        exactly as it found it.
        """
        self.bot.tree.interaction_check = self.interaction_check
        self.bot.add_check(self.text_command_check)

    async def cog_unload(self) -> None:
        self.bot.remove_check(self.text_command_check)
        # drops the instance attribute, the class default takes over again
        self.bot.tree.__dict__.pop("interaction_check", None)

    # ------------------------------------------------------------- settings
    async def load_settings(self, guild_id: int) -> CommandChannelSettings:
        """Read the allow list of a server.

        Unlike the XP path in front of every message this runs once per
        command, so it reads the database instead of keeping a cache.
        """
        return CommandChannelSettings(await list_command_channels(guild_id))

    # --------------------------------------------------------- the decision
    async def is_owner(self, user: discord.abc.User) -> bool:
        """True for the owner of the bot, False when the lookup failed.

        `Bot.is_owner()` asks Discord for the application info the first time
        it is asked and does not handle a failure itself. Since this runs in
        front of every command, such a hiccup must count as "not the owner"
        instead of breaking every command of the bot.
        """
        try:
            return await self.bot.is_owner(user)
        except discord.HTTPException:
            self.log.warning("Could not read the owner of the bot")
            return False

    async def bypasses(self, user: discord.abc.User,
                       guild: discord.Guild | None,
                       command_name: str | None) -> bool:
        """True for the users and the commands that skip the rule."""
        if guild is None:
            return True  # a direct message is not a channel of the server
        if await self.is_owner(user):
            return True  # the owner has to stay able to sync and restart
        if command_name == MANAGEMENT_GROUP:
            return True  # /botchannels configures the rule itself
        permissions = getattr(user, "guild_permissions", None)
        if permissions is None:
            return False
        return bool(getattr(permissions, BYPASS_PERMISSION, False))

    async def channel_ids(self, channel_id: int | None
                          ) -> tuple[int | None, int | None]:
        """The channel id and, for a thread, the id of its parent channel.

        A thread of an allowed channel is allowed too, so the parent has to be
        known as well. A thread that is not in the cache is fetched once.
        """
        if channel_id is None:
            return None, None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                channel = None  # gone: it cannot be on the allow list
        if isinstance(channel, discord.Thread):
            return channel.id, channel.parent_id
        return channel_id, None

    def allowed_channels_text(self, settings: CommandChannelSettings,
                              limit: int = LIST_FIELD_LIMIT) -> str:
        """The allow list as one block of channel mentions."""
        ids = sorted(settings.allowed_channels)
        if not ids:
            return SETTINGS_EMPTY
        lines = [CHANNEL_TARGET.format(target_id=target_id)
                 for target_id in ids[:limit]]
        rest = len(ids) - limit
        if rest > 0:
            lines.append(SETTINGS_MORE.format(count=rest))
        return "\n".join(lines)

    def warning_text(self, settings: CommandChannelSettings,
                     by_dm: bool = False) -> str:
        """The private answer to a command from a channel that is not allowed."""
        text = WRONG_CHANNEL.format(
            channels=self.allowed_channels_text(settings))
        if by_dm:
            text += "\n" + DM_SENTINEL
        return text

    # ------------------------------------------------------- the two checks
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The global check of the slash tree.

        False means the command is not run. The refusal is sent from here
        instead of being raised on purpose: raising would hand the error to the
        error handler of whichever cog owns the command, and every one of them
        would answer with its own text.
        """
        guild = interaction.guild
        if guild is None:
            return True
        if await self.bypasses(interaction.user, guild,
                               root_command_name(interaction)):
            return True
        settings = await self.load_settings(guild.id)
        if not settings.enabled:
            return True
        channel_id, parent_id = await self.channel_ids(interaction.channel_id)
        if settings.allows(channel_id, parent_id):
            return True

        text = self.warning_text(settings)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            self.log.warning("Could not refuse a command in %s of guild %s",
                             interaction.channel_id, guild.id)
        return False

    async def text_command_check(self, ctx: commands.Context) -> bool:
        """The global check of the `-` prefix commands.

        Raises instead of answering straight away, so the message is built with
        the channel list and sent by `on_command_error`.
        """
        guild = ctx.guild
        if guild is None:
            return True
        name = ctx.command.qualified_name.split()[0] if ctx.command else None
        if await self.bypasses(ctx.author, guild, name):
            return True
        settings = await self.load_settings(guild.id)
        if not settings.enabled:
            return True
        channel_id, parent_id = await self.channel_ids(ctx.channel.id)
        if settings.allows(channel_id, parent_id):
            return True
        raise ChannelNotAllowed(self.warning_text(settings, by_dm=True))

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context,
                               error: commands.CommandError) -> None:
        """Answers a refused prefix command where only the author sees it."""
        if not isinstance(error, ChannelNotAllowed):
            # not ours, and this listener replaces the default one that logged
            # it, so it must not disappear silently
            self.log.error("Command %s failed: %r", ctx.command, error,
                           exc_info=error)
            return
        await self.send_private(ctx, str(error))

    async def send_private(self, ctx: commands.Context, text: str) -> None:
        """Send `text` to the author alone.

        A prefix command cannot be ephemeral, so the warning goes as a direct
        message. With the DMs closed it is posted in the channel and removed
        again after a few seconds.
        """
        try:
            await ctx.author.send(text)
        except discord.Forbidden:
            try:
                await ctx.send(text, delete_after=ERROR_DELETE_AFTER)
            except discord.HTTPException:
                pass
        except discord.HTTPException:
            pass

    # ---------------------------------------------------- staff only commands
    botchannels = app_commands.Group(
        name=MANAGEMENT_GROUP, description="Where the bot may answer commands",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True))

    @botchannels.command(name="add",
                         description="Let the bot answer commands in a channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Text channel or thread to add")
    async def botchannels_add(
            self, interaction: discord.Interaction,
            channel: discord.TextChannel | discord.Thread) -> None:
        """Put `channel` on the allow list."""
        added = await add_command_channel(interaction.guild.id, channel.id)
        text = CHANNEL_ADDED if added else CHANNEL_ALREADY_ADDED
        await interaction.response.send_message(
            text.format(channel=channel.mention), ephemeral=True)

    @botchannels.command(name="remove",
                         description="Stop answering commands in a channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Text channel or thread to take off the list")
    async def botchannels_remove(
            self, interaction: discord.Interaction,
            channel: discord.TextChannel | discord.Thread) -> None:
        """Take `channel` off the allow list."""
        removed = await remove_command_channel(interaction.guild.id, channel.id)
        text = CHANNEL_REMOVED if removed else CHANNEL_NOT_REMOVED
        await interaction.response.send_message(
            text.format(channel=channel.mention), ephemeral=True)

    @botchannels.command(name="list",
                         description="Show where the bot answers commands")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def botchannels_list(self, interaction: discord.Interaction) -> None:
        """Show the allow list, or that the rule is off."""
        settings = await self.load_settings(interaction.guild.id)
        value = (self.allowed_channels_text(settings) if settings.enabled
                 else GATE_OFF_FIELD)
        embed = discord.Embed(
            title=GATE_TITLE.format(guild=interaction.guild.name),
            color=discord.Color.blurple())
        embed.add_field(name=GATE_FIELD, value=value, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @botchannels.command(name="clear",
                         description="Answer commands in every channel again")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def botchannels_clear(self, interaction: discord.Interaction) -> None:
        """Empty the allow list, which turns the rule off again."""
        removed = await clear_command_channels(interaction.guild.id)
        text = (CHANNELS_CLEARED.format(count=removed) if removed
                else CHANNELS_NOTHING_TO_CLEAR)
        await interaction.response.send_message(text, ephemeral=True)

    # ------------------------------------------------------------- clean ups
    @commands.Cog.listener()
    async def on_guild_channel_delete(
            self, channel: discord.abc.GuildChannel) -> None:
        """Forget a deleted channel instead of leaving a dangling id behind."""
        await remove_command_channel(channel.guild.id, channel.id)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        """Same for a deleted thread, which is not reported as a channel."""
        await remove_command_channel(thread.guild.id, thread.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CommandChannels(bot))
