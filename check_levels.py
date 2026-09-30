"""Offline checks for the level cog's per server settings.

Run with:  python check_levels.py   (prints "ALL GOOD" and exits 0 when green)

Covers the settings built on top of the XP loop: where a level up gets
announced, which channels and roles earn nothing, and how the lists clean
themselves up when a channel or a role is deleted. Everything runs against a
throwaway database in the temp folder, so the real bot.db is never touched.
"""

import asyncio
import os
import tempfile

import discord
from discord.ext import commands

import db
import cogs.levels as lv

DB_PATH = os.path.join(tempfile.gettempdir(), "check_levels.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
db.DB_PATH = DB_PATH

GUILD_ID = 1554266264149696573
TEXT_ID = 1001
VOICE_ID = 1002
THREAD_ID = 1003
ANNOUNCE_ID = 1004
DEAD_ID = 1099
IGNORED_ROLE_ID = 866824826077446204
MOD_ROLE_ID = 2002
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
        self.sent.append(content)


class FakeVoice:
    def __init__(self, guild, channel_id, name, perms=None):
        self.guild = guild
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self._perms = perms or Perms()

    def permissions_for(self, member):
        return self._perms


class FakeThread(discord.Thread):
    """A real Thread subclass, so the isinstance check of the cog holds."""

    async def send(self, content=None, **kwargs):
        self.sent.append(content)

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


class Role:
    def __init__(self, role_id, name, guild=None):
        self.id = role_id
        self.name = name
        self.guild = guild

    def is_default(self):
        return self.id == GUILD_ID


class Member:
    def __init__(self, roles=(), bot=False):
        self.id = USER_ID
        self.roles = list(roles)
        self.mention = f"<@{USER_ID}>"
        self.display_name = "Tester"
        self.bot = bot


class Guild:
    id = GUILD_ID
    name = "Probe Guild"

    def __init__(self):
        self.text = FakeText(self, TEXT_ID, "general")
        self.voice = FakeVoice(self, VOICE_ID, "voice")
        self.thread = make_thread(self, THREAD_ID, "thread")
        self.announce = FakeText(self, ANNOUNCE_ID, "level-ups")
        self.locked = FakeText(self, 1005, "locked",
                               Perms(view=False, send=False))
        self.channels = {c.id: c for c in
                         (self.text, self.voice, self.thread, self.announce,
                          self.locked)}
        self.roles = {MOD_ROLE_ID: Role(MOD_ROLE_ID, "mod", self),
                      IGNORED_ROLE_ID: Role(IGNORED_ROLE_ID, "muted", self)}
        self.me = Member()
        self.system_channel = self.text
        self.afk_channel = None

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        channel = self.channels.get(channel_id)
        if channel is None:
            raise discord.NotFound(Resp(), "Unknown Channel")
        return channel

    def get_role(self, role_id):
        return self.roles.get(role_id)


class Ctx:
    def __init__(self, guild, author):
        self.guild = guild
        self.author = author
        self.clean_prefix = "!"
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs.get("embed")))


class Message:
    def __init__(self, guild, channel, author):
        self.guild = guild
        self.channel = channel
        self.author = author


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())


def last(ctx):
    content, embed = ctx.sent[-1]
    return embed if embed is not None else content


