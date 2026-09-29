"""Offline checks for the reaction role cog - no token, no network needed.

Run with:  python check_roles.py   (prints "ALL GOOD" and exits 0 when green)

Covers the pieces that broke in production: reading a message link vs a bare
ID, picking the channel of a bare ID, and granting from a raw reaction event
that only carries a guild id.
"""

import asyncio

import discord

import cogs.roles as roles
from cogs.roles import Roles, parse_message_ref, RoleSetupError

GUILD = 1554266264149696573
CHANNEL = 1554266265328287900
MESSAGE = 1554353425955692595
ROLE = 1554300000000000000
USER = 547790278082428930
BOT = 1554262040413999135

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")
        print(f"FAIL {name}: {got!r} != {want!r}")
    else:
        print(f"ok   {name}")


def raises(name, func, *args):
    try:
        func(*args)
    except RoleSetupError as error:
        print(f"ok   {name} -> {error}")
    except Exception as error:  # noqa: BLE001 - any other error is a failure
        failures.append(f"{name} raised {error!r}")
        print(f"FAIL {name} raised {error!r}")
    else:
        failures.append(f"{name} did not raise")
        print(f"FAIL {name} did not raise")


# --------------------------------------------------------------- message refs
LINK = f"https://discord.com/channels/{GUILD}/{CHANNEL}/{MESSAGE}"
check("bare id", parse_message_ref(str(MESSAGE)), (None, MESSAGE))
check("bare id in junk", parse_message_ref(f" <@{MESSAGE}> "), (None, MESSAGE))
check("link", parse_message_ref(LINK), (CHANNEL, MESSAGE))
check("link @me", parse_message_ref(
    f"https://canary.discord.com/channels/@me/{CHANNEL}/{MESSAGE}"),
    (CHANNEL, MESSAGE))
check("link newest", parse_message_ref(
    f"https://ptb.discord.com/channels/{GUILD}/{CHANNEL}/-/{MESSAGE}"),
    (CHANNEL, MESSAGE))
check("link in markdown", parse_message_ref(f"[msg]({LINK})"), (CHANNEL, MESSAGE))
check("link trailing dot", parse_message_ref(LINK + "."), (CHANNEL, MESSAGE))
raises("guild and channel only", parse_message_ref, f"{GUILD}/{CHANNEL}")
raises("newest message link", parse_message_ref,
       f"https://discord.com/channels/{GUILD}/{CHANNEL}/-")
raises("empty", parse_message_ref, "")
raises("word", parse_message_ref, "the one from yesterday")
raises("channel mention", parse_message_ref, f"<#{CHANNEL}>")
raises("role mention", parse_message_ref, f"<@&{ROLE}>")
check("angle bracketed link", parse_message_ref(f"<{LINK}>"),
      (CHANNEL, MESSAGE))


# ------------------------------------------------------------ resolve_target
class FakeChannel(discord.abc.Messageable):
    def __init__(self, channel_id, kind="channel"):
        self.id = channel_id
        self.kind = kind

    async def _get_channel(self):  # pragma: no cover - unused
        return self

    async def fetch_message(self, message_id):
        return f"{self.kind}:{self.id}:{message_id}"


class ChannelGone(discord.HTTPException):
    def __init__(self):
        self.response = None
        self.status = 404
        self.code = 10003
        self.message = self.text = "Unknown Channel"


class FakeGuild:
    def __init__(self, channels=(), threads=(), gone=()):
        self.id = GUILD
        self.channels = {c.id: c for c in channels}
        self.threads = {t.id: t for t in threads}
        self.gone = set(gone)

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_thread(self, channel_id):
        return self.threads.get(channel_id)

    async def fetch_channel(self, channel_id):
        if channel_id in self.gone:
            raise ChannelGone()
        return self.threads.get(channel_id) or FakeChannel(channel_id,
                                                           "fetched")


async def resolve_cases():
    text = FakeChannel(CHANNEL)
    thread = FakeChannel(CHANNEL + 1, "thread")
    guild = FakeGuild(channels=[text], threads=[thread], gone=[CHANNEL + 7])
    checker = object.__new__(Roles)  # resolve_target needs no bot state

    async def target_of(message, channel=None):
        target, message_id = await checker.resolve_target(guild, message,
                                                         channel)
        return target.id, message_id

    check("link without channel", await target_of(LINK), (CHANNEL, MESSAGE))
    check("link with the same channel", await target_of(LINK, text),
          (CHANNEL, MESSAGE))
    check("thread link", await target_of(
        f"https://discord.com/channels/{GUILD}/{thread.id}/{MESSAGE}"),
        (thread.id, MESSAGE))
    check("uncached channel of a link", await target_of(
        f"https://discord.com/channels/{GUILD}/{CHANNEL + 3}/{MESSAGE}"),
        (CHANNEL + 3, MESSAGE))
    check("bare id with channel", await target_of(str(MESSAGE), text),
          (CHANNEL, MESSAGE))

    for name, message, channel in (
            ("mismatching channel", LINK, FakeChannel(CHANNEL + 9)),
            ("bare id without channel", str(MESSAGE), None),
            ("unreadable link", f"https://discord.com/channels/{GUILD}/"
                               f"{CHANNEL + 7}/{MESSAGE}", None)):
        try:
            await checker.resolve_target(guild, message, channel)
        except RoleSetupError as error:
            print(f"ok   {name} -> {error}")
        else:
            failures.append(f"{name} did not raise")
            print(f"FAIL {name} did not raise")


