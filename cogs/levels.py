"""Levels cog: message XP, rank cards and the server leaderboard.

Every tunable (XP range, cooldown, level curve, texts) lives at the top of
this file so the command logic never has to change for a balance tweak.
"""

import logging
import random

import discord
from discord import app_commands
from discord.abc import Messageable
from discord.ext import commands, tasks

from db import (
    add_balance,
    add_xp_with_cooldown,
    get_leaderboard,
    get_rank_position,
    get_user,
    mark_voice_join,
    reset_xp,
    set_level,
    set_xp,
)

# ------------------------------------------------------------------ settings
XP_MIN = 15                  # XP for a single message
XP_MAX = 25
XP_COOLDOWN = 60             # seconds between two XP gains for the same user
LEVEL_UP_COINS_PER_LEVEL = 1  # coins paid per level reached (0 = no reward)
IGNORED_CHANNEL_IDS: set[int] = set()   # e.g. {123456789} to mute XP in #spam
IGNORED_ROLE_IDS: set[int] = set()      # e.g. {123456789} to skip a muted role

# voice XP: sitting in a voice channel pays out once per interval
VOICE_XP_ENABLED = True
VOICE_XP_MIN = 15            # XP for a full interval of voice time
VOICE_XP_MAX = 25
VOICE_XP_INTERVAL = 60       # seconds in voice that pay out
VOICE_XP_IGNORE_MUTED = True            # muted/deafened members earn nothing
VOICE_XP_REQUIRE_OTHER_MEMBERS = False  # True = must not sit alone in a channel
VOICE_XP_IGNORE_AFK_CHANNEL = True

# channel for level ups that did not happen in a text channel (voice XP);
# falls back to guild.system_channel while this stays None
LEVEL_UP_CHANNEL_ID: int | None = None

# user facing texts
LEVEL_UP_MESSAGE = "🎉 {mention} reached **level {level}**!"
LEVEL_UP_REWARD_MESSAGE = "💰 +{coins} coins"
LEADERBOARD_TITLE = "🏆 {guild} leaderboard"
EMPTY_LEADERBOARD = "Nobody earned XP here yet - start chatting!"
PERMISSION_ERROR = "You need the **Manage Server** permission to use this command."
MEMBER_NOT_FOUND_ERROR = "I could not find that member."
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


