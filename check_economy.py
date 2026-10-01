"""Offline checks for the economy cog's /upgrade bet option.

Run with:  python check_economy.py   (prints "ALL GOOD" and exits 0 when green)

Covers the bet added to /upgrade: the default (5), the hard cap (100) and that
exactly the amount the caller passed is what reaches the wallet and the answer.
Everything runs against a throwaway database in the temp folder, so the real
bot.db is never touched.
"""

import asyncio
import os
import tempfile

import discord
from discord.ext import commands

import db
import cogs.economy as ec

DB_PATH = os.path.join(tempfile.gettempdir(), "check_economy.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
db.DB_PATH = DB_PATH

GUILD_ID = 1554266264149696573
USER_ID = 777

failures = []


def check(name, got, want):
    if got == want:
        print(f"ok   {name}")
    else:
        failures.append(name)
        print(f"FAIL {name}: {got!r} != {want!r}")


class Rigged:
    """Replaces the SystemRandom draw so a win or a loss is predictable."""

    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


class User:
    def __init__(self):
        self.id = USER_ID
        self.mention = f"<@{USER_ID}>"


class Guild:
    id = GUILD_ID


class Response:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    def is_done(self):
        return True


class Interaction:
    def __init__(self):
        self.guild = Guild()
        self.user = User()
        self.response = Response()


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())


def last(interaction):
    content, _ = interaction.response.sent[-1]
    return content


def win_text(bet, balance):
    return ec.UPGRADE_WIN.format(mention=f"<@{USER_ID}>", bet=bet,
                                 currency=ec.CURRENCY_NAME, balance=balance)


def lose_text(bet, balance):
    return ec.UPGRADE_LOSE.format(mention=f"<@{USER_ID}>", bet=bet,
                                  currency=ec.CURRENCY_NAME, balance=balance)


async def balance():
    return (await db.get_user(GUILD_ID, USER_ID))["balance"]


async def main():
    await db.init_db()
    bot = Bot()
    cog = ec.Economy(bot)
    interaction = Interaction()

    # -------------------------------------------------- the slash option shape
    await bot.add_cog(cog)
    command = bot.tree.get_command("upgrade")
    params = {param.name: param for param in command.parameters}
    check("only bet is asked for", sorted(params), ["bet"])
    bet = params["bet"]
    check("bet optional", bet.required, False)
    check("bet default", bet.default, ec.UPGRADE_DEFAULT_BET)
    check("bet default is 5", ec.UPGRADE_DEFAULT_BET, 5)
    check("bet min", bet.min_value, 1)
    check("bet max", bet.max_value, ec.UPGRADE_MAX_BET)
    check("bet max is 100", ec.UPGRADE_MAX_BET, 100)

    # -------------------------------------------------- the money actually moves
    await db.add_balance(GUILD_ID, USER_ID, 200)
    check("seeded wallet", await balance(), 200)

    ec._rng = Rigged(0.0)  # always win
    # no argument at all -> the default bet is used
    await cog.upgrade.callback(cog, interaction)
    check("default bet win message", last(interaction),
          win_text(ec.UPGRADE_DEFAULT_BET, 205))
    check("default bet doubled 200 -> 205", await balance(), 205)

    # the hard cap the user is allowed to bet
    await cog.upgrade.callback(cog, interaction, ec.UPGRADE_MAX_BET)
    check("max bet win message", last(interaction),
          win_text(ec.UPGRADE_MAX_BET, 305))
    check("max bet doubled 205 -> 305", await balance(), 305)

    # a loss takes the very same amount away
    ec._rng = Rigged(1.0)  # always lose
    await cog.upgrade.callback(cog, interaction, ec.UPGRADE_MAX_BET)
    check("max bet lose message", last(interaction),
          lose_text(ec.UPGRADE_MAX_BET, 205))
    check("max bet lost 305 -> 205", await balance(), 205)

    # the smallest allowed bet
    await db.remove_balance(GUILD_ID, USER_ID, 1000)  # drain to zero
    check("drained wallet", await balance(), 0)
    await db.add_balance(GUILD_ID, USER_ID, 1)
    ec._rng = Rigged(0.0)
    await cog.upgrade.callback(cog, interaction, 1)
    check("min bet win message", last(interaction), win_text(1, 2))
    check("min bet doubled 1 -> 2", await balance(), 2)

    # a bet the member cannot afford is refused and changes nothing
    await db.remove_balance(GUILD_ID, USER_ID, 1000)
    try:
        await cog.upgrade.callback(cog, interaction, ec.UPGRADE_DEFAULT_BET)
        check("poor user refused", "no error", "InsufficientFunds")
    except ec.InsufficientFunds as error:
        check("poor user refused", error.price, ec.UPGRADE_DEFAULT_BET)
    check("nothing taken on a refusal", await balance(), 0)

    print("\n" + ("ALL GOOD" if not failures
                  else f"FAILURES ({len(failures)}):\n- "
                       + "\n- ".join(failures)))
    os.remove(DB_PATH)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
