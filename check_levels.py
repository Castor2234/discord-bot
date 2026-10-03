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
BOOST_ROLE_ID = 2003       # a role that earns 1.25x
BIG_BOOST_ROLE_ID = 2004   # a role that earns 1.5x
HALF_ROLE_ID = 2005        # a role that earns 0.5x
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
                      IGNORED_ROLE_ID: Role(IGNORED_ROLE_ID, "muted", self),
                      BOOST_ROLE_ID: Role(BOOST_ROLE_ID, "boost", self),
                      BIG_BOOST_ROLE_ID: Role(BIG_BOOST_ROLE_ID, "vip", self),
                      HALF_ROLE_ID: Role(HALF_ROLE_ID, "slow", self)}
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


class Response:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, *, embed=None,
                           ephemeral=False, **kwargs):
        self.sent.append((content, embed))

    def is_done(self):
        return True


class Interaction:
    def __init__(self, guild, user):
        self.guild = guild
        self.user = user
        self.response = Response()


class Message:
    def __init__(self, guild, channel, author):
        self.guild = guild
        self.channel = channel
        self.author = author


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())


def last(ctx):
    content, embed = ctx.response.sent[-1]
    return embed if embed is not None else content


async def main():
    await db.init_db()
    bot = Bot()
    cog = lv.Levels(bot)
    guild = Guild()
    author = Member()
    ctx = Interaction(guild, author)

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
    check("settings has no multipliers yet",
          values[lv.SETTINGS_MULTIPLIERS_FIELD], lv.MULTIPLIER_NO_ROLES)
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

    # ------------------------------------------------------- the XP multipliers
    # a server that never set one leaves everybody on 1x
    plain = lv.GuildLevelSettings()
    check("no multipliers by default", plain.role_multipliers, {})
    check("plain member is 1x", plain.multiplier_for(Member()), 1.0)
    check("plain xp untouched", lv.Levels.xp_for(20, Member(), plain), 20)

    boosted = lv.GuildLevelSettings(role_multipliers={
        BOOST_ROLE_ID: 1.25, BIG_BOOST_ROLE_ID: 1.5, HALF_ROLE_ID: 0.5})
    boost = lambda *ids: Member([Role(i, "r") for i in ids])
    check("member with no booster is 1x", boosted.multiplier_for(Member()), 1.0)
    check("1.25x role", boosted.multiplier_for(boost(BOOST_ROLE_ID)), 1.25)
    check("1.5x role", boosted.multiplier_for(boost(BIG_BOOST_ROLE_ID)), 1.5)
    check("0.5x role", boosted.multiplier_for(boost(HALF_ROLE_ID)), 0.5)
    check("only the best role counts",
          boosted.multiplier_for(boost(BOOST_ROLE_ID, BIG_BOOST_ROLE_ID)), 1.5)
    check("an unrelated role changes nothing",
          boosted.multiplier_for(boost(MOD_ROLE_ID)), 1.0)

    # the XP a member is actually paid
    check("1.25x of 20",
          lv.Levels.xp_for(20, boost(BOOST_ROLE_ID), boosted), 25)
    check("1.5x of 20",
          lv.Levels.xp_for(20, boost(BIG_BOOST_ROLE_ID), boosted), 30)
    check("0.5x of 20", lv.Levels.xp_for(20, boost(HALF_ROLE_ID), boosted), 10)
    check("a tiny multiplier still pays one",
          lv.Levels.xp_for(1, boost(HALF_ROLE_ID),
                           lv.GuildLevelSettings(
                               role_multipliers={HALF_ROLE_ID: 0.1})), 1)

    # how a multiplier is shown and snapped
    check("show drops a trailing zero", lv.Levels.show_multiplier(1.50), "1.5")
    check("show keeps two decimals", lv.Levels.show_multiplier(1.25), "1.25")
    check("show a whole number", lv.Levels.show_multiplier(2.0), "2")
    check("normalise snaps up", lv.Levels.normalise_multiplier(1.23), 1.25)
    check("normalise keeps an exact step",
          lv.Levels.normalise_multiplier(1.25), 1.25)

    # the settings field: best multiplier first
    check("multiplier field sorted", cog.render_multipliers(
        boosted.role_multipliers),
        lv.MULTIPLIER_TARGET.format(role_id=BIG_BOOST_ROLE_ID, multiplier="1.5")
        + "\n" + lv.MULTIPLIER_TARGET.format(role_id=BOOST_ROLE_ID,
                                             multiplier="1.25")
        + "\n" + lv.MULTIPLIER_TARGET.format(role_id=HALF_ROLE_ID,
                                             multiplier="0.5"))
    check("multiplier field empty", cog.render_multipliers({}),
          lv.MULTIPLIER_NO_ROLES)

    # /levels multiplier, including the @everyone guard
    try:
        await cog.levels_multiplier.callback(
            cog, ctx, Role(GUILD_ID, "@everyone"), 1.25)
        check("multiplier everyone refused", "no error", "LevelConfigError")
    except lv.LevelConfigError as exc:
        check("multiplier everyone refused", str(exc),
              lv.ROLE_MULTIPLIER_EVERYONE.replace("@everyone", "@\u200beveryone"))
    check("everyone stored nothing",
          await db.list_role_multipliers(GUILD_ID), {})

    await cog.levels_multiplier.callback(cog, ctx, guild.roles[BOOST_ROLE_ID],
                                         1.25)
    check("multiplier answer", last(ctx),
          lv.MULTIPLIER_SET.format(role="boost", multiplier="1.25"))
    check("multiplier stored", (await cog.load_settings(GUILD_ID)
                                ).role_multipliers, {BOOST_ROLE_ID: 1.25})

    # setting the same value again says so instead of claiming a change
    await cog.levels_multiplier.callback(cog, ctx, guild.roles[BOOST_ROLE_ID],
                                         1.25)
    check("multiplier unchanged answer", last(ctx),
          lv.MULTIPLIER_UNCHANGED.format(role="boost", multiplier="1.25"))

    # a value off the step grid is refused instead of quietly rounded
    await cog.levels_multiplier.callback(cog, ctx, guild.roles[MOD_ROLE_ID],
                                         1.23)
    check("off grid refused", last(ctx), lv.MULTIPLIER_OFF_GRID.format(
        value="1.23", step=cog.show_multiplier(lv.MULTIPLIER_STEP),
        nearest="1.25"))
    check("off grid stored nothing", (await cog.load_settings(GUILD_ID)
                                      ).role_multipliers, {BOOST_ROLE_ID: 1.25})
    await cog.levels_multiplier.callback(cog, ctx, guild.roles[MOD_ROLE_ID],
                                         99.0)
    check("out of range refused", last(ctx), lv.MULTIPLIER_OUT_OF_RANGE.format(
        min=lv.MULTIPLIER_MIN, max=lv.MULTIPLIER_MAX, value="99"))

    # a booster that is also on the ignore list says so, it earns nothing
    await cog.levels_multiplier.callback(cog, ctx,
                                         guild.roles[IGNORED_ROLE_ID], 1.5)
    check("ignored booster warns", last(ctx),
          lv.MULTIPLIER_SET.format(role="muted", multiplier="1.5")
          + lv.MULTIPLIER_IS_IGNORED.format(role="muted"))
    await cog.levels_clearmultiplier.callback(cog, ctx,
                                              guild.roles[IGNORED_ROLE_ID])

    # the XP gate honours the multiplier: the paid XP matches the base draw
    await db.set_xp(GUILD_ID, USER_ID, 0, 0)
    async with db._connect() as conn:   # the cooldown would eat the next message
        await conn.execute("UPDATE users SET last_xp=0 WHERE guild_id=?",
                           (GUILD_ID,))
        await conn.commit()
    cog.forget_settings(GUILD_ID)
    await cog.on_message(Message(guild, guild.text,
                                 Member([guild.roles[BOOST_ROLE_ID]])))
    boosted_xp = (await db.get_user(GUILD_ID, USER_ID))["xp"]
    check("boosted message paid above the base range",
          boosted_xp >= round(lv.XP_MIN * 1.25)
          and boosted_xp <= round(lv.XP_MAX * 1.25), True)

    # clearing
    await cog.levels_clearmultiplier.callback(cog, ctx,
                                              guild.roles[BOOST_ROLE_ID])
    check("multiplier cleared answer", last(ctx),
          lv.MULTIPLIER_CLEARED.format(role="boost"))
    check("multiplier cleared",
          (await cog.load_settings(GUILD_ID)).role_multipliers, {})
    await cog.levels_clearmultiplier.callback(cog, ctx,
                                              guild.roles[BOOST_ROLE_ID])
    check("clearing twice says so", last(ctx),
          lv.MULTIPLIER_NOT_CLEARED.format(role="boost"))

    await cog.levels_multiplier.callback(cog, ctx, guild.roles[BOOST_ROLE_ID],
                                         1.25)
    await cog.levels_multiplier.callback(cog, ctx,
                                         guild.roles[BIG_BOOST_ROLE_ID], 1.5)
    await cog.levels_clearmultipliers.callback(cog, ctx)
    check("cleared all answer", last(ctx),
          lv.MULTIPLIERS_ALL_CLEARED.format(count=2))
    check("cleared all",
          (await cog.load_settings(GUILD_ID)).role_multipliers, {})
    await cog.levels_clearmultipliers.callback(cog, ctx)
    check("clearing all twice says so", last(ctx),
          lv.MULTIPLIERS_NOTHING_TO_CLEAR)

    # a deleted role takes its multiplier with it
    await db.set_role_multiplier(GUILD_ID, MOD_ROLE_ID, 1.25)
    await db.set_role_multiplier(GUILD_ID, 9999, 1.5)   # a role that is gone
    cog.forget_settings(GUILD_ID)
    await cog.on_guild_role_delete(guild.roles[MOD_ROLE_ID])
    check("deleted role lost its multiplier",
          (await cog.load_settings(GUILD_ID)).role_multipliers, {9999: 1.5})
    cog.forget_settings(GUILD_ID)
    await cog.prune_missing(guild, await cog.load_settings(GUILD_ID))
    check("dead multiplier pruned",
          (await cog.load_settings(GUILD_ID)).role_multipliers, {})

    # stacking is opt-in, so the default path above never touched it: flip the
    # switch and make sure the bonuses add up (and cannot fall through zero)
    was_stacking = lv.MULTIPLIER_STACKS
    lv.MULTIPLIER_STACKS = True
    try:
        stacked = lv.GuildLevelSettings(role_multipliers={
            BOOST_ROLE_ID: 1.25, BIG_BOOST_ROLE_ID: 1.5, HALF_ROLE_ID: 0.5})
        check("stacked adds the bonuses",
              stacked.multiplier_for(boost(BOOST_ROLE_ID, BIG_BOOST_ROLE_ID)),
              1.75)
        check("stacked two of a kind",
              stacked.multiplier_for(boost(BIG_BOOST_ROLE_ID,
                                           BIG_BOOST_ROLE_ID)), 2.0)
        check("stacked with no role", stacked.multiplier_for(Member()), 1.0)
        # a penalty role drags the other roles down with it
        check("stacked penalty and bonus",
              stacked.multiplier_for(boost(HALF_ROLE_ID, BOOST_ROLE_ID)), 0.75)
        # two penalties would add up past 1x without a floor
        check("stacked penalties stop at the floor",
              stacked.multiplier_for(boost(HALF_ROLE_ID, HALF_ROLE_ID)),
              lv.MULTIPLIER_MIN)
        check("stacked penalties never go negative",
              lv.GuildLevelSettings(
                  role_multipliers={HALF_ROLE_ID: lv.MULTIPLIER_MIN}
              ).multiplier_for(boost(HALF_ROLE_ID, HALF_ROLE_ID)),
              lv.MULTIPLIER_MIN)
    finally:
        lv.MULTIPLIER_STACKS = was_stacking
    check("stacking off again",
          lv.GuildLevelSettings(role_multipliers={HALF_ROLE_ID: 0.5}
                                ).multiplier_for(boost(HALF_ROLE_ID,
                                                       HALF_ROLE_ID)), 0.5)

    # a second server starts from the defaults
    other = await cog.load_settings(4242)
    check("other server channel", other.level_up_channel_id,
          lv.DEFAULT_LEVEL_UP_CHANNEL_ID)
    check("other server ignores", other.ignored_roles,
          set(lv.DEFAULT_IGNORED_ROLE_IDS))
    check("other server multipliers", other.role_multipliers, {})

    # what the slash tree looks like
    await bot.add_cog(cog)
    group = bot.tree.get_command("levels")
    names = sorted(command.name for command in group.walk_commands())
    check("slash subcommands", names,
          ["addxp", "clearmultiplier", "clearmultipliers", "ignorechannel",
           "ignorerole", "levelup", "multiplier", "resetxp", "settings",
           "setxp", "unignorechannel", "unignorerole"])
    mult = group.get_command("multiplier")
    mparams = {param.name: param for param in mult.parameters}
    check("multiplier params", sorted(mparams), ["multiplier", "role"])
    check("multiplier value required", mparams["multiplier"].required, True)
    check("multiplier role required", mparams["role"].required, True)
    clear = group.get_command("clearmultiplier")
    check("clearmultiplier takes a role",
          [p.name for p in clear.parameters], ["role"])
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

