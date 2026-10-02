"""Offline checks for the command channel cog.

Run with:  python check_command_channels.py
(prints "ALL GOOD" and exits 0 when green)

Covers the rule that keeps the bot quiet outside the channels a server chose:
who is let through, what the private refusal of a command looks like and the
staff commands that edit the allow list. Everything runs against a throwaway
database in the temp folder, so the real bot.db is never touched.
"""

import asyncio
import os
import tempfile

import discord
from discord.ext import commands

import db
import cogs.command_channels as cc

DB_PATH = os.path.join(tempfile.gettempdir(), "check_command_channels.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
db.DB_PATH = DB_PATH

GUILD_ID = 1554266264149696573
OTHER_GUILD_ID = 4242
COMMANDS_ID = 1001        # the channel the server allows
CHAT_ID = 1002            # a channel the server did not allow
THREAD_ID = 1003          # a thread of the allowed channel
ORPHAN_ID = 1004          # a thread of the not allowed channel
USER_ID = 777
OWNER_ID = 555

failures = []


def check(name, got, want):
    if got == want:
        print(f"ok   {name}")
    else:
        failures.append(name)
        print(f"FAIL {name}: {got!r} != {want!r}")


class Resp:
    status = 404
    reason = "Not Found"
    text = '{"code": 10003, "message": "Unknown Channel"}'


class FakeChannel:
    def __init__(self, guild, channel_id, name):
        self.guild = guild
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"


class FakeThread(discord.Thread):
    """A real Thread subclass, so the isinstance check of the cog holds."""


def make_thread(guild, channel_id, name, parent_id):
    thread = FakeThread.__new__(FakeThread)
    thread.guild = guild
    thread.id = channel_id
    thread.name = name
    thread.parent_id = parent_id
    # `mention` is a read only property on Thread, so it is left alone
    return thread


class Perms:
    def __init__(self, administrator=False):
        self.administrator = administrator


class Member:
    def __init__(self, user_id=USER_ID, admin=False, dms=True):
        self.id = user_id
        self.display_name = "Tester"
        self.mention = f"<@{user_id}>"
        self.guild_permissions = Perms(administrator=admin)
        self.dms = dms
        self.dms_sent = []

    async def send(self, content=None, **kwargs):
        if not self.dms:
            raise discord.Forbidden(Resp(), "Cannot send messages to this user")
        self.dms_sent.append(content)


class Guild:
    id = GUILD_ID
    name = "Probe Guild"

    def __init__(self):
        self.commands = FakeChannel(self, COMMANDS_ID, "commands")
        self.chat = FakeChannel(self, CHAT_ID, "chat")
        self.thread = make_thread(self, THREAD_ID, "of-commands", COMMANDS_ID)
        self.orphan = make_thread(self, ORPHAN_ID, "of-chat", CHAT_ID)
        self.channels = {c.id: c for c in
                         (self.commands, self.chat, self.thread, self.orphan)}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        channel = self.channels.get(channel_id)
        if channel is None:
            raise discord.NotFound(Resp(), "Unknown Channel")
        return channel


class OtherGuild(Guild):
    """A second server, which never configured the rule."""

    id = OTHER_GUILD_ID


class Bot(commands.Bot):
    """An offline bot whose channels come from the probe guild."""

    def __init__(self, guild, owner_id=OWNER_ID):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.guild = guild
        # without it is_owner() would try to fetch the application info
        self.owner_ids = {owner_id}

    def get_channel(self, channel_id):
        return self.guild.get_channel(channel_id)

    async def fetch_channel(self, channel_id):
        return await self.guild.fetch_channel(channel_id)


class Response:
    def __init__(self):
        self.sent = []
        self.done = False

    async def send_message(self, content=None, *, embed=None,
                           ephemeral=False, **kwargs):
        self.sent.append((content, embed, ephemeral))
        self.done = True

    def is_done(self):
        return self.done


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class Interaction:
    def __init__(self, guild, channel, user, command=None):
        self.guild = guild
        self.channel = channel
        self.channel_id = channel.id if channel is not None else None
        self.user = user
        self.command = command
        self.response = Response()
        self.followup = Followup()


class PrefixCommand:
    def __init__(self, name="sync"):
        self.qualified_name = name


class TextCtx:
    def __init__(self, guild, channel, author, command=None):
        self.guild = guild
        self.channel = channel
        self.author = author
        self.command = command or PrefixCommand()
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


def last(ctx):
    content, embed, _ = ctx.response.sent[-1]
    return embed if embed is not None else content


def was_ephemeral(ctx):
    return ctx.response.sent[-1][2]


async def main():
    await db.init_db()
    guild = Guild()
    bot = Bot(guild)
    cog = cc.CommandChannels(bot)
    await bot.add_cog(cog)
    await asyncio.sleep(0)  # let the cog_load task install the global checks

    # the two global checks are in place, and gone again after a reload
    check("slash tree hook installed",
          "interaction_check" in bot.tree.__dict__, True)
    check("prefix check installed", cog.text_command_check in bot._checks, True)

    # -------------------------------------------------------- the plain object
    off = cc.CommandChannelSettings()
    check("rule off when empty", off.enabled, False)
    check("empty list allows all", off.allows(CHAT_ID), True)
    check("empty list allows None", off.allows(None), True)
    on = cc.CommandChannelSettings({COMMANDS_ID})
    check("rule on once set", on.enabled, True)
    check("allowed channel", on.allows(COMMANDS_ID), True)
    check("other channel blocked", on.allows(CHAT_ID), False)
    check("thread of allowed channel", on.allows(THREAD_ID, COMMANDS_ID), True)
    check("thread of other channel", on.allows(ORPHAN_ID, CHAT_ID), False)
    check("unknown channel blocked", on.allows(None), False)

    # -------------------------------------------------------- nothing configured
    member = Member()
    admin = Member(USER_ID + 1, admin=True)
    owner = Member(OWNER_ID)
    ctx = Interaction(guild, guild.chat, member)

    check("no channels, nothing blocked",
          await cog.interaction_check(ctx), True)
    check("no channels, no message", len(ctx.response.sent), 0)
    check("no channels, no DM", len(member.dms_sent), 0)

    # ------------------------------------------------------------ /botchannels
    staff = Interaction(guild, guild.chat, admin)
    await cog.botchannels_add.callback(cog, staff, guild.commands)
    check("channel added", last(staff), cc.CHANNEL_ADDED.format(
        channel=guild.commands.mention))
    check("added answer is private", was_ephemeral(staff), True)
    check("stored", await db.list_command_channels(GUILD_ID), {COMMANDS_ID})
    check("other guild untouched",
          await db.list_command_channels(OTHER_GUILD_ID), set())

    await cog.botchannels_add.callback(cog, staff, guild.commands)
    check("adding twice", last(staff), cc.CHANNEL_ALREADY_ADDED.format(
        channel=guild.commands.mention))

    # the rule is on for this server now, but a second one starts empty
    fresh = Interaction(OtherGuild(), guild.chat, member)
    check("another server stays open",
          await cog.interaction_check(fresh), True)

    # --------------------------------------------------------- the slash gate
    allowed = Interaction(guild, guild.commands, member)
    check("allowed channel passes", await cog.interaction_check(allowed), True)
    check("allowed channel is quiet", len(allowed.response.sent), 0)

    blocked = Interaction(guild, guild.chat, member)
    check("other channel refused", await cog.interaction_check(blocked), False)
    check("refusal is private", was_ephemeral(blocked), True)
    check("refusal names the channel", cc.CHANNEL_TARGET.format(
        target_id=COMMANDS_ID) in blocked.response.sent[-1][0], True)
    check("refusal stays out of the channel", blocked.response.sent[0][1], None)

    # the people who skip the rule
    admin_ctx = Interaction(guild, guild.chat, admin)
    check("administrator passes", await cog.interaction_check(admin_ctx), True)
    check("administrator hears nothing", len(admin_ctx.response.sent), 0)
    owner_ctx = Interaction(guild, guild.chat, owner)
    check("owner passes", await cog.interaction_check(owner_ctx), True)

    # a failing owner lookup must not take every command down with it
    class BrokenOwnerBot:
        async def is_owner(self, user):
            raise discord.HTTPException(Resp(), "gateway is busy")

    lonely = cc.CommandChannels(BrokenOwnerBot())
    lonely.log.disabled = True
    check("owner lookup failure survives",
          await lonely.is_owner(owner), False)
    lonely.log.disabled = False

    # a thread is judged by its parent channel
    thread_ok = Interaction(guild, guild.thread, member)
    check("thread of allowed channel", await cog.interaction_check(thread_ok),
          True)
    thread_bad = Interaction(guild, guild.orphan, member)
    check("thread of other channel", await cog.interaction_check(thread_bad),
          False)

    # a direct message is not a channel of the server
    dm = Interaction(None, None, member)
    check("direct message passes", await cog.interaction_check(dm), True)

    # the command that configures the rule is never gated
    group = bot.tree.get_command(cc.MANAGEMENT_GROUP)
    manage_ctx = Interaction(guild, guild.chat, member,
                             command=group.get_command("list"))
    check("the rule bypasses itself",
          await cog.interaction_check(manage_ctx), True)
    check("root command name read",
          cc.root_command_name(manage_ctx), cc.MANAGEMENT_GROUP)
    check("unknown command name",
          cc.root_command_name(Interaction(guild, guild.chat, member)), None)


    # ---------------------------------------------------------- the prefix gate
    prefix_ok = TextCtx(guild, guild.commands, member)
    check("prefix in allowed channel",
          await cog.text_command_check(prefix_ok), True)
    prefix_dm = TextCtx(None, None, member)
    check("prefix in a direct message",
          await cog.text_command_check(prefix_dm), True)

    prefix_bad = TextCtx(guild, guild.chat, member)
    text = None
    try:
        await cog.text_command_check(prefix_bad)
        check("prefix in another channel", "no error", "ChannelNotAllowed")
    except cc.ChannelNotAllowed as error:
        text = str(error)
        check("prefix in another channel", True, True)
        check("prefix error names the channel",
              cc.CHANNEL_TARGET.format(target_id=COMMANDS_ID) in text, True)
        check("prefix error explains the DM", cc.DM_SENTINEL in text, True)

    check("prefix as administrator",
          await cog.text_command_check(
              TextCtx(guild, guild.chat, admin)), True)
    check("prefix as owner",
          await cog.text_command_check(
              TextCtx(guild, guild.chat, owner)), True)
    check("prefix of the rule itself",
          await cog.text_command_check(TextCtx(
              guild, guild.chat, member, PrefixCommand("botchannels add"))),
          True)

    # the refusal of a prefix command is a direct message, not a channel post
    await cog.on_command_error(prefix_bad, cc.ChannelNotAllowed(text))
    check("prefix refusal goes to the author", len(member.dms_sent), 1)
    check("prefix refusal stays out of the channel", len(prefix_bad.sent), 0)

    # with the direct messages closed the warning is posted and removed again
    silent = Member(USER_ID + 2, dms=False)
    closed = TextCtx(guild, guild.chat, silent)
    await cog.on_command_error(closed, cc.ChannelNotAllowed(text))
    check("closed DMs fall back to the channel", len(closed.sent), 1)
    check("closed DMs message is removed",
          closed.sent[0][1].get("delete_after"), cc.ERROR_DELETE_AFTER)

    # an error of somebody else is not ours to answer, but it is still logged:
    # this listener replaces the default one that used to do that
    foreign = TextCtx(guild, guild.chat, member)
    cog.log.disabled = True
    await cog.on_command_error(foreign, commands.MissingPermissions("nope"))
    cog.log.disabled = False
    check("foreign error answered here", len(member.dms_sent), 1)

    # ------------------------------------------------------- more staff commands
    await cog.botchannels_add.callback(cog, staff, guild.chat)
    shown = Interaction(guild, guild.commands, admin)
    await cog.botchannels_list.callback(cog, shown)
    embed = last(shown)
    check("list title", embed.title, cc.GATE_TITLE.format(guild=guild.name))
    check("list is private", was_ephemeral(shown), True)
    check("list shows both channels", embed.fields[0].value.split("\n"), [
        cc.CHANNEL_TARGET.format(target_id=COMMANDS_ID),
        cc.CHANNEL_TARGET.format(target_id=CHAT_ID),
    ])

    # a long list is trimmed for one field: 2 chosen plus 10 more is 12
    for extra in range(3000, 3010):
        await db.add_command_channel(GUILD_ID, extra)
    crowded = Interaction(guild, guild.commands, admin)
    await cog.botchannels_list.callback(cog, crowded)
    value = last(crowded).fields[0].value
    check("list trimmed to the limit", len(value.split("\n")),
          cc.LIST_FIELD_LIMIT + 1)
    check("list says how many are left",
          cc.SETTINGS_MORE.format(count=3) in value, True)

    await cog.botchannels_remove.callback(cog, staff, guild.commands)
    check("channel removed", last(staff), cc.CHANNEL_REMOVED.format(
        channel=guild.commands.mention))
    await cog.botchannels_remove.callback(cog, staff, guild.commands)
    check("removing twice", last(staff), cc.CHANNEL_NOT_REMOVED.format(
        channel=guild.commands.mention))

    await cog.botchannels_clear.callback(cog, staff)
    check("list cleared", last(staff), cc.CHANNELS_CLEARED.format(count=11))
    check("nothing stored", await db.list_command_channels(GUILD_ID), set())
    check("rule off again",
          await cog.interaction_check(Interaction(guild, guild.chat, member)),
          True)
    await cog.botchannels_clear.callback(cog, staff)
    check("clearing twice", last(staff), cc.CHANNELS_NOTHING_TO_CLEAR)

    # -------------------------------------------------------------- clean ups
    await db.add_command_channel(GUILD_ID, COMMANDS_ID)
    await db.add_command_channel(GUILD_ID, THREAD_ID)
    await cog.on_guild_channel_delete(guild.commands)
    check("deleted channel dropped",
          await db.list_command_channels(GUILD_ID), {THREAD_ID})
    await cog.on_thread_delete(guild.thread)
    check("deleted thread dropped",
          await db.list_command_channels(GUILD_ID), set())
    await db.add_command_channel(GUILD_ID, COMMANDS_ID)

    # -------------------------------------------------------------- slash tree
    names = sorted(command.name for command in group.walk_commands())
    check("slash subcommands", names, ["add", "clear", "list", "remove"])
    add = group.get_command("add")
    params = {param.name: param for param in add.parameters}
    check("add takes a channel", sorted(params), ["channel"])
    check("add channel is required", params["channel"].required, True)
    types = [t.value for t in params["channel"].channel_types]
    check("add takes a text channel",
          discord.ChannelType.text.value in types, True)
    check("add takes a thread",
          discord.ChannelType.public_thread.value in types, True)
    check("add takes no voice channel",
          discord.ChannelType.voice.value in types, False)
    check("list takes no argument",
          list(group.get_command("list").parameters), [])
    check("group is guild only", group.guild_only, True)
    check("group needs manage guild",
          group.default_permissions.manage_guild, True)

    # a reload takes both checks away again
    await bot.remove_cog("CommandChannels")
    check("slash tree hook removed",
          "interaction_check" in bot.tree.__dict__, False)
    check("prefix check removed", cog.text_command_check in bot._checks, False)

    print("\n" + ("ALL GOOD" if not failures
                  else f"FAILURES ({len(failures)}):\n- "
                       + "\n- ".join(failures)))
    os.remove(DB_PATH)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
