"""Message management cog: bulk delete messages in a channel.

The cog adds a single slash command, ``/clear``, which removes the last N
messages from the channel where it was used. Only members holding the
**Manage Messages** permission can run it, and the bot needs the same
permission plus **Read Message History** in that channel.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

# ------------------------------------------------------------------ limits
CLEAR_MIN = 1
CLEAR_MAX = 100  # Discord bulk-deletes at most 100 messages per request

# user facing texts
CLEAR_DESCRIPTION = "Удалить последние сообщения в этом канале"
CLEAR_AMOUNT_DESC = "Сколько последних сообщений удалить (1–100)"
CLEAR_DONE = "🗑️ Удалено сообщений: **{count}**"
CLEAR_NOT_TEXT = "❌ Команда работает только в текстовых каналах сервера."
CLEAR_NO_PERMS = ("🚫 Вам нужно право **Управлять сообщениями**, "
                  "чтобы использовать эту команду.")
CLEAR_BOT_NO_PERMS = ("🚫 У бота нет права **Управлять сообщениями** "
                      "в этом канале.\n"
                      "Выдайте ему право, чтобы `/clear` заработал.")
CLEAR_FAILED = "❌ Не удалось удалить сообщения: {error}"


class MessageManagement(commands.Cog):
    """Commands for managing messages, starting with bulk deletion."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.log = logging.getLogger(__name__)

    # --------------------------------------------------------------- /clear
    @app_commands.command(name="clear", description=CLEAR_DESCRIPTION)
    @app_commands.describe(amount=CLEAR_AMOUNT_DESC)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction,
                    amount: app_commands.Range[int, CLEAR_MIN, CLEAR_MAX]
                    ) -> None:
        """Delete the last `amount` messages of the current channel."""
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(CLEAR_NOT_TEXT,
                                                    ephemeral=True)
            return

        # Defer so a long purge does not make the interaction expire.
        await interaction.response.defer(ephemeral=True, thinking=True)

        me = interaction.guild.me
        if me is None or not channel.permissions_for(me).manage_messages:
            await interaction.followup.send(CLEAR_BOT_NO_PERMS,
                                            ephemeral=True)
            return

        try:
            deleted = await channel.purge(limit=amount)
        except discord.Forbidden:
            await interaction.followup.send(CLEAR_BOT_NO_PERMS,
                                            ephemeral=True)
            return
        except discord.HTTPException as error:
            self.log.warning("clear failed in %s: %r", channel, error)
            await interaction.followup.send(
                CLEAR_FAILED.format(error=error), ephemeral=True)
            return

        await interaction.followup.send(
            CLEAR_DONE.format(count=len(deleted)),
            ephemeral=True,
            delete_after=10.0)

    # ------------------------------------------------------------- errors
    async def cog_app_command_error(self,
                                    interaction: discord.Interaction,
                                    error: app_commands.AppCommandError
                                    ) -> None:
        """Handles the application command errors the tree forwards to the cog."""
        original: BaseException = error
        for _ in range(5):
            inner = getattr(original, "original", None)
            if inner is None:
                break
            original = inner

        if isinstance(original, app_commands.MissingPermissions):
            text = CLEAR_NO_PERMS
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
    await bot.add_cog(MessageManagement(bot))