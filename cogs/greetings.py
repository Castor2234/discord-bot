"""Greetings cog: a welcome for new members and a goodbye for the ones that leave.

Both messages go to one channel of the server, chosen by the staff with
`/greetings channel`. Each of the two events has its own switch, so a server
can welcome people without announcing every departure.

A server that never picked a channel is left alone: the cog sends nothing at
all until `/greetings channel` is used, and it never falls back to the system
channel on its own. A deleted channel is forgotten instead of leaving a dangling
id behind, exactly like the level up channel of the levels cog.

Bots are skipped by default: a server that adds a dozen integrations would
otherwise be greeted and bid farewell to on every deploy.
"""

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.abc import Messageable
from discord.ext import commands

from db import (
    clear_greeting_channel,
    get_guild_settings,
    set_greeting_channel,
    set_greeting_switches,
)

# ------------------------------------------------------------------ settings
GREET_BOTS = False              # bots are skipped unless this is turned on

# user facing texts
MEMBER_COUNT_FIELD = "Members"
ACCOUNT_BORN_FIELD = "Account created"
ACCOUNT_AGE_FIELD = "Account age"
ACCOUNT_AGE_DAYS = "{days} day(s) old"
DATE_FORMAT = "%Y-%m-%d"

WELCOME_TITLE = "\U0001F44B Welcome to **{guild}**, {member}!"
FAREWELL_TITLE = "\U0001F44B **{member}** just left **{guild}**."

PREVIEW_JOIN = "_(preview of the welcome message)_"
PREVIEW_LEAVE = "_(preview of the goodbye message)_"

CHANNEL_SET = "\u2705 Greetings go to {channel}."
CHANNEL_CLEARED = ("\U0001F5D1 Greeting channel forgotten: the bot greets "
                   "nobody any more.")
CHANNEL_NEEDS_CHANNEL = "nothing greeted yet, use `/greetings channel`"
CHANNEL_NOT_SET = ("\u26A0\uFE0F No greeting channel yet, use "
                   "`/greetings channel` first.")
SWITCH_JOIN_ON = "\u2705 New members are greeted in {channel}."
SWITCH_JOIN_OFF = "\U0001F507 New members are not greeted any more."
SWITCH_LEAVE_ON = "\u2705 Leaving members are said goodbye to in {channel}."
SWITCH_LEAVE_OFF = "\U0001F515 Leaving members are not said goodbye to any more."
SWITCH_OFF_NO_CHANNEL = " \u26A0\uFE0F No channel is set, so nothing is posted."
SWITCH_ON_NO_CHANNEL = ("\u2705 The switch is on, but no channel is set yet: "
                        "nothing is posted until `/greetings channel`.")
MISSING_CHANNEL = "<#{channel_id}> (channel deleted)"
PREVIEW_SENT = "\u2705 Preview posted to {channel}."

SETTINGS_TITLE = "\U0001F44B Greetings of **{guild}**"
SETTINGS_CHANNEL_FIELD = "Greeting channel"
SETTINGS_JOIN_FIELD = "New members"
SETTINGS_LEAVE_FIELD = "Leaving members"
SETTINGS_YES = "\u2705 greeted"
SETTINGS_NO = "—"

PERMISSION_ERROR = "You need **Manage Server** rights for this command."
CHANNEL_NO_ACCESS = "I cannot post in {channel}, I am missing {perms} there."


# ------------------------------------------------------------------- errors
class GreetingConfigError(commands.CommandError):
    """The requested channel cannot be used for the greetings."""


# ------------------------------------------------------- one server's greeting
# settings, read from the database
class GreetingSettings:
    """The greeting settings of one server.

    A small plain object instead of the database row, so the listeners never
    have to know how the values were stored. The empty defaults are what an
    unconfigured server gets: no channel, and therefore nothing is sent.
    """

    __slots__ = ("channel_id", "greet_join", "greet_leave")

    def __init__(self, channel_id: int | None = None,
                 greet_join: bool = False,
                 greet_leave: bool = False) -> None:
        self.channel_id = channel_id
        self.greet_join = greet_join
        self.greet_leave = greet_leave

    @property
    def enabled(self) -> bool:
        """True once the server picked a channel."""
        return self.channel_id is not None