async def main():
    await db.init_db()
    bot = Bot()
    cog = lv.Levels(bot)
    guild = Guild()
    author = Member()
    ctx = Ctx(guild, author)

    # the settings of a server nobody configured yet
    settings = await cog.settings_for(GUILD_ID)
    check("default channel", settings.level_up_channel_id,
          lv.DEFAULT_LEVEL_UP_CHANNEL_ID)
    check("default everywhere", settings.level_up_everywhere, False)
    check("default ignored roles", settings.ignored_roles,
          set(lv.DEFAULT_IGNORED_ROLE_IDS))
    check("default ignored channels", settings.ignored_channels, set())
    check("blocks unknown channel", settings.blocks_channel(TEXT_ID), False)
    check("blocks none", settings.blocks_channel(None), False)
    check("blocks member without role", settings.blocks_member(
        Member([Role(GUILD_ID, "@everyone")])), False)
    check("blocks member with role",
          settings.blocks_member(Member([Role(IGNORED_ROLE_ID, "muted")])),
          True)

    # where an announcement goes
    check("keeps text channel", cog.level_up_channel(guild, settings,
                                                     guild.text), guild.text)
    check("voice goes to system channel", cog.level_up_channel(guild, settings),
          guild.system_channel)
    every = lv.GuildLevelSettings(level_up_channel_id=ANNOUNCE_ID,
                                  level_up_everywhere=True)
    check("everywhere wins", cog.level_up_channel(guild, every, guild.text),
          guild.announce)
    check("missing channel falls back", cog.level_up_channel(
        guild, lv.GuildLevelSettings(level_up_channel_id=DEAD_ID), None),
        guild.system_channel)

    # /levels levelup #level-ups everywhere:True
    await cog.levels_levelup.callback(cog, ctx, guild.announce, True)
    check("levelup answer",
          last(ctx).startswith(lv.LEVEL_UP_CHANNEL_SET.format(
              channel="<#1004>")), True)
    stored = await cog.load_settings(GUILD_ID)
    check("levelup stored", stored.level_up_channel_id, ANNOUNCE_ID)
    check("everywhere stored", stored.level_up_everywhere, True)

    # a channel the bot cannot write in is refused
    try:
        await cog.levels_levelup.callback(cog, ctx, guild.locked, False)
        check("locked channel refused", "no error", "LevelConfigError")
    except lv.LevelConfigError as exc:
        check("locked channel refused", "View Channel" in str(exc), True)
    check("locked channel not stored",
          (await cog.load_settings(GUILD_ID)).level_up_channel_id, ANNOUNCE_ID)

    # a thread works as announcement channel too
    await cog.levels_levelup.callback(cog, ctx, guild.thread, False)
    check("thread accepted",
          (await cog.load_settings(GUILD_ID)).level_up_channel_id, THREAD_ID)
    check("thread everywhere off",
          (await cog.load_settings(GUILD_ID)).level_up_everywhere, False)
    await cog.levels_levelup.callback(cog, ctx, guild.announce, True)

    # ignoring the announcement channel warns about it
    await cog.levels_ignorechannel.callback(cog, ctx, guild.announce)
    check("ignore warn",
          lv.CHANNEL_IS_LEVEL_UP.format(channel="<#1004>") in last(ctx), True)
    await cog.levels_ignorechannel.callback(cog, ctx, guild.announce)
    check("ignore twice", last(ctx),
          lv.CHANNEL_ALREADY_IGNORED.format(channel="<#1004>"))
    await cog.levels_unignorechannel.callback(cog, ctx, guild.announce)
    check("unignore", last(ctx),
          lv.CHANNEL_UNIGNORED.format(channel="<#1004>"))
    await cog.levels_unignorechannel.callback(cog, ctx, guild.announce)
    check("unignore twice", last(ctx),
          lv.CHANNEL_NOT_IGNORED.format(channel="<#1004>"))

    # voice channels go on the list as well
    await cog.levels_ignorechannel.callback(cog, ctx, guild.voice)
    stored = await cog.load_settings(GUILD_ID)
    check("voice ignored", stored.ignored_channels, {VOICE_ID})
    check("voice xp stopped", cog.voice_channel_allowed(guild, guild.voice,
                                                        stored), False)
    # roles
    try:
        await cog.levels_ignorerole.callback(cog, ctx, Role(GUILD_ID, "@everyone"))
        check("everyone refused", "no error", "LevelConfigError")
    except lv.LevelConfigError as exc:
        # CommandError escapes @everyone mentions, so compare the same way
        check("everyone refused", str(exc),
              lv.ROLE_EVERYONE.replace("@everyone", "@\u200beveryone"))
    await cog.levels_ignorerole.callback(cog, ctx, guild.roles[MOD_ROLE_ID])
    check("role ignored", (await cog.load_settings(GUILD_ID)).ignored_roles,
          set(lv.DEFAULT_IGNORED_ROLE_IDS) | {MOD_ROLE_ID})
    await cog.levels_ignorerole.callback(cog, ctx, guild.roles[MOD_ROLE_ID])
    check("role ignored twice", last(ctx),
          lv.ROLE_ALREADY_IGNORED.format(role="mod"))
    await cog.levels_unignorerole.callback(cog, ctx, guild.roles[MOD_ROLE_ID])
    check("role unignored", (await cog.load_settings(GUILD_ID)).ignored_roles,
          set(lv.DEFAULT_IGNORED_ROLE_IDS))
    await cog.levels_unignorerole.callback(cog, ctx, guild.roles[MOD_ROLE_ID])
    check("role unignore twice", last(ctx),
          lv.ROLE_NOT_IGNORED.format(role="mod"))

    # the XP gate: ignored channel, ignored role, then a normal message
    await cog.levels_ignorechannel.callback(cog, ctx, guild.text)
    await cog.on_message(Message(guild, guild.text,
                                 Member([guild.roles[MOD_ROLE_ID]])))
    check("no xp in ignored channel",
          (await db.get_user(GUILD_ID, USER_ID))["xp"], 0)
    await db.remove_ignored_channel(GUILD_ID, TEXT_ID)
    cog.forget_settings(GUILD_ID)
    await cog.on_message(Message(guild, guild.text,
                                 Member([guild.roles[IGNORED_ROLE_ID]])))
    check("no xp for ignored role",
          (await db.get_user(GUILD_ID, USER_ID))["xp"], 0)
    await cog.on_message(Message(guild, guild.text, Member()))
    xp = (await db.get_user(GUILD_ID, USER_ID))["xp"]
    check("xp in normal channel", lv.XP_MIN <= xp <= lv.XP_MAX, True)
    # still no level up message: 25 XP is far from level 1
    check("no announcement yet", guild.text.sent, [])
    # the "everywhere" switch is on, so even a text level up goes to #level-ups
    await cog.announce_level_up(guild, Member(), 1, guild.text)
    check("everywhere announcement",
          (len(guild.text.sent), len(guild.announce.sent)), (0, 1))
    # switch it off again: announcements stay where the XP happened
    await cog.levels_levelup.callback(cog, ctx, guild.announce, False)
    await cog.announce_level_up(guild, Member(), 2, guild.text)
    check("announcement stays where xp was earned", len(guild.text.sent), 1)
    await cog.announce_level_up(guild, Member(), 3, None)
    check("voice announcement redirected", len(guild.announce.sent), 2)

    # the voice gate reads the same list
    stored = await cog.load_settings(GUILD_ID)
    check("ignored voice stopped",
          cog.voice_channel_allowed(guild, guild.voice, stored), False)
    await cog.levels_unignorechannel.callback(cog, ctx, guild.voice)
    stored = await cog.load_settings(GUILD_ID)
    check("voice allowed again",
          cog.voice_channel_allowed(guild, guild.voice, stored), True)
    # put it back on the list, the settings view below expects it there
    await cog.levels_ignorechannel.callback(cog, ctx, guild.voice)
    # /levels settings, with a deleted channel and a deleted role inside
    await db.add_ignored_channel(GUILD_ID, DEAD_ID)
    await db.add_ignored_role(GUILD_ID, 9999)
    await cog.levels_settings.callback(cog, ctx)
    values = {field.name: field.value for field in last(ctx).fields}
    check("settings channel field", values[lv.SETTINGS_CHANNEL_FIELD],
          guild.announce.mention)
    check("settings everywhere field", values[lv.SETTINGS_EVERYWHERE_FIELD],
          lv.SETTINGS_NO)
    check("settings lists voice", values[lv.SETTINGS_CHANNELS_FIELD],
          lv.CHANNEL_TARGET.format(target_id=VOICE_ID))
    check("settings lists default role", values[lv.SETTINGS_ROLES_FIELD],
          lv.ROLE_TARGET.format(target_id=IGNORED_ROLE_ID))
    check("dead channel pruned", await db.list_ignored_channels(GUILD_ID),
          {VOICE_ID})
    check("dead role pruned", await db.list_ignored_roles(GUILD_ID),
          set(lv.DEFAULT_IGNORED_ROLE_IDS))

    # a level up channel that got deleted is cleared by the settings view
    await db.set_level_up_channel(GUILD_ID, DEAD_ID)
    cog.forget_settings(GUILD_ID)
    await cog.levels_settings.callback(cog, ctx)
    values = {field.name: field.value for field in last(ctx).fields}
    check("deleted channel reported", values[lv.SETTINGS_CHANNEL_FIELD],
          lv.MISSING_CHANNEL.format(channel_id=DEAD_ID))
    check("deleted channel cleared",
          (await cog.load_settings(GUILD_ID)).level_up_channel_id, None)

    # long lists are trimmed
    many = frozenset(range(1, 15))
    check("list trimmed",
          lv.Levels.render_list(many, lv.CHANNEL_TARGET).count("\n"), 9)
    check("list trimmed tail",
          lv.Levels.render_list(many, lv.CHANNEL_TARGET).endswith(
              lv.SETTINGS_MORE.format(count=5)), True)
    check("empty list", lv.Levels.render_list(frozenset(), lv.CHANNEL_TARGET),
          lv.SETTINGS_EMPTY)

    # resetting the announcement channel
    await cog.levels_levelup.callback(cog, ctx, None, False)
    check("reset answer", last(ctx), lv.LEVEL_UP_CHANNEL_CLEARED)
    check("reset stored",
          (await cog.load_settings(GUILD_ID)).level_up_channel_id, None)

    # "everywhere" without a channel can only explain itself and stay off
    await cog.levels_levelup.callback(cog, ctx, None, True)
    check("reset with everywhere warns",
          last(ctx),
          lv.LEVEL_UP_CHANNEL_CLEARED + lv.LEVEL_UP_EVERYWHERE_NEEDS_CHANNEL)
    check("everywhere dropped",
          (await cog.load_settings(GUILD_ID)).level_up_everywhere, False)

    # the clean up listeners
    await db.add_ignored_channel(GUILD_ID, VOICE_ID)
    await db.set_level_up_channel(GUILD_ID, ANNOUNCE_ID)
    await db.add_ignored_role(GUILD_ID, MOD_ROLE_ID)
    await cog.on_guild_channel_delete(guild.voice)
    await cog.on_thread_delete(guild.thread)
    await cog.on_guild_role_delete(guild.roles[MOD_ROLE_ID])
    stored = await cog.load_settings(GUILD_ID)
    check("deleted voice off the ignore list", stored.ignored_channels, set())
    check("announcement channel untouched", stored.level_up_channel_id,
          ANNOUNCE_ID)
    check("deleted role dropped", MOD_ROLE_ID in stored.ignored_roles, False)
    await db.clear_level_up_channel(GUILD_ID, ANNOUNCE_ID)

    # a second server starts from the defaults
    other = await cog.load_settings(4242)
    check("other server channel", other.level_up_channel_id,
          lv.DEFAULT_LEVEL_UP_CHANNEL_ID)
    check("other server ignores", other.ignored_roles,
          set(lv.DEFAULT_IGNORED_ROLE_IDS))

    # what the slash tree looks like
    await bot.add_cog(cog)
    group = bot.tree.get_command("levels")
    names = sorted(command.name for command in group.walk_commands())
    check("slash subcommands", names,
          ["addxp", "ignorechannel", "ignorerole", "levelup", "resetxp",
           "settings", "setxp", "unignorechannel", "unignorerole"])
    levelup = group.get_command("levelup")
    params = {param.name: param for param in levelup.parameters}
    check("levelup params", sorted(params), ["channel", "everywhere"])
    check("levelup channel optional", params["channel"].required, False)
    check("everywhere optional", params["everywhere"].required, False)
    print("levelup channel types:",
          [t.value for t in params["channel"].channel_types])
    ignore = group.get_command("ignorechannel")
    iparam = {param.name: param for param in ignore.parameters}["channel"]
    check("ignorechannel channel required", iparam.required, True)
    print("ignorechannel types:", [t.value for t in iparam.channel_types])
    settings_cmd = group.get_command("settings")
    check("settings takes no argument", list(settings_cmd.parameters), [])

    print("\n" + ("ALL GOOD" if not failures
                  else f"FAILURES ({len(failures)}):\n- "
                       + "\n- ".join(failures)))
    os.remove(DB_PATH)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