# ---------------------------------------------------------- command options
async def command_shapes():
    """Loads the cog and dumps the slash command options the tree builds."""
    from discord.ext import commands

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    try:
        await bot.load_extension("cogs.roles")
    except Exception as error:  # noqa: BLE001
        failures.append(f"extension failed to load: {error!r}")
        print(f"FAIL load cogs.roles: {error!r}")
        return
    group = bot.tree.get_command("rolemenu")
    if group is None:
        failures.append("rolemenu missing from the command tree")
        print("FAIL rolemenu missing from the command tree")
    else:
        for sub in sorted(group.commands, key=lambda c: c.name):
            app = getattr(sub, "app_command", sub)  # hybrid commands wrap it
            try:
                # exactly the payload /commands sync sends to Discord
                payload = app.to_dict(bot.tree)
            except Exception as error:  # noqa: BLE001
                failures.append(f"{sub.name}: {error!r}")
                print(f"FAIL /rolemenu {sub.name}: {error!r}")
                continue
            rendered = ", ".join(
                f"{option['name']}:{discord.AppCommandOptionType(option['type']).name}"
                f"{'?' if not option.get('required', False) else ''}"
                for option in payload.get("options", []))
            print(f"ok   /{payload['name']} {sub.name}({rendered})")
    await bot.close()


# ------------------------------------------------------------- granting path
class FakeRole:
    def __init__(self, role_id, name, position):
        self.id, self.name, self.position = role_id, name, position
        self.managed = False

    def is_default(self):
        return False

    def __ge__(self, other):
        return self.position >= other.position

    def __lt__(self, other):
        return self.position < other.position


class FakeMember:
    def __init__(self, member_id, top_role, bot=False):
        self.id, self.top_role, self.roles = member_id, top_role, []
        self.bot = bot
        self.mention = f"<@{member_id}>"
        self.granted = []

    async def add_roles(self, role, *, reason=None):
        self.granted.append((role.id, reason))


class GrantGuild:
    def __init__(self, role, me):
        self.id = GUILD
        self.role = role
        self.me = me
        self.fetched = None

    def get_role(self, role_id):
        return self.role if role_id == self.role.id else None

    async def fetch_member(self, user_id):
        return self.fetched


class GrantBotUser:
    id = BOT


class GrantBot:
    def __init__(self, guild):
        self.guild = guild
        self.user = GrantBotUser()

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD else None

    def get_channel(self, channel_id):
        return None


class Payload:
    def __init__(self, guild_id=0, user_id=USER, member=None):
        self.guild_id = guild_id
        self.channel_id = CHANNEL
        self.message_id = MESSAGE
        self.user_id = user_id
        self.emoji = discord.PartialEmoji.from_str("\u2705")
        self.member = member


async def mapped_row(message_id, key):
    return {"guild_id": GUILD, "channel_id": CHANNEL,
            "message_id": message_id, "emoji": key, "role_id": ROLE}


async def unmapped(message_id, key):
    return None


async def grant_cases():
    """A raw reaction event carries no guild object, the cog must look it up."""
    import logging

    top = FakeRole(ROLE + 1, "bot top", 10)
    role = FakeRole(ROLE, "Member", 5)
    me = FakeMember(BOT, top)
    guild = GrantGuild(role, me)
    cog = Roles.__new__(Roles)
    cog.bot = GrantBot(guild)
    cog.log = logging.getLogger("validate")

    original = roles.get_reaction_role
    roles.get_reaction_role = unmapped
    # a reaction in DMs and an unknown guild must not blow up
    await cog.grant_role(Payload(guild_id=None))
    await cog.grant_role(Payload(guild_id=9999))
    await cog.grant_role(Payload(guild_id=GUILD))
    print("ok   dm / unknown guild / unmapped emoji all survived")

    roles.get_reaction_role = mapped_row
    author = FakeMember(USER, top)
    guild.fetched = author
    await cog.grant_role(Payload(guild_id=GUILD))  # payload.member is None
    check("role granted through fetch_member", author.granted,
          [(ROLE, roles.GRANT_REASON)])

    own = FakeMember(BOT, top)
    guild.fetched = own
    await cog.grant_role(Payload(guild_id=GUILD, user_id=BOT, member=own))
    check("own reaction skipped", own.granted, [])

    guild.fetched = None
    holder = FakeMember(USER, top)
    holder.roles = [role]
    guild.me = holder  # role sits above the bot: mapping kept, nothing granted
    await cog.grant_role(Payload(guild_id=GUILD, member=holder))
    check("already held, nothing changed", holder.granted, [])

    roles.get_reaction_role = original


async def main():
    await resolve_cases()
    await grant_cases()
    await command_shapes()
    print("\n" + ("ALL GOOD" if not failures
                  else "FAILURES:\n- " + "\n- ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

