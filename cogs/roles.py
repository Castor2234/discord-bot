"""Reaction roles: react to a message, receive the role behind that emoji.

Every mapping lives in the reaction_roles table (see db.py), so a menu keeps
working after a restart and can be attached to any message the bot can read.

Granting is deliberately add-only: adding a reaction hands the role out,
removing the reaction never takes it back. Clearing the reactions of a message
therefore cannot strip roles from anybody, and staff who want a role removed
use `rolemenu take`.
"""

import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.abc import Messageable
from discord.ext import commands

from db import (
    add_reaction_role,
    delete_reaction_menu,
    delete_reaction_role,
    delete_reaction_roles_for_role,
    get_reaction_menu,
    get_reaction_menus,
    get_reaction_role,
)

# ------------------------------------------------------------------ settings
# staff channel that gets a line for every granted role; nothing is sent
# while this stays None - unlike the shop log there is no fallback to the
# system channel, a busy menu would flood it
ROLE_LOG_CHANNEL_ID: int | None = None

# staff may only map roles that sit below their own highest role, otherwise
# anyone with the Manage Server permission could hand out Administrator
REQUIRE_STAFF_OUTRANK = True

MAX_OPTIONS_PER_MENU = 20        # Discord allows 20 different reactions
MAX_UNICODE_EMOJI_CHARS = 16     # sanity limit for a typed unicode emoji
MENU_LIST_LIMIT = 15             # menus rendered by one `rolemenu list`

GRANT_REASON = "Reaction role"
REVOKE_REASON = "Reaction role revoked by staff"

# user facing texts
JUMP_URL = "https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
MENU_ADDED = "\u2705 {emoji} on [that message]({url}) now grants **{role}**."
MENU_UPDATED = "\U0001f501 {emoji} on [that message]({url}) now grants " \
               "**{role}** (it used to grant **{old}**)."
MENU_REMOVED = "\U0001f5d1 {emoji} on [that message]({url}) no longer grants " \
               "a role."
MENU_NOT_MAPPED = "That message has no {emoji} mapping."
MENU_CLEARED = "\U0001f5d1 Removed **{count}** mapping(s) of [that message]" \
               "({url})."
MENU_EMPTY = "That message has no reaction role mapping."
MENU_SHOW_TITLE = "\U0001f3ad [That message]({url}) maps **{count}** emoji:"
MENU_LIST_TITLE = "\U0001f3ad Reaction role menus of **{guild}**"
MENU_LIST_LINE = "\u2022 <#{channel_id}> [that message]({url}): {options}"
MENU_LIST_EMPTY = "There is no reaction role menu on this server yet."
MENU_LIST_MORE = "\u2026 and **{count}** more menu(s), use `show` for one of them."
MENU_MISSING_ROLE = "(deleted role)"
MENU_FULL = "One message can map at most **{limit}** emoji."

MESSAGE_NOT_FOUND = "I cannot find message **{message_id}** in <#{channel_id}>. " \
                    "Right click the message \u2192 *Copy Message Link* and paste " \
                    "that link instead, a bare ID only works inside the channel " \
                    "you pick."
MESSAGE_REF_INVALID = "I cannot read `{text}` as a message. Paste the message " \
                      "link (right click the message \u2192 *Copy Message Link*) " \
                      "or its bare ID."
MESSAGE_CHANNEL_MISSING = "A bare message ID needs the `channel` argument. The " \
                         "easy way round it is pasting the message link instead, " \
                         "it already carries the channel."
MESSAGE_CHANNEL_MISMATCH = "That message link points at <#{link_channel}> while " \
                          "the `channel` argument says <#{picked_channel}> - keep " \
                          "only one of them."
MESSAGE_CHANNEL_UNREADABLE = "<#{channel_id}> is gone, or I cannot read messages " \
                            "there (I need View Channel and Read Message History)."
CHANNEL_NO_ACCESS = "I cannot read <#{channel_id}> - I need View Channel and " \
                    "Read Message History there."
