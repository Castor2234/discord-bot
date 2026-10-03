"""Offline checks for the greetings cog.

Run with:  python check_greetings.py   (prints "ALL GOOD" and exits 0 when green)

Covers what a server configures and what members end up seeing: a server that
never picked a channel stays quiet, the two switches work on their own, bots
are skipped, and a deleted channel is forgotten instead of leaving a dangling
id behind. Everything runs against a throwaway database in the temp folder, so
the real bot.db is never touched.
"""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

import db
import cogs.greetings as gr

DB_PATH = os.path.join(tempfile.gettempdir(), "check_greetings.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
db.DB_PATH = DB_PATH

GUILD_ID = 1554266264149696573
OTHER_GUILD_ID = 4242
GREET_ID = 1001       # the channel the server picks
CHAT_ID = 1002        # a channel nobody picked
LOCKED_ID = 1003      # a channel the bot cannot post in
THREAD_ID = 1004
USER_ID = 777

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


class Perms:
    def __init__(self, view=True, send=True, threads=True):
        self.view_channel = view
        self.send_messages = send
        self.send_messages_in_threads = threads


class FakeText(discord.abc.Messageable):
    def __init__(self, guild, channel_id, name, perms=None):
        # Messageable.__init__ wants gateway internals, so the two attributes
        # it would set are filled in by hand instead
        self._state = None
        self._get_channel = lambda: self
        self.guild = guild
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self._perms = perms or Perms()
        self.sent = []

    def permissions_for(self, member):
        return self._perms

    async def send(self, content=None, **kwargs):
        self.sent.append(kwargs.get("embed", content))


class FakeThread(discord.Thread):
    """A real Thread subclass, so the isinstance check of the cog holds."""

    async def send(self, content=None, **kwargs):
        self.sent.append(kwargs.get("embed", content))

    def permissions_for(self, member):
        return self._perms


def make_thread(guild, channel_id, name, perms=None):
    thread = FakeThread.__new__(FakeThread)
    thread.guild = guild
    thread.id = channel_id
    thread.name = name
    thread._perms = perms or Perms()
    thread.sent = []
    return thread


class Avatar:
    url = "https://example.invalid/avatar.png"


class Member:
    def __init__(self, guild=None, user_id=USER_ID, bot=False, age_days=30):
        self.id = user_id
        self.guild = guild
        self.display_name = "Tester"
        self.mention = f"<@{user_id}>"
        self.display_avatar = Avatar()
        self.bot = bot
        self.created_at = datetime.now(timezone.utc) - timedelta(days=age_days)


class Guild:
    id = GUILD_ID
    name = "Probe Guild"
    member_count = 42

    def __init__(self):
        self.greet = FakeText(self, GREET_ID, "greetings")
        self.chat = FakeText(self, CHAT_ID, "general")
        self.locked = FakeText(self, LOCKED_ID, "locked",
                               Perms(view=False, send=False))
        self.thread = make_thread(self, THREAD_ID, "of-greetings")
        self.channels = {c.id: c for c in
                         (self.greet, self.chat, self.locked, self.thread)}
        self.me = Member(self)

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        channel = self.channels.get(channel_id)
        if channel is None:
            raise discord.NotFound(Resp(), "Unknown Channel")
        return channel


class Response:
    def __init__(self):
        self.sent = []
        self.done = False

    async def send_message(self, content=None, *, embed=None,
                           ephemeral=False, **kwargs):
        self.sent.append(content if embed is None else embed)
        self.done = True

    def is_done(self):
        return self.done


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class Interaction:
    def __init__(self, guild, user):
        self.guild = guild
        self.user = user
        self.response = Response()
        self.followup = Followup()


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())


def last(ctx):
    return ctx.response.sent[-1]


