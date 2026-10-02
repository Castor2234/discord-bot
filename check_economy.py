"""Offline checks for the economy cog: the /roll bet and the staff shop tools.

Run with:  python check_economy.py   (prints "ALL GOOD" and exits 0 when green)

Covers the bet added to /roll - the default (5), the hard cap (100) and that
exactly the amount the caller passed is what reaches the wallet and the answer -
plus the staff commands that manage the catalogue: /shop add, setprice, hide and
restore, including the `active` flag that takes an item off the shelf.
The /transfer command is checked too: the 1-100 amount range, the 60 s
cooldown, that both wallets move together and that a refusal (to yourself, to a
bot, with an empty wallet) changes nothing.
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
FRIEND_ID = 888

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
    def __init__(self, user_id=USER_ID, bot=False):
        self.id = user_id
        self.mention = f"<@{user_id}>"
        self.bot = bot


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
    def __init__(self, user_id=USER_ID):
        self.guild = Guild()
        self.user = User(user_id)
        self.response = Response()


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())


def last(interaction):
    content, _ = interaction.response.sent[-1]
    return content


def win_text(bet, balance):
    return ec.ROLL_WIN.format(mention=f"<@{USER_ID}>", bet=bet,
                                 currency=ec.CURRENCY_NAME, balance=balance)


def lose_text(bet, balance):
    return ec.ROLL_LOSE.format(mention=f"<@{USER_ID}>", bet=bet,
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
    command = bot.tree.get_command("roll")
    params = {param.name: param for param in command.parameters}
    check("only bet is asked for", sorted(params), ["bet"])
    bet = params["bet"]
    check("bet optional", bet.required, False)
    check("bet default", bet.default, ec.ROLL_DEFAULT_BET)
    check("bet default is 5", ec.ROLL_DEFAULT_BET, 5)
    check("bet min", bet.min_value, 1)
    check("bet max", bet.max_value, ec.ROLL_MAX_BET)
    check("bet max is 100", ec.ROLL_MAX_BET, 100)

    # -------------------------------------------------- the money actually moves
    await db.add_balance(GUILD_ID, USER_ID, 200)
    check("seeded wallet", await balance(), 200)

    ec._rng = Rigged(0.0)  # always win
    # no argument at all -> the default bet is used
    await cog.roll.callback(cog, interaction)
    check("default bet win message", last(interaction),
          win_text(ec.ROLL_DEFAULT_BET, 205))
    check("default bet doubled 200 -> 205", await balance(), 205)

    # the hard cap the user is allowed to bet
    await cog.roll.callback(cog, interaction, ec.ROLL_MAX_BET)
    check("max bet win message", last(interaction),
          win_text(ec.ROLL_MAX_BET, 305))
    check("max bet doubled 205 -> 305", await balance(), 305)

    # a loss takes the very same amount away
    ec._rng = Rigged(1.0)  # always lose
    await cog.roll.callback(cog, interaction, ec.ROLL_MAX_BET)
    check("max bet lose message", last(interaction),
          lose_text(ec.ROLL_MAX_BET, 205))
    check("max bet lost 305 -> 205", await balance(), 205)

    # the smallest allowed bet
    await db.remove_balance(GUILD_ID, USER_ID, 1000)  # drain to zero
    check("drained wallet", await balance(), 0)
    await db.add_balance(GUILD_ID, USER_ID, 1)
    ec._rng = Rigged(0.0)
    await cog.roll.callback(cog, interaction, 1)
    check("min bet win message", last(interaction), win_text(1, 2))
    check("min bet doubled 1 -> 2", await balance(), 2)

    # a bet the member cannot afford is refused and changes nothing
    await db.remove_balance(GUILD_ID, USER_ID, 1000)
    try:
        await cog.roll.callback(cog, interaction, ec.ROLL_DEFAULT_BET)
        check("poor user refused", "no error", "InsufficientFunds")
    except ec.InsufficientFunds as error:
        check("poor user refused", error.price, ec.ROLL_DEFAULT_BET)
    check("nothing taken on a refusal", await balance(), 0)

    # ------------------------------------------------------ /transfer shape
    command = bot.tree.get_command("transfer")
    params = {param.name: param for param in command.parameters}
    check("transfer asks for member and amount", sorted(params),
          ["amount", "member"])
    check("member is required", params["member"].required, True)
    check("amount is required", params["amount"].required, True)
    check("amount range", (params["amount"].min_value, params["amount"].max_value),
          (1, ec.TRANSFER_MAX_AMOUNT))
    check("amount max is 100", ec.TRANSFER_MAX_AMOUNT, 100)
    check("transfer cooldown is 60 s", ec.TRANSFER_COOLDOWN, 60.0)
    check("transfer is gated by a cooldown", bool(command.checks), True)

    # --------------------------------------------------------- the coins move
    friend = User(FRIEND_ID)
    await db.add_balance(GUILD_ID, USER_ID, 100)

    async def friend_balance():
        return (await db.get_user(GUILD_ID, FRIEND_ID))["balance"]

    await cog.transfer.callback(cog, interaction, friend, ec.TRANSFER_MAX_AMOUNT)
    check("transfer message", last(interaction), ec.TRANSFER_SENT.format(
        sender=f"<@{USER_ID}>", receiver=f"<@{FRIEND_ID}>",
        amount=ec.TRANSFER_MAX_AMOUNT, balance=100,
        currency=ec.CURRENCY_NAME))
    check("sender paid 100", await balance(), 0)
    check("receiver got 100", await friend_balance(), 100)

    # the db records both sides of the move, so the log matches the wallets
    sent = await db.get_transactions(GUILD_ID, USER_ID, 1)
    check("the payer is logged as spent", [row["amount"] for row in sent], [-100])
    check("the payer sees the receiver",
          [row["other_user_id"] for row in sent], [FRIEND_ID])
    received = await db.get_transactions(GUILD_ID, FRIEND_ID, 1)
    check("the receiver is logged as gained",
          [row["amount"] for row in received], [100])
    check("the receiver sees the payer",
          [row["other_user_id"] for row in received], [USER_ID])

    # a second transfer moves the very same amount again
    await db.add_balance(GUILD_ID, USER_ID, 30)
    await cog.transfer.callback(cog, interaction, friend, 7)
    check("small transfer message", last(interaction), ec.TRANSFER_SENT.format(
        sender=f"<@{USER_ID}>", receiver=f"<@{FRIEND_ID}>", amount=7,
        balance=107, currency=ec.CURRENCY_NAME))
    check("sender paid 7 of 30", await balance(), 23)
    check("receiver got 7 more", await friend_balance(), 107)
    check("the second transfer is logged too",
          [row["amount"] for row in await db.get_transactions(GUILD_ID, USER_ID, 1)],
          [-7])

    # -------------------------------------------------------------- refusals
    async def refused(name, member, amount, want):
        try:
            await cog.transfer.callback(cog, interaction, member, amount)
            check(name, "no error", want)
        except ec.TransferError as error:
            check(name, error.text, want)

    # to yourself: nothing moves
    await refused("self transfer refused", interaction.user, 5, ec.TRANSFER_SELF)
    # to a bot: nothing moves
    await refused("bot receiver refused", User(FRIEND_ID, bot=True), 5,
                  ec.TRANSFER_NOT_FOR_BOTS)
    # more than the sender's wallet holds: nothing moves
    await refused("poor sender refused", friend, 1000,
                  ec.TRANSFER_INSUFFICIENT.format(missing=1000 - 23, amount=1000,
                                                  balance=23,
                                                  currency=ec.CURRENCY_NAME))
    check("a refusal takes nothing", (await balance(), await friend_balance()),
          (23, 107))

    # a bad amount is caught by the db even if the range were bypassed
    await refused("zero amount refused", friend, 0,
                  ec.TRANSFER_BAD_AMOUNT.format(max=ec.TRANSFER_MAX_AMOUNT))
    check("a bad amount takes nothing", await friend_balance(), 107)

    # the error handler turns the exception into exactly that text
    check("error_text of a refusal",
          await cog.error_text(ec.TransferError(ec.TRANSFER_SELF)),
          ec.TRANSFER_SELF)
    check("error_text of a cooldown",
          await cog.error_text(
              discord.app_commands.CommandOnCooldown(None, 12.4)),
          ec.COOLDOWN_MESSAGE.format(seconds=12))

    # the feature flag closes the command without touching the wallets
    ec.TRANSFER_ENABLED = False
    await cog.transfer.callback(cog, interaction, friend, 1)
    check("disabled transfer answer", last(interaction), ec.TRANSFER_DISABLED)
    check("a disabled transfer takes nothing", await friend_balance(), 107)
    ec.TRANSFER_ENABLED = True

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