BOT_MISSING_PERMISSIONS = "I am missing {perms} in <#{channel_id}> for that menu."
REACTION_FAILED = "I could not react with {emoji}: {reason}. Nothing was saved."
EMOJI_INVALID = "I cannot read `{emoji}` as an emoji. Paste the emoji itself " \
                "(\u2705 or <:name:id>), not a `:name:` shortcut."

ROLE_EVERYONE = "@everyone cannot be granted by a reaction."
ROLE_MANAGED = "**{role}** is managed by Discord or by an integration, so I " \
               "cannot hand it out."
ROLE_TOO_HIGH_BOT = "My highest role must be above **{role}** before I can " \
                    "assign it."
ROLE_TOO_HIGH_STAFF = "You cannot hand out **{role}**: it is at or above your " \
                      "own highest role."
ROLE_NOT_HELD = "{member} does not have **{role}**."
ROLE_TAKEN = "\U0001f3ad Took **{role}** away from {member}."
REVOKE_FAILED = "I could not remove **{role}**: {reason}."
ROLE_GRANT_LOG = "\U0001f3ad {mention} got **{role}** from a reaction in " \
                 "<#{channel_id}>."

PERMISSION_ERROR = "You need the **Manage Server** permission to use this command."
MEMBER_NOT_FOUND_ERROR = "I could not find that member."
ROLE_NOT_FOUND_ERROR = "I could not find that role."


# -------------------------------------------------------------------- emojis
def reaction_key(emoji) -> str | None:
    """Return the ``name:id`` / unicode key that identifies this reaction.

    This is the form discord.py uses internally for reactions, so it matches a
    stored mapping and ``payload.emoji`` of a raw reaction event alike.
    """
    if isinstance(emoji, str):
        return emoji or None
    name = getattr(emoji, "name", None)
    if not name:
        return None
    emoji_id = getattr(emoji, "id", None)
    if emoji_id is not None:
        return f"{name}:{emoji_id}"
    return name


def render_key(key: str) -> str:
    """Turn a stored key back into something Discord renders as an emoji."""
    name, _, raw_id = key.rpartition(":")
    if name and raw_id.isdigit():
        return f"<:{name}:{raw_id}>"
    return key


def emoji_from_key(key: str) -> discord.PartialEmoji | str:
    """Rebuild the emoji object ``add_reaction`` expects from a stored key."""
    name, _, raw_id = key.rpartition(":")
    if name and raw_id.isdigit():
        return discord.PartialEmoji(name=name, id=int(raw_id))
    return key