async def main():
    await db.init_db()
    bot = Bot()
    cog = gr.Greetings(bot)
    guild = Guild()
    member = Member(guild)
    ctx = Interaction(guild, member)

    # ------------------------------------------------ a server that never ran
    # the command: no channel, so nothing is ever sent
    settings = await cog.settings_for(GUILD_ID)
    check("default channel", settings.channel_id, None)
    check("default join switch", settings.greet_join, False)
    check("default leave switch", settings.greet_leave, False)
    check("default not enabled", settings.enabled, False)
    check("default has no target", cog.greeting_channel(guild, settings), None)

    await cog.on_member_join(member)
    await cog.on_member_remove(member)
    check("nothing sent without a channel",
          (len(guild.greet.sent), len(guild.chat.sent)), (0, 0))

    # ---------------------------------------------------------- the switches
    # off, so the join above was already silent and can be seen as such
    await cog.greetings_join.callback(cog, ctx, False)
    check("join off stored",
          (await cog.settings_for(GUILD_ID)).greet_join, False)
    await cog.on_member_join(member)
    check("join off sends nothing", len(guild.chat.sent), 0)

    # a switch turned on without a channel still explains that nothing is sent
    await cog.greetings_join.callback(cog, ctx, True)
    check("switch on without a channel", last(ctx), gr.SWITCH_ON_NO_CHANNEL)

    # ----------------------------------------------------- /greetings channel
    await cog.greetings_channel.callback(cog, ctx, guild.greet)
    check("channel answer", last(ctx),
          gr.CHANNEL_SET.format(channel=guild.greet.mention))
    settings = await cog.settings_for(GUILD_ID)
    check("channel stored", settings.channel_id, GREET_ID)
    check("setting a channel turns both on",
          (settings.greet_join, settings.greet_leave), (True, True))
    check("now enabled", settings.enabled, True)
    check("target found", cog.greeting_channel(guild, settings), guild.greet)

    # a channel the bot cannot post in is refused before its id is stored
    try:
        await cog.greetings_channel.callback(cog, ctx, guild.locked)
        check("locked channel refused", "no error", "an error")
    except gr.GreetingConfigError as error:
        check("locked channel refused", gr.CHANNEL_NO_ACCESS.format(
            channel=guild.locked.mention,
            perms="*View Channel*, *Send Messages*"), str(error))
    check("locked channel not stored",
          (await cog.settings_for(GUILD_ID)).channel_id, GREET_ID)

    # ------------------------------------------------------- the join / leave
    await cog.on_member_join(member)
    check("welcome posted", len(guild.greet.sent), 1)
    embed = guild.greet.sent[-1]
    check("welcome title", embed.title, gr.WELCOME_TITLE.format(
        guild=guild.name, member=member.display_name))
    check("welcome has the member count",
          any(f.name == gr.MEMBER_COUNT_FIELD and f.value == "42"
              for f in embed.fields), True)

    await cog.on_member_remove(member)
    check("goodbye posted", len(guild.greet.sent), 2)
    check("goodbye title", guild.greet.sent[-1].title, gr.FAREWELL_TITLE.format(
        guild=guild.name, member=member.display_name))

    # a bot is skipped by default
    bot_member = Member(guild, user_id=USER_ID + 1, bot=True)
    await cog.on_member_join(bot_member)
    await cog.on_member_remove(bot_member)
    check("bots skipped", len(guild.greet.sent), 2)

    # turning one switch off leaves the other one alone
    await cog.greetings_join.callback(cog, ctx, False)
    await cog.on_member_join(member)
    check("join off after a channel", len(guild.greet.sent), 2)
    check("join off answer", last(ctx), gr.SWITCH_JOIN_OFF)
    check("leave switch untouched",
          (await cog.settings_for(GUILD_ID)).greet_leave, True)
    await cog.on_member_remove(member)
    check("leave still on", len(guild.greet.sent), 3)

    await cog.greetings_leave.callback(cog, ctx, True)
    check("leave on answer", last(ctx),
          gr.SWITCH_LEAVE_ON.format(channel=guild.greet.mention))

    # ----------------------------------------------------------- the preview
    await cog.greetings_test.callback(cog, ctx, False)
    check("preview posted", len(guild.greet.sent), 4)
    check("preview marked", guild.greet.sent[-1].footer.text, gr.PREVIEW_JOIN)
    await cog.greetings_test.callback(cog, ctx, True)
    check("leave preview marked", guild.greet.sent[-1].footer.text,
          gr.PREVIEW_LEAVE)

    # ---------------------------------------------------- /greetings settings
    await cog.greetings_settings.callback(cog, ctx)
    embed = last(ctx)
    check("settings shows the channel",
          any(f.name == gr.SETTINGS_CHANNEL_FIELD
              and f.value == guild.greet.mention for f in embed.fields), True)
    check("settings shows join off",
          any(f.name == gr.SETTINGS_JOIN_FIELD and f.value == gr.SETTINGS_NO
              for f in embed.fields), True)
    check("settings shows leave on",
          any(f.name == gr.SETTINGS_LEAVE_FIELD and f.value == gr.SETTINGS_YES
              for f in embed.fields), True)

    # ------------------------------------------------------------- resetting
    await cog.greetings_channel.callback(cog, ctx, None)
    check("reset answer", last(ctx), gr.CHANNEL_CLEARED)
    settings = await cog.settings_for(GUILD_ID)
    check("reset stored", settings.channel_id, None)
    check("reset drops the switches",
          (settings.greet_join, settings.greet_leave), (False, False))
    await cog.on_member_join(member)
    await cog.on_member_remove(member)
    check("quiet again", len(guild.greet.sent), 5)

    # a switch may still be turned on before a channel is chosen
    await cog.greetings_join.callback(cog, ctx, True)
    check("join on without a channel",
          (await cog.settings_for(GUILD_ID)).greet_join, True)
    check("but nothing is posted", len(guild.chat.sent), 0)

    # --------------------------------------------------------- clean up paths
    await cog.greetings_channel.callback(cog, ctx, guild.greet)
    await cog.greetings_leave.callback(cog, ctx, True)
    await cog.on_guild_channel_delete(guild.greet)
    settings = await cog.settings_for(GUILD_ID)
    check("deleted channel dropped", settings.channel_id, None)
    check("deleted channel drops the switches",
          (settings.greet_join, settings.greet_leave), (False, False))

    # a deleted thread leaves the greeting channel alone
    await cog.greetings_channel.callback(cog, ctx, guild.greet)
    await cog.on_thread_delete(guild.thread)
    check("unrelated thread left the channel",
          (await cog.settings_for(GUILD_ID)).channel_id, GREET_ID)

    # a channel the bot can no longer read is pruned when the settings are shown
    del guild.channels[GREET_ID]
    ctx.response.sent.clear()
    await cog.greetings_settings.callback(cog, ctx)
    check("dead channel shown as missing",
          any(gr.MISSING_CHANNEL.format(channel_id=GREET_ID) in f.value
              for f in last(ctx).fields), True)
    check("dead channel pruned",
          (await cog.settings_for(GUILD_ID)).channel_id, None)

    # a second server starts from the defaults
    other = await cog.settings_for(OTHER_GUILD_ID)
    check("other server channel", other.channel_id, None)
    check("other server switches",
          (other.greet_join, other.greet_leave), (False, False))

    # ----------------------------------------------------------- the slash tree
    await bot.add_cog(cog)
    group = bot.tree.get_command("greetings")
    names = sorted(command.name for command in group.walk_commands())
    check("slash subcommands", names,
          ["channel", "join", "leave", "settings", "test"])
    channel_cmd = group.get_command("channel")
    params = {param.name: param for param in channel_cmd.parameters}
    check("channel param optional", params["channel"].required, False)
    join = group.get_command("join")
    jparam = {param.name: param for param in join.parameters}
    check("join takes the switch", list(jparam), ["enabled"])
    check("switch required", jparam["enabled"].required, True)
    settings_cmd = group.get_command("settings")
    check("settings takes no argument", list(settings_cmd.parameters), [])

    print("\n" + ("ALL GOOD" if not failures
                  else f"FAILURES ({len(failures)}):\n- "
                       + "\n- ".join(failures)))
    os.remove(DB_PATH)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
