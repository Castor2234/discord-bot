"""Offline checks for the economy cog: the /upgrade bet and the staff shop tools.

Run with:  python check_economy.py   (prints "ALL GOOD" and exits 0 when green)

Covers the bet added to /upgrade - the default (5), the hard cap (100) and that
exactly the amount the caller passed is what reaches the wallet and the answer -
plus the staff commands that manage the catalogue: /shop add, setprice, hide and
restore, including the `active` flag that takes an item off the shelf.
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

    # ------------------------------------------------ the staff shop commands
    group = bot.tree.get_command("shop")
    check("shop is a group", isinstance(group, discord.app_commands.Group),
          True)
    check("shop subcommands", sorted(c.name for c in group.walk_commands()),
          ["add", "buy", "fulfil", "hide", "list", "orders", "restore",
           "setprice"])
    check("shop staff gating", {c.name: bool(c.checks) for c in group.commands},
          {"list": False, "buy": False, "add": True, "setprice": True,
           "hide": True, "restore": True, "orders": True, "fulfil": True})
    add = group.get_command("add")
    add_params = {p.name: p for p in add.parameters}
    check("add params", sorted(add_params),
          ["code", "description", "name", "price"])
    check("add code length",
          (add_params["code"].min_value, add_params["code"].max_value),
          (1, ec.SHOP_CODE_MAX))
    check("add name length",
          (add_params["name"].min_value, add_params["name"].max_value),
          (1, ec.SHOP_NAME_MAX))
    check("add price range",
          (add_params["price"].min_value, add_params["price"].max_value),
          (1, ec.SHOP_MAX_PRICE))
    check("add description optional", add_params["description"].required, False)
    check("setprice params",
          sorted(p.name for p in group.get_command("setprice").parameters),
          ["code", "price"])
    check("hide params",
          sorted(p.name for p in group.get_command("hide").parameters),
          ["code"])
    check("restore params",
          sorted(p.name for p in group.get_command("restore").parameters),
          ["code"])

    # ------------------------------------------------------- the catalogue
    row = await db.add_shop_item("2x4", "Double", "a test item", 30)
    check("item added", (row["code"], row["name"], row["price"], row["active"]),
          ("2x4", "Double", 30, 1))
    # cheapest first: 2x4 costs 30, the seeded 1x6 costs 40
    check("item shows up in the catalogue",
          [r["code"] for r in await db.get_shop_items()], ["2x4", "1x6"])
    check("a taken code is refused", await db.add_shop_item("2x4", "Again"), None)

    before = await db.set_shop_item_price("2x4", 45)
    check("price change returns the old row", before["price"], 30)
    check("new price stored", (await db.get_shop_item("2x4"))["price"], 45)
    check("price of an unknown item",
          await db.set_shop_item_price("nope", 1), None)

    before = await db.hide_shop_item("2x4")
    check("hide returns the row as it was", before["active"], 1)
    check("hidden item leaves the catalogue",
          [r["code"] for r in await db.get_shop_items()], ["1x6"])
    check("hidden item is not found by buyers",
          await db.get_shop_item("2x4"), None)
    check("hidden row is still there",
          (await db.get_shop_item("2x4", active_only=False))["price"], 45)
    check("hidden item cannot be bought",
          await db.purchase_item(GUILD_ID, USER_ID, before["item_id"]),
          ("not_found", None, None))
    check("hiding twice reports it was already hidden",
          (await db.hide_shop_item("2x4"))["active"], 0)
    check("hide of an unknown item", await db.hide_shop_item("nope"), None)

    before = await db.restore_shop_item("2x4")
    check("restore returns the row as it was", before["active"], 0)
    check("restored item is back in the catalogue",
          [r["code"] for r in await db.get_shop_items()], ["1x6", "2x4"])
    check("restoring twice reports it was already on sale",
          (await db.restore_shop_item("2x4"))["active"], 1)
    check("restore of an unknown item", await db.restore_shop_item("nope"), None)

    # -------------------------------------------------- the commands themselves
    await cog.shop_add.callback(cog, interaction, " 3X3 ", "Tic", 50)
    check("add lower cases the code", last(interaction),
          ec.SHOP_ITEM_ADDED.format(name="Tic", code="3x3", price=50))
    await cog.shop_add.callback(cog, interaction, "3x3", "Tic again", 1)
    check("add refuses a taken code", last(interaction),
          ec.SHOP_ITEM_EXISTS.format(code="3x3"))
    await cog.shop_add.callback(cog, interaction, "4 4", "Spaces", 1)
    check("add refuses a code with a space", last(interaction),
          ec.SHOP_BAD_CODE.format(max=ec.SHOP_CODE_MAX))
    await cog.shop_add.callback(cog, interaction, "123", "Digits", 1)
    check("add refuses a numeric code", last(interaction),
          ec.SHOP_BAD_CODE.format(max=ec.SHOP_CODE_MAX))

    await cog.shop_setprice.callback(cog, interaction, "3x3", 60)
    check("setprice answer", last(interaction),
          ec.SHOP_ITEM_PRICED.format(name="Tic", code="3x3", old=50, new=60))
    await cog.shop_setprice.callback(cog, interaction, "zzz", 10)
    check("setprice of an unknown item", last(interaction),
          ec.SHOP_ITEM_NOT_FOUND.format(query="zzz"))

    await cog.shop_hide.callback(cog, interaction, "3x3")
    check("hide answer", last(interaction),
          ec.SHOP_ITEM_HIDDEN.format(name="Tic", code="3x3"))
    await cog.shop_hide.callback(cog, interaction, "3x3")
    check("hide twice", last(interaction),
          ec.SHOP_ITEM_ALREADY_HIDDEN.format(name="Tic", code="3x3"))
    await cog.shop_hide.callback(cog, interaction, "zzz")
    check("hide of an unknown item", last(interaction),
          ec.SHOP_ITEM_NOT_FOUND.format(query="zzz"))

    await cog.shop_restore.callback(cog, interaction, "3x3")
    check("restore answer", last(interaction),
          ec.SHOP_ITEM_RESTORED.format(name="Tic", code="3x3", price=60))
    await cog.shop_restore.callback(cog, interaction, "3x3")
    check("restore twice", last(interaction),
          ec.SHOP_ITEM_ALREADY_ON_SALE.format(name="Tic", code="3x3"))

    print("\n" + ("ALL GOOD" if not failures
                  else f"FAILURES ({len(failures)}):\n- "
                       + "\n- ".join(failures)))
    os.remove(DB_PATH)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