def parse_emoji(text: str) -> discord.PartialEmoji | None:
    """Parse what a staff member typed, None when it is not a usable emoji."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        emoji = discord.PartialEmoji.from_str(text)
    except ValueError:
        return None
    if emoji.id is not None:
        return emoji
    name = emoji.name or ""
    # a unicode emoji is short and carries no markup; anything longer or with
    # a colon inside is a plain word or a `:name:` shortcut
    if len(name) > MAX_UNICODE_EMOJI_CHARS or ":" in name or "<" in name:
        return None
    return emoji or None


def guild_emoji_by_name(guild: discord.Guild, text: str):
    """Resolve a `name` / `:name:` shortcut against the custom emoji of `guild`.

    Staff copy the shortcut from the emoji picker of their own server, so this
    only ever reaches for an emoji this server actually owns.
    """
    name = (text or "").strip().strip(":")
    if not name or ":" in name or "<" in name:
        return None
    return discord.utils.get(guild.emojis, name=name)


def http_reason(error: BaseException) -> str:
    """Short human readable reason taken from a failed REST call."""
    return getattr(error, "text", None) or str(error) or type(error).__name__


class RoleSetupError(commands.CommandError):
    """Raised when a menu cannot be built or a role cannot be handed out."""


# ------------------------------------------------------------- message targets
SNOWFLAKE = r"\d{15,25}"
# `guild/channel/message` of a message link, `@me` allowed in place of the
# guild and the extra `-` segment Discord uses in a "newest message" link
MESSAGE_LINK = re.compile(
    rf"(?:{SNOWFLAKE}|@me)/({SNOWFLAKE})(?:/-)?/({SNOWFLAKE})")


_USER_MENTION = re.compile(rf"^<@!?({SNOWFLAKE})>$")


def parse_message_ref(text: str) -> tuple[int | None, int]:
    """Split what staff pasted into ``(channel id or None, message id)``.

    A bare message ID carries no channel, every Discord message link does, so
    the link is the form that cannot point at the wrong place. A number typed
    as an argument reaches us untouched, but a number that happens to be a user
    ID is autolinked into a `<@id>` mention, which is unwrapped here. A channel
    mention is deliberately *not* accepted: it looks like an ID and is not one.
    """
    text = (text or "").strip()
    mention = _USER_MENTION.match(text)
    text = mention.group(1) if mention else text.strip("`\"'.,;")
    if text.isdigit():
        return None, int(text)
    match = MESSAGE_LINK.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    raise RoleSetupError(MESSAGE_REF_INVALID.format(text=text or "<empty>"))


class Roles(commands.Cog):
    """Reaction role menus: react with an emoji, get the role behind it."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.log = logging.getLogger(__name__)

    # ------------------------------------------------------------- granting
    @commands.Cog.listener()
    async def on_raw_reaction_add(self,
                                  payload: discord.RawReactionActionEvent) -> None:
        """Give the mapped role to whoever reacted with the right emoji."""
        await self.grant_role(payload)

    async def grant_role(self, payload) -> None:
        """Assign the role behind this reaction, ignoring anything unusable."""
        if self.bot.user is None or payload.guild_id is None:
            return  # a reaction in DMs can never map to a guild role
        # a raw reaction event only carries the guild id, never the object
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        if payload.user_id == self.bot.user.id:
            return  # the reaction we added ourselves while building the menu
        key = reaction_key(payload.emoji)
        if key is None:
            return
        row = await get_reaction_role(payload.message_id, key)
        if row is None:
            return
        if row["guild_id"] != guild.id:
            return  # message ids are global, roles are not

        role = guild.get_role(row["role_id"])
        if role is None:
            dropped = await delete_reaction_roles_for_role(guild.id,
                                                           row["role_id"])
            self.log.warning("Dropped %s dead mapping(s) of removed role %s in "
                             "guild %s", dropped, row["role_id"], guild.id)
            return
        if role.is_default() or role.managed:
            await delete_reaction_role(payload.message_id, key)
            self.log.warning("Reaction role %s of guild %s cannot be assigned "
                             "by anybody", role.id, guild.id)
            return

        member = payload.member
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return
        if member is None or member.bot or role in member.roles:
            return
        if guild.me is None or role >= guild.me.top_role:
            # the mapping is kept on purpose: an admin can still fix the
            # role hierarchy and the menu starts working again by itself
            self.log.warning("I cannot assign reaction role %s of guild %s: it "
                             "sits at or above my top role", role.id, guild.id)
            return

        try:
            await member.add_roles(role, reason=GRANT_REASON)
        except discord.Forbidden:
            self.log.warning("Missing Manage Roles or hierarchy for reaction "
                             "role %s of guild %s", role.id, guild.id)
            return
        except discord.HTTPException as error:
            self.log.warning("Granting reaction role %s of guild %s failed: %s",
                             role.id, guild.id, http_reason(error))
            return

        self.log.info("%s got role %s from a reaction on message %s in guild %s",
                      member.id, role.id, payload.message_id, guild.id)
        await self.announce(member, role, payload.channel_id)

    async def announce(self, member: discord.Member, role: discord.Role,
                       channel_id: int) -> None:
        """Mirror a granted role into ROLE_LOG_CHANNEL_ID when one is set."""
        if ROLE_LOG_CHANNEL_ID is None:
            return
        channel = self.bot.get_channel(ROLE_LOG_CHANNEL_ID)
        if not isinstance(channel, Messageable):
            return
        try:
            await channel.send(ROLE_GRANT_LOG.format(
                mention=member.mention, role=role.name, channel_id=channel_id))
        except discord.HTTPException:
            pass  # the role is assigned, the log line is only a bonus

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        """A deleted role must not leave clickable but dead reactions."""
        dropped = await delete_reaction_roles_for_role(role.guild.id, role.id)
        if dropped:
            self.log.info("Removed %s reaction role mapping(s) of deleted role "
                          "%s in guild %s", dropped, role.id, role.guild.id)

    # ------------------------------------------------------------ validation
    def check_role(self, interaction: discord.Interaction,
                   role: discord.Role) -> None:
        """Refuse every role that nobody should be able to hand out."""
        if role.is_default():
            raise RoleSetupError(ROLE_EVERYONE)
        if role.managed:
            raise RoleSetupError(ROLE_MANAGED.format(role=role.name))
        me = interaction.guild.me
        if me is None or role >= me.top_role:
            raise RoleSetupError(ROLE_TOO_HIGH_BOT.format(role=role.name))
        author = interaction.user
        if not isinstance(author, discord.Member):
            return
        privileged = (author.guild_permissions.administrator
                      or interaction.guild.owner_id == author.id)
        if REQUIRE_STAFF_OUTRANK and not privileged and role >= author.top_role:
            raise RoleSetupError(ROLE_TOO_HIGH_STAFF.format(role=role.name))

    @staticmethod
    def missing_permissions(channel: Messageable) -> list[str]:
        """What the bot lacks in `channel` for a working menu."""
        perms = channel.permissions_for(channel.guild.me)
        missing = []
        if not perms.manage_roles:
            missing.append("**Manage Roles**")
        if not perms.view_channel:
            missing.append("**View Channel**")
        if not perms.read_message_history:
            missing.append("**Read Message History**")
        if not perms.add_reactions:
            missing.append("**Add Reactions**")
        return missing

    @staticmethod
    async def fetch_message(channel: Messageable,
                            message_id: int) -> discord.Message:
        """The menu message, with a readable error instead of a raw 404."""
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound as error:
            raise RoleSetupError(MESSAGE_NOT_FOUND.format(
                message_id=message_id, channel_id=channel.id)) from error
        except discord.Forbidden as error:
            raise RoleSetupError(CHANNEL_NO_ACCESS.format(
                channel_id=channel.id)) from error

    async def resolve_target(self, guild: discord.Guild, message: str,
                             channel: Optional[discord.TextChannel]
                             ) -> tuple[Messageable, int]:
        """The ``(channel, message id)`` a menu should be built on.

        A message link brings its own channel and works for threads too, a
        bare message ID can only be read together with the `channel` argument.
        """
        link_channel_id, message_id = parse_message_ref(message)
        if channel is not None:
            if link_channel_id is not None and link_channel_id != channel.id:
                raise RoleSetupError(MESSAGE_CHANNEL_MISMATCH.format(
                    link_channel=link_channel_id, picked_channel=channel.id))
            return channel, message_id
        if link_channel_id is None:
            raise RoleSetupError(MESSAGE_CHANNEL_MISSING)
        target = guild.get_channel(link_channel_id) or guild.get_thread(
            link_channel_id)
        if target is None:
            try:
                target = await guild.fetch_channel(link_channel_id)
            except discord.HTTPException as error:
                raise RoleSetupError(MESSAGE_CHANNEL_UNREADABLE.format(
                    channel_id=link_channel_id)) from error
        if not isinstance(target, Messageable):
            raise RoleSetupError(MESSAGE_CHANNEL_UNREADABLE.format(
                channel_id=link_channel_id))
        return target, message_id

    @staticmethod
    def menu_url(row) -> str:
        """Jump link of the message a stored mapping points at."""
        return JUMP_URL.format(guild_id=row["guild_id"],
                              channel_id=row["channel_id"],
                              message_id=row["message_id"])

    # ------------------------------------------------------------- commands
    rolemenu = app_commands.Group(
        name="rolemenu", description="Manage the reaction role menus",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True))

    @rolemenu.command(name="add",
                      description="Map an emoji of a message to a role")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        message="Message link (the safe one) or the message ID",
        emoji="Emoji to react with, e.g. \u2705 or <:name:id>",
        role="Role that this reaction grants",
        channel="Channel of that message, only needed for a bare ID",
    )
    async def rolemenu_add(self, interaction: discord.Interaction,
                           message: str, emoji: str, role: discord.Role,
                           channel: Optional[discord.TextChannel] = None) -> None:
        """Map `emoji` on `message` to `role` and react with it."""
        guild = interaction.guild
        target, message_id = await self.resolve_target(guild, message, channel)
        parsed = parse_emoji(emoji) or guild_emoji_by_name(guild, emoji)
        if parsed is None:
            raise RoleSetupError(EMOJI_INVALID.format(emoji=emoji))
        key = reaction_key(parsed)
        self.check_role(interaction, role)

        missing = self.missing_permissions(target)
        if missing:
            raise RoleSetupError(BOT_MISSING_PERMISSIONS.format(
                channel_id=target.id, perms=", ".join(missing)))
        menu_message = await self.fetch_message(target, message_id)

        menu = await get_reaction_menu(message_id)
        old = next((row for row in menu if row["emoji"] == key), None)
        if old is None and len(menu) >= MAX_OPTIONS_PER_MENU:
            raise RoleSetupError(MENU_FULL.format(limit=MAX_OPTIONS_PER_MENU))

        await add_reaction_role(guild.id, target.id, message_id, key, role.id,
                                interaction.user.id)
        try:
            await menu_message.add_reaction(emoji_from_key(key))
        except discord.HTTPException as error:
            # a mapping nobody can click would just hide a broken menu
            await delete_reaction_role(message_id, key)
            raise RoleSetupError(REACTION_FAILED.format(
                emoji=render_key(key), reason=http_reason(error))) from error

        url = JUMP_URL.format(guild_id=guild.id, channel_id=target.id,
                              message_id=message_id)
        if old is None:
            await interaction.response.send_message(MENU_ADDED.format(
                emoji=render_key(key), url=url, role=role.name), ephemeral=True)
            return
        previous = guild.get_role(old["role_id"])
        await interaction.response.send_message(MENU_UPDATED.format(
            emoji=render_key(key), url=url, role=role.name,
            old=previous.name if previous else MENU_MISSING_ROLE), ephemeral=True)

    @rolemenu.command(name="remove",
                      description="Unmap a single emoji of a message")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(message="Message link or message ID",
                           emoji="Emoji that should stop granting a role")
    async def rolemenu_remove(self, interaction: discord.Interaction,
                              message: str, emoji: str) -> None:
        """Stop mapping `emoji` of `message` to a role."""
        message_id = parse_message_ref(message)[1]
        parsed = (parse_emoji(emoji)
                  or guild_emoji_by_name(interaction.guild, emoji))
        if parsed is None:
            raise RoleSetupError(EMOJI_INVALID.format(emoji=emoji))
        key = reaction_key(parsed)
        row = await get_reaction_role(message_id, key)
        if row is None or row["guild_id"] != interaction.guild.id:
            await interaction.response.send_message(
                MENU_NOT_MAPPED.format(emoji=render_key(key)), ephemeral=True)
            return
        await delete_reaction_role(message_id, key)
        await interaction.response.send_message(
            MENU_REMOVED.format(emoji=render_key(key), url=self.menu_url(row)),
            ephemeral=True)

    @rolemenu.command(name="clear",
                      description="Unmap a whole menu message")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(message="Message link or message ID")
    async def rolemenu_clear(self, interaction: discord.Interaction,
                             message: str) -> None:
        """Drop every mapping of `message`, the reactions stay untouched."""
        message_id = parse_message_ref(message)[1]
        menu = await get_reaction_menu(message_id)
        if not menu or menu[0]["guild_id"] != interaction.guild.id:
            await interaction.response.send_message(MENU_EMPTY, ephemeral=True)
            return
        url = self.menu_url(menu[0])
        count = await delete_reaction_menu(message_id)
        await interaction.response.send_message(
            MENU_CLEARED.format(count=count, url=url), ephemeral=True)

    @rolemenu.command(name="show",
                      description="Show the mapping of one menu message")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(message="Message link or message ID")
    async def rolemenu_show(self, interaction: discord.Interaction,
                            message: str) -> None:
        """List every emoji of `message` and the role behind it."""
        guild = interaction.guild
        message_id = parse_message_ref(message)[1]
        menu = await get_reaction_menu(message_id)
        if not menu or menu[0]["guild_id"] != guild.id:
            await interaction.response.send_message(MENU_EMPTY, ephemeral=True)
            return
        lines = [MENU_SHOW_TITLE.format(url=self.menu_url(menu[0]),
                                       count=len(menu))]
        lines += [f"{render_key(row['emoji'])} \u2192 "
                  f"{self.role_text(guild, row)}" for row in menu]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @rolemenu.command(name="list",
                      description="List every reaction role menu")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rolemenu_list(self, interaction: discord.Interaction) -> None:
        """Show every menu of this server with its emoji to role pairs."""
        guild = interaction.guild
        rows = await get_reaction_menus(guild.id)
        if not rows:
            await interaction.response.send_message(MENU_LIST_EMPTY,
                                                    ephemeral=True)
            return
        menus: dict[int, list] = {}
        for row in rows:
            menus.setdefault(row["message_id"], []).append(row)

        lines = [MENU_LIST_TITLE.format(guild=guild)]
        for index, menu in enumerate(menus.values()):
            if index >= MENU_LIST_LIMIT:
                lines.append(MENU_LIST_MORE.format(count=len(menus) - index))
                break
            options = "   ".join(self.option_text(guild, row) for row in menu)
            lines.append(MENU_LIST_LINE.format(
                channel_id=menu[0]["channel_id"], url=self.menu_url(menu[0]),
                options=options))
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @staticmethod
    def role_text(guild: discord.Guild, row) -> str:
        """The role of a stored mapping, even when it is gone by now."""
        role = guild.get_role(row["role_id"])
        return f"**{role.name}**" if role else MENU_MISSING_ROLE

    @staticmethod
    def option_text(guild: discord.Guild, row) -> str:
        """One ``emoji -> role`` pair as shown by `rolemenu list`."""
        role = guild.get_role(row["role_id"])
        target = role.name if role else MENU_MISSING_ROLE
        return f"{render_key(row['emoji'])} \u2192 {target}"

    @rolemenu.command(name="take",
                      description="Take a role away from a member")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(member="Member to revoke the role from",
                           role="Role to take away")
    async def rolemenu_take(self, interaction: discord.Interaction,
                            member: discord.Member,
                            role: discord.Role) -> None:
        """Remove `role` from `member`.

        Granting is add-only, so this is the way back: the reaction of the
        member is left alone and reacting again hands the role right back.
        """
        self.check_role(interaction, role)
        if role not in member.roles:
            await interaction.response.send_message(
                ROLE_NOT_HELD.format(member=member.mention, role=role.name),
                ephemeral=True)
            return
        try:
            await member.remove_roles(role, reason=REVOKE_REASON)
        except discord.HTTPException as error:
            raise RoleSetupError(REVOKE_FAILED.format(
                role=role.name, reason=http_reason(error))) from error
        self.log.info("%s took role %s from %s in guild %s",
                      interaction.user.id, role.id, member.id,
                      interaction.guild.id)
        await interaction.response.send_message(
            ROLE_TAKEN.format(role=role.name, member=member.mention),
            ephemeral=True)

    # ---------------------------------------------------------------- errors
    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError) -> None:
        """Handles the application command errors the tree forwards to the cog."""
        text = await self.error_text(unwrap_error(error))
        if text is None:
            raise error
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def error_text(self, error: BaseException) -> str | None:
        """Turn a command error into a message, None when it is unexpected."""
        if isinstance(error, RoleSetupError):
            return str(error)
        if isinstance(error, (commands.MissingPermissions, commands.CheckFailure,
                              app_commands.CheckFailure)):
            return PERMISSION_ERROR
        if isinstance(error, commands.MemberNotFound):
            return MEMBER_NOT_FOUND_ERROR
        if isinstance(error, commands.RoleNotFound):
            return ROLE_NOT_FOUND_ERROR
        if isinstance(error, commands.BadArgument):
            return f"Invalid argument: {error}"
        return None


def unwrap_error(error: BaseException) -> BaseException:
    """Peel CommandInvokeError/HybridCommandError wrappers off an error."""
    original = error
    for _ in range(5):
        inner = getattr(original, "original", None)
        if inner is None:
            break
        original = inner
    return original


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roles(bot))