class Greetings(commands.Cog):
    """Greets new members and says goodbye to the ones that leave."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.log = logging.getLogger(__name__)

    # ------------------------------------------------------------- settings
    async def settings_for(self, guild_id: int) -> GreetingSettings:
        """Read the greeting settings of a server.

        Members join and leave rarely compared to messages, so this reads the
        database every time instead of keeping a cache that could go stale.
        """
        row = await get_guild_settings(guild_id)
        return GreetingSettings(
            channel_id=row["greeting_channel_id"] if row else None,
            greet_join=bool(row["greeting_join"]) if row else False,
            greet_leave=bool(row["greeting_leave"]) if row else False,
        )

    async def switch(self, interaction: discord.Interaction,
                     greet_join: bool | None = None,
                     greet_leave: bool | None = None) -> GreetingSettings:
        """Write one of the two switches, then read the settings back.

        Both switches keep the channel they had, so changing one of them never
        silently resets the other.
        """
        await set_greeting_switches(interaction.guild.id, greet_join, greet_leave)
        return await self.settings_for(interaction.guild.id)

    # --------------------------------------------------------- the two events
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Welcome somebody who just joined the server."""
        if member.bot and not GREET_BOTS:
            return
        settings = await self.settings_for(member.guild.id)
        if not settings.greet_join:
            return
        await self.announce(member.guild, member, settings, hello=True)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Say goodbye to somebody who just left the server."""
        if member.bot and not GREET_BOTS:
            return
        settings = await self.settings_for(member.guild.id)
        if not settings.greet_leave:
            return
        await self.announce(member.guild, member, settings, hello=False)

    # ------------------------------------------------------ the announcement
    async def announce(self, guild: discord.Guild, member: discord.Member,
                       settings: GreetingSettings, hello: bool) -> None:
        """Post the welcome or the goodbye, as far as Discord allows.

        Best effort on purpose: a member that leaves (or a channel that is gone)
        must never raise into the event loop, since that would only be logged.
        """
        target = self.greeting_channel(guild, settings)
        if target is None:
            return
        embed = self.build_embed(guild, member, hello)
        try:
            await target.send(embed=embed)
        except discord.HTTPException as error:
            self.log.warning("Could not greet %s in guild %s: %r",
                             member.id, guild.id, error)

    def build_embed(self, guild: discord.Guild, member: discord.Member,
                    hello: bool) -> discord.Embed:
        """The card posted for a member that joined or left."""
        embed = discord.Embed(
            title=(WELCOME_TITLE if hello else FAREWELL_TITLE).format(
                guild=guild.name, member=member.display_name),
            color=discord.Color.blurple() if hello else discord.Color.greyple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name=MEMBER_COUNT_FIELD, value=str(guild.member_count))
        if member.created_at is not None:
            embed.add_field(name=ACCOUNT_BORN_FIELD,
                            value=member.created_at.strftime(DATE_FORMAT),
                            inline=True)
            embed.add_field(name=ACCOUNT_AGE_FIELD,
                            value=ACCOUNT_AGE_DAYS.format(
                                days=self.account_age_days(member)),
                            inline=True)
        return embed

    @staticmethod
    def account_age_days(member: discord.Member) -> int:
        """How many days ago the account of `member` was created.

        The date comes from Discord, so it is always there for a real member;
        the fallback keeps the card buildable for one without any date at all.
        """
        created = member.created_at or datetime.now(timezone.utc)
        return max(0, (datetime.now(timezone.utc) - created).days)

    @staticmethod
    def greeting_channel(guild: discord.Guild,
                         settings: GreetingSettings) -> Messageable | None:
        """Where a greeting has to go, None while there is no such channel.

        There is no fallback to the system channel on purpose: a server that
        never asked for greetings must not be written to out of the blue.
        """
        if settings.channel_id is None:
            return None
        channel = guild.get_channel(settings.channel_id)
        if isinstance(channel, Messageable):
            return channel
        return None

    # ---------------------------------------------------- staff only commands
    greetings = app_commands.Group(
        name="greetings", description="Welcome and goodbye messages",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True))

    @greetings.command(name="channel",
                       description="Choose where the greetings are posted")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="Channel for both messages, leave empty to turn them off")
    async def greetings_channel(
            self, interaction: discord.Interaction,
            channel: discord.TextChannel | discord.Thread | None = None
    ) -> None:
        """Store `channel` as the greeting channel, empty resets the setting."""
        if channel is None:
            await set_greeting_channel(interaction.guild.id, None)
            await interaction.response.send_message(
                CHANNEL_CLEARED, ephemeral=True)
            return

        self.check_writable_channel(interaction, channel)
        await set_greeting_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            CHANNEL_SET.format(channel=channel.mention), ephemeral=True)

    @greetings.command(name="join", description="Greet new members or not")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(enabled="Turn the welcome of new members on or off")
    async def greetings_join(self, interaction: discord.Interaction,
                             enabled: bool) -> None:
        """Turn the welcome message on or off."""
        settings = await self.switch(interaction, greet_join=enabled)
        text = await self.switch_answer(interaction, settings, enabled,
                                        SWITCH_JOIN_ON, SWITCH_JOIN_OFF)
        await interaction.response.send_message(text, ephemeral=True)

    @greetings.command(name="leave",
                       description="Say goodbye to leaving members or not")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(enabled="Turn the goodbye of leavers on or off")
    async def greetings_leave(self, interaction: discord.Interaction,
                              enabled: bool) -> None:
        """Turn the goodbye message on or off."""
        settings = await self.switch(interaction, greet_leave=enabled)
        text = await self.switch_answer(interaction, settings, enabled,
                                        SWITCH_LEAVE_ON, SWITCH_LEAVE_OFF)
        await interaction.response.send_message(text, ephemeral=True)

    async def switch_answer(self, interaction: discord.Interaction,
                            settings: GreetingSettings, enabled: bool,
                            on_text: str, off_text: str) -> str:
        """The answer to one of the two switches.

        A switch that is turned on without a channel says so plainly, since
        nothing would be posted yet and the plain "on" text would promise
        something the server does not get.
        """
        if not enabled:
            return off_text
        if not settings.enabled:
            return SWITCH_ON_NO_CHANNEL
        return on_text.format(
            channel=await self.channel_field(interaction.guild,
                                             settings.channel_id))

    @greetings.command(name="test", description="Post a preview greeting")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(leaving="Preview the goodbye instead of the welcome")
    async def greetings_test(self, interaction: discord.Interaction,
                             leaving: bool = False) -> None:
        """Post one message to the greeting channel, without a real member."""
        settings = await self.settings_for(interaction.guild.id)
        target = self.greeting_channel(interaction.guild, settings)
        if target is None:
            await interaction.response.send_message(
                CHANNEL_NOT_SET, ephemeral=True)
            return

        # the group is guild_only, so interaction.user is a member of the server
        embed = self.build_embed(interaction.guild, interaction.user,
                                 hello=not leaving)
        embed.set_footer(text=PREVIEW_LEAVE if leaving else PREVIEW_JOIN)
        try:
            await target.send(embed=embed)
        except discord.HTTPException as error:
            self.log.warning("Could not post a greeting preview in guild %s: %r",
                             interaction.guild.id, error)
            await interaction.response.send_message(
                MISSING_CHANNEL.format(channel_id=settings.channel_id),
                ephemeral=True)
            return
        await interaction.response.send_message(
            PREVIEW_SENT.format(channel=target.mention), ephemeral=True)

    @greetings.command(name="settings",
                       description="Show the greeting settings of this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def greetings_settings(self, interaction: discord.Interaction) -> None:
        """Show the greeting channel and both switches of this server."""
        settings = await self.settings_for(interaction.guild.id)
        embed = discord.Embed(
            title=SETTINGS_TITLE.format(guild=interaction.guild.name),
            color=discord.Color.blurple())
        embed.add_field(name=SETTINGS_CHANNEL_FIELD,
                        value=await self.channel_field(
                            interaction.guild, settings.channel_id),
                        inline=False)
        embed.add_field(name=SETTINGS_JOIN_FIELD,
                        value=SETTINGS_YES if settings.greet_join
                        else SETTINGS_NO)
        embed.add_field(name=SETTINGS_LEAVE_FIELD,
                        value=SETTINGS_YES if settings.greet_leave
                        else SETTINGS_NO)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------------------------------------------------- view helpers
    async def channel_field(self, guild: discord.Guild,
                            channel_id: int | None) -> str:
        """Name the stored greeting channel, or say that there is none."""
        if channel_id is None:
            return CHANNEL_NEEDS_CHANNEL
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.HTTPException:
                # the channel is gone: stop pointing at it instead of showing a
                # dead id forever
                await clear_greeting_channel(guild.id, channel_id)
                return MISSING_CHANNEL.format(channel_id=channel_id)
        return channel.mention

    @staticmethod
    def check_writable_channel(interaction: discord.Interaction, channel) -> None:
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
            raise GreetingConfigError(CHANNEL_NO_ACCESS.format(
                channel=channel.mention, perms=", ".join(missing)))

    # ------------------------------------------------------------- clean ups
    @commands.Cog.listener()
    async def on_guild_channel_delete(self,
                                      channel: discord.abc.GuildChannel) -> None:
        """Forget a deleted channel instead of leaving a dangling id behind."""
        await clear_greeting_channel(channel.guild.id, channel.id)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        """Same for a deleted thread, which is not reported as a channel."""
        await clear_greeting_channel(thread.guild.id, thread.id)

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

        if isinstance(original, (app_commands.CheckFailure,
                                 commands.MissingPermissions)):
            text = PERMISSION_ERROR
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
    await bot.add_cog(Greetings(bot))