# --------------------------------------------------------- level curve maths
def xp_to_next_level(level: int) -> int:
    """XP that has to be earned inside `level` to reach `level` + 1."""
    return 5 * level * level + 50 * level + 100


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

    # ------------------------------------------------------------ XP gaining
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if message.channel.id in IGNORED_CHANNEL_IDS:
            return
        roles = getattr(message.author, "roles", ())
        if IGNORED_ROLE_IDS.intersection(role.id for role in roles):
            return

        amount = random.randint(XP_MIN, XP_MAX)
        awarded, xp, stored_level = await add_xp_with_cooldown(
            message.guild.id, message.author.id, amount, XP_COOLDOWN)
        if not awarded:
            return

        new_level = level_from_xp(xp)
        if new_level <= stored_level:
            return
        await set_level(message.guild.id, message.author.id, new_level)
        await self.announce_level_up(message.guild, message.author, new_level,
                                     message.channel)

    async def announce_level_up(self, guild: discord.Guild, member: discord.Member,
                                level: int,
                                channel: Messageable | None = None) -> None:
        """Pay the level reward and post the level up message, if any."""
        coins = level_up_reward(level)
        if coins:
            await add_balance(guild.id, member.id, coins)
        channel = channel or self.level_up_channel(guild)
        if channel is None:
            return
        message = LEVEL_UP_MESSAGE.format(mention=member.mention, level=level)
        if coins:
            message += "\n" + LEVEL_UP_REWARD_MESSAGE.format(coins=coins)
        try:
            await channel.send(message)
        except discord.HTTPException:
            pass  # missing permissions or deleted channel: XP is already saved

    @staticmethod
    def level_up_channel(guild: discord.Guild) -> Messageable | None:
        """LEVEL_UP_CHANNEL_ID when it is usable, else the system channel."""
        if LEVEL_UP_CHANNEL_ID:
            channel = guild.get_channel(LEVEL_UP_CHANNEL_ID)
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
        if not self.voice_channel_allowed(member.guild, after.channel):
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
        for channel in guild.voice_channels:
            if not self.voice_channel_allowed(guild, channel):
                continue
            members = [m for m in channel.members
                       if not m.bot and self.voice_eligible(m)]
            if VOICE_XP_REQUIRE_OTHER_MEMBERS and len(members) < 2:
                continue
            for member in members:
                amount = random.randint(VOICE_XP_MIN, VOICE_XP_MAX)
                awarded, xp, stored_level = await add_xp_with_cooldown(
                    guild.id, member.id, amount, VOICE_XP_INTERVAL,
                    column="last_voice_xp")
                if not awarded:
                    continue  # joined less than one interval ago, or paid already
                new_level = level_from_xp(xp)
                if new_level > stored_level:
                    await set_level(guild.id, member.id, new_level)
                    await self.announce_level_up(guild, member, new_level)

    @staticmethod
    def voice_channel_allowed(guild: discord.Guild,
                              channel: discord.VoiceChannel) -> bool:
        if channel.id in IGNORED_CHANNEL_IDS:
            return False
        if VOICE_XP_IGNORE_AFK_CHANNEL and channel == guild.afk_channel:
            return False
        return True

    @staticmethod
    def voice_eligible(member: discord.Member) -> bool:
        """True while the member sits in voice and counts as unmuted."""
        state = member.voice
        if state is None or state.channel is None or state.afk:
            return False
        if IGNORED_ROLE_IDS.intersection(role.id for role in member.roles):
            return False
        if VOICE_XP_IGNORE_MUTED and (state.mute or state.deaf
                                      or state.self_mute or state.self_deaf):
            return False
        return True

    # -------------------------------------------------------------- commands
    @commands.hybrid_command(
        name="rank", description="Show the level and XP of a member")
    @commands.guild_only()
    async def rank(self, ctx: commands.Context,
                   member: discord.Member | None = None) -> None:
        """Show the level, XP and leaderboard position of a member.

        Leave `member` empty to see your own rank.
        """
        member = member or ctx.author
        row = await get_user(ctx.guild.id, member.id)
        xp = row["xp"]
        level = level_from_xp(xp)
        current = xp - total_xp_for_level(level)
        needed = xp_to_next_level(level)
        position = await get_rank_position(ctx.guild.id, member.id)

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
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="leaderboard", description="Show the XP leaderboard of this server")
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context) -> None:
        """Show the ten members with the most XP."""
        rows = await get_leaderboard(ctx.guild.id, 10)
        if not rows:
            await ctx.send(EMPTY_LEADERBOARD)
            return

        lines = []
        for index, row in enumerate(rows, start=1):
            member = ctx.guild.get_member(row["user_id"])
            name = member.display_name if member else f"Unknown member ({row['user_id']})"
            marker = MEDALS.get(index, f"**{index}.**")
            lines.append(f"{marker} {name} — level {level_from_xp(row['xp'])}, {row['xp']} XP")

        embed = discord.Embed(
            title=LEADERBOARD_TITLE.format(guild=ctx.guild.name),
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await ctx.send(embed=embed)

    # ---------------------------------------------------- staff only commands
    @commands.hybrid_group(
        name="levels", description="Manage the XP and levels of this server")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levels(self, ctx: commands.Context) -> None:
        """Show how to use the level management commands."""
        prefix = ctx.clean_prefix or "/"
        await ctx.send(
            "**Level management**\n"
            f"`{prefix}levels addxp <member> <amount>` - give XP "
            "(a negative amount takes XP away)\n"
            f"`{prefix}levels setxp <member> <amount>` - overwrite the total XP\n"
            f"`{prefix}levels resetxp <member>` - back to level 0\n"
            "The same names work as slash commands: `/levels [...]`."
        )

    @levels.command(name="addxp", description="Add XP to a member")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levels_addxp(self, ctx: commands.Context,
                           member: discord.Member, amount: int) -> None:
        """Add `amount` XP to `member`. A negative amount removes XP."""
        row = await get_user(ctx.guild.id, member.id)
        new_xp = max(0, row["xp"] + amount)
        new_level = level_from_xp(new_xp)
        await set_xp(ctx.guild.id, member.id, new_xp, new_level)
        await ctx.send(
            f"{member.mention} now has **{new_xp} XP** (level **{new_level}**).")

    @levels.command(name="setxp", description="Overwrite the total XP of a member")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levels_setxp(self, ctx: commands.Context,
                           member: discord.Member, amount: int) -> None:
        """Set the total XP of `member` to `amount`."""
        new_xp = max(0, amount)
        new_level = level_from_xp(new_xp)
        await set_xp(ctx.guild.id, member.id, new_xp, new_level)
        await ctx.send(
            f"{member.mention} is now at **{new_xp} XP** (level **{new_level}**).")

    @levels.command(name="resetxp", description="Reset a member back to level 0")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def levels_resetxp(self, ctx: commands.Context,
                             member: discord.Member) -> None:
        """Delete every XP of `member`."""
        await reset_xp(ctx.guild.id, member.id)
        await ctx.send(f"{member.mention} is back to **level 0**.")

    # ---------------------------------------------------------------- errors
    async def cog_command_error(self, ctx: commands.Context,
                                error: commands.CommandError) -> None:
        """Handles failures of both the prefix and the slash invocation."""
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(PERMISSION_ERROR, ephemeral=True)
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send(MEMBER_NOT_FOUND_ERROR, ephemeral=True)
        elif isinstance(error, commands.BadArgument):
            await ctx.send(f"Invalid argument: {error}", ephemeral=True)
        else:
            raise error

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

