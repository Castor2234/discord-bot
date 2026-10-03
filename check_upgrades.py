"""Offline checks for the upgrades cog and the tiers it feeds.

Run with:  python check_upgrades.py   (prints "ALL GOOD" and exits 0 when green)

Covers the catalogue itself (every tier gives what the plan says and costs what
the plan says), the atomic purchase in db.buy_upgrade, and - the part that is
easy to get wrong - that a bought tier really raises the limit of /daily,
/roll and /transfer, that the tiers are sequential, and that a refused click
takes neither mango nor the tier.
Everything runs against a throwaway database in the temp folder, so the real
bot.db is never touched.
"""

import asyncio
import os
import sys
import tempfile

# the texts carry emoji, which the default Windows console cannot print
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import discord
from discord.ext import commands

import db
import upgrade_levels as lv
import cogs.economy as ec
import cogs.upgrades as uc

DB_PATH = os.path.join(tempfile.gettempdir(), "check_upgrades.db")
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


class Avatar:
    url = "https://example.invalid/avatar.png"


class User:
    def __init__(self, user_id=USER_ID, bot=False):
        self.id = user_id
        self.mention = f"<@{user_id}>"
        self.display_name = f"user{user_id}"
        self.display_avatar = Avatar()
        self.bot = bot


class Guild:
    id = GUILD_ID


class Response:
    def __init__(self):
        self.sent = []
        self.ephemeral = []

    async def send_message(self, content=None, embed=None, view=None,
                           ephemeral=False, **kwargs):
        self.sent.append({"content": content, "embed": embed, "view": view})
        self.ephemeral.append(ephemeral)

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
    return interaction.response.sent[-1]["content"]


def footer(interaction):
    return interaction.response.sent[-1]["embed"].footer.text


async def balance(user_id=USER_ID):
    return (await db.get_user(GUILD_ID, user_id))["balance"]


async def give(amount, user_id=USER_ID):
    await db.add_balance(GUILD_ID, user_id, amount)


async def tiers():
    return await db.get_user_upgrades(GUILD_ID, USER_ID)


async def drain(user_id=USER_ID):
    await db.remove_balance(GUILD_ID, user_id, 1_000_000)


async def click(view, category, tier, user_id=USER_ID):
    """Press one button of a rendered catalogue, the way a member would.

    Returns the interaction the button answered, since that is where the reply
    lands - not the one that rendered the catalogue.
    """
    for button in view.children:
        if button.custom_id == f"upgrade:{category}:{tier}":
            press = Interaction(user_id)
            await button.callback(press)
            return press
    raise AssertionError(f"no button {category}:{tier}")


async def reset_tiers(user_id=USER_ID):
    """Put a member back on the free tier of every ladder."""
    async with db._connect() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO user_upgrades (guild_id, user_id) VALUES (?, ?)",
            (GUILD_ID, user_id))
        await conn.execute(
            "UPDATE user_upgrades SET daily_tier=0, roll_tier=0, "
            "transfer_tier=0 WHERE guild_id=? AND user_id=?", (GUILD_ID, user_id))
        await conn.commit()


async def main():
    await db.init_db()
    bot = Bot()
    cog = uc.Upgrades(bot)
    eco = ec.Economy(bot)
    await bot.add_cog(cog)
    await bot.add_cog(eco)
    interaction = Interaction()

    # ------------------------------------------------- the catalogue itself
    check("daily ladder", lv.DAILY_TIERS, (5, 10, 25, 50))
    check("roll ladder", lv.ROLL_TIERS, (100, 200, 500, 1000))
    check("transfer ladder", lv.TRANSFER_TIERS,
          ((100, 60.0), (200, 30.0), (500, 20.0), (1000, 10.0)))
    check("three tiers to buy", lv.max_tier("daily"), 3)
    check("daily prices", lv.PRICES["daily"], (100, 300, 900))
    check("roll prices", lv.PRICES["roll"], (450, 1000, 1500))
    check("transfer prices", lv.PRICES["transfer"], (500, 1000, 2000))
    check("tier 0 is not sellable", lv.price_for("daily", 0), None)
    check("tier 4 does not exist", lv.price_for("daily", 4), None)
    check("tier 1 prices", lv.price_for("daily", 1), 100)
    check("tier 3 prices", lv.price_for("transfer", 3), 2000)

    # what each tier hands out
    check("free daily", lv.daily_amount(0), 5)
    check("best daily", lv.daily_amount(3), 50)
    check("free roll bet", lv.roll_max_bet(0), 100)
    check("best roll bet", lv.roll_max_bet(3), 1000)
    check("free transfer amount", lv.transfer_max(0), 100)
    check("best transfer amount", lv.transfer_max(3), 1000)
    check("free transfer cooldown", lv.transfer_cooldown(0), 60.0)
    check("best transfer cooldown", lv.transfer_cooldown(3), 10.0)
    check("an out-of-range tier is clamped, not crashed",
          lv.roll_max_bet(99), 1000)
    check("a negative tier is clamped too", lv.daily_amount(-5), 5)
    check("an unknown category is refused loudly",
          _raises(lambda: lv.price_for("nope", 1)), True)

    # ------------------------------------------------------ the /upgrades command
    command = bot.tree.get_command("upgrades")
    check("/upgrades asks for nothing", sorted(p.name for p in command.parameters),
          [])
    check("/upgrades is guild only", command.guild_only, True)

    # ------------------------------------------------- a fresh member sees it
    await cog.upgrades.callback(cog, interaction)
    embed = interaction.response.sent[-1]["embed"]
    check("catalogue title", embed.title, uc.UPGRADES_TITLE)
    for name in lv.CATEGORIES:
        check(f"{name} block is listed", name in str(embed.description) or
              uc.CATEGORY_LABELS[name] in embed.description, True)
    check("every ladder line is drawn",
          all(lv.effect(c, t) in embed.description
              for c in lv.CATEGORIES
              for t in range(lv.max_tier(c) + 1)), True)
    check("an empty wallet shows 0", ec.coins(0) in footer(interaction), True)
    check("the catalogue is ephemeral", interaction.response.ephemeral[-1], True)

    # only the very next step of each ladder is clickable
    view = interaction.response.sent[-1]["view"]
    check("a button per sellable tier", len(view.children),
          sum(lv.max_tier(c) for c in lv.CATEGORIES))
    check("tier 1 is the only buyable one",
          [b.disabled for b in view.children if b.custom_id == "upgrade:daily:1"],
          [False])
    check("tier 2 is locked",
          [b.disabled for b in view.children if b.custom_id == "upgrade:daily:2"],
          [True])
    check("the buttons answer only their owner",
          await _is_owner_only(view), True)


    # ------------------------------------------------------- buying a tier
    # not enough mango: nothing is taken and no tier is granted
    status, bal, got = await db.buy_upgrade(GUILD_ID, USER_ID, "daily", 1)
    check("a poor member cannot buy", status, "insufficient")
    check("nothing was taken", await balance(), 0)
    check("no tier was granted", got["daily"], 0)

    # the tiers are sequential: tier 2 is refused before tier 1 is bought
    await give(10_000)
    status, bal, _ = await db.buy_upgrade(GUILD_ID, USER_ID, "daily", 2)
    check("tier 2 waits for tier 1", status, "sequential")
    check("still nothing bought", (await tiers())["daily"], 0)

    # buying tier 1 takes exactly its price and grants exactly its tier
    status, bal, got = await db.buy_upgrade(GUILD_ID, USER_ID, "daily", 1)
    check("tier 1 is bought", status, "ok")
    check("tier 1 price taken", await balance(), 10_000 - 100)
    check("tier 1 stored", got["daily"], 1)
    check("the other ladders are untouched", (got["roll"], got["transfer"]),
          (0, 0))
    check("the coin log names the category",
          [r["reason"] for r in await db.get_transactions(GUILD_ID, USER_ID, 1)],
          ["upgrade:daily"])
    check("the log shows the price",
          [r["amount"] for r in await db.get_transactions(GUILD_ID, USER_ID, 1)],
          [-100])

    # buying it twice changes nothing the second time
    status, bal, _ = await db.buy_upgrade(GUILD_ID, USER_ID, "daily", 1)
    check("an owned tier is not sold twice", status, "owned")
    check("and is not charged twice", await balance(), 10_000 - 100)

    # an unknown category can never reach the SQL
    check("an unknown category is refused",
          (await db.buy_upgrade(GUILD_ID, USER_ID, "daily_tier; DROP", 1))[0],
          "unknown")

    # buying to the very top, then one step too far
    for tier in (2, 3):
        check(f"daily tier {tier} bought",
              (await db.buy_upgrade(GUILD_ID, USER_ID, "daily", tier))[0], "ok")
    check("daily is maxed", (await tiers())["daily"], 3)
    check("a tier past the top is refused",
          (await db.buy_upgrade(GUILD_ID, USER_ID, "daily", 4))[0], "maxed")

    # ------------------------------------------------- the tier really counts
    # /roll accepts the bought ceiling and refuses one coin more
    await drain()
    await give(10_000)
    ec._rng = _AlwaysWin()
    await eco.roll.callback(eco, interaction, lv.roll_max_bet(0))
    check("free tier plays the free bet", await balance(), 10_000 + 100)
    try:
        await eco.roll.callback(eco, interaction, lv.roll_max_bet(0) + 1)
        check("a bet over the free tier is refused", "no error", "BetTooHigh")
    except ec.BetTooHigh as error:
        check("a bet over the free tier is refused", error.limit, 100)

    # buy the top roll tier and the very same bet now goes through
    check("roll tier 3 bought",
          (await db.buy_upgrade(GUILD_ID, USER_ID, "roll", 1))[0], "ok")
    check("roll tier 2 bought",
          (await db.buy_upgrade(GUILD_ID, USER_ID, "roll", 2))[0], "ok")
    check("roll tier 3 bought",
          (await db.buy_upgrade(GUILD_ID, USER_ID, "roll", 3))[0], "ok")
    balance_before = await balance()
    await eco.roll.callback(eco, interaction, lv.roll_max_bet(3))
    check("the bought ceiling is playable",
          await balance(), balance_before + lv.roll_max_bet(3))
    try:
        await eco.roll.callback(eco, interaction, lv.roll_max_bet(3) + 1)
        check("over the bought ceiling is refused", "no error", "BetTooHigh")
    except ec.BetTooHigh as error:
        check("over the bought ceiling is refused", error.limit, 1000)

    # /transfer obeys its own tier
    friend = User(888)
    for tier in (1, 2, 3):
        check(f"transfer tier {tier} bought",
              (await db.buy_upgrade(GUILD_ID, USER_ID, "transfer", tier))[0], "ok")
    await drain()
    await give(10_000)
    await db.refund_transfer_cooldown(GUILD_ID, USER_ID)
    await eco.transfer.callback(eco, interaction, friend, lv.transfer_max(3))
    check("the bought transfer limit is usable", await balance(888), 1000)
    try:
        await eco.transfer.callback(eco, interaction, friend,
                                    lv.transfer_max(3) + 1)
        check("over the bought transfer limit is refused", "no error",
              "TransferTooHigh")
    except ec.TransferTooHigh as error:
        check("over the bought transfer limit is refused", error.limit, 1000)

    # the bought cooldown really is the shorter one
    check("the bought cooldown is stored in the ladder",
          lv.transfer_cooldown((await tiers())["transfer"]), 10.0)


    # ------------------------------------------------- the buttons buy for real
    await reset_tiers()
    await drain()
    await give(2_000)
    await cog.upgrades.callback(cog, interaction)
    view = interaction.response.sent[-1]["view"]

    press = await click(view, "roll", 1)
    check("a click answers with the bought text", last(press),
          uc.UPGRADE_BOUGHT.format(
              tier=1, effect=lv.effect("roll", 1), price=ec.coins(450),
              balance=ec.coins(1_550)))
    check("the click bought the tier", (await tiers())["roll"], 1)
    check("the click charged the price", await balance(), 1_550)
    check("the click is ephemeral", press.response.ephemeral[-1], True)
    check("the click re-renders with the new balance",
          ec.coins(1_550) in footer(press), True)
    check("the bought tier shows as owned in the new embed",
          "✅" in str(press.response.sent[-1]["embed"].description), True)
    check("the next step of that ladder is now the open one",
          [b.disabled for b in press.response.sent[-1]["view"].children
           if b.custom_id == "upgrade:roll:2"], [False])
    check("and tier 3 is locked again",
          [b.disabled for b in press.response.sent[-1]["view"].children
           if b.custom_id == "upgrade:roll:3"], [True])

    # a member the catalogue was not rendered for gets nothing, even though the
    # 1550 coins on that wallet could well afford the next tier
    stranger = await click(view, "roll", 2, user_id=999)
    check("another member cannot buy from it", last(stranger),
          uc.UPGRADE_NOT_YOURS)
    check("and is not charged", await balance(), 1_550)
    check("and gets no tier", (await tiers())["roll"], 1)

    # clicking a tier too far ahead is refused by the db
    locked = await click(view, "roll", 3)
    check("a locked tier is refused", last(locked), uc.UPGRADE_SEQUENTIAL)
    check("a locked click takes nothing", await balance(), 1_550)

    # and a poor member is told exactly how short they are
    await drain()
    await give(10)
    await cog.upgrades.callback(cog, interaction)
    view = interaction.response.sent[-1]["view"]
    poor = await click(view, "daily", 1)
    check("a poor click is refused", last(poor),
          uc.UPGRADE_INSUFFICIENT.format(missing=ec.coins(90),
                                         price=ec.coins(100),
                                         balance=ec.coins(10)))
    check("a poor click takes nothing", await balance(), 10)
    check("a poor click grants nothing", (await tiers())["daily"], 0)

    # clicking a tier already owned is answered, not charged: 1010 - 100 = 910
    await give(1_000)
    await click(view, "daily", 1)
    await cog.upgrades.callback(cog, interaction)
    again = await click(interaction.response.sent[-1]["view"], "daily", 1)
    check("an owned tier is not sold twice", last(again), uc.UPGRADE_OWNED)
    check("and is not charged twice", await balance(), 910)

    # the feature flag closes the catalogue without touching anything
    uc.UPGRADES_ENABLED = False
    await cog.upgrades.callback(cog, interaction)
    check("a disabled catalogue says so", last(interaction), uc.UPGRADES_DISABLED)
    check("and is still ephemeral", interaction.response.ephemeral[-1], True)
    check("and takes no tier", (await tiers())["daily"], 1)
    uc.UPGRADES_ENABLED = True

    # ------------------------------------------ /userupgrades, another member
    OTHER_ID = 4321
    await reset_tiers(OTHER_ID)
    # 100 + 450 + 1000 buys the three steps below, which leaves 250 in the wallet
    await db.add_balance(GUILD_ID, OTHER_ID, 1_800)
    for category, tier in (("daily", 1), ("roll", 1), ("roll", 2)):
        check(f"the other member buys {category} tier {tier}",
              (await db.buy_upgrade(GUILD_ID, OTHER_ID, category, tier))[0], "ok")
    check("and pays the catalogue price for them", await balance(OTHER_ID), 250)

    command = bot.tree.get_command("userupgrades")
    check("/userupgrades asks only for a member",
          sorted(p.name for p in command.parameters), ["member"])
    check("/userupgrades is guild only", command.guild_only, True)

    other = Interaction(OTHER_ID)
    await cog.userupgrades.callback(cog, interaction, other.user)
    shown = interaction.response.sent[-1]["embed"]
    check("the title names the member", shown.title,
          uc.USER_UPGRADES_TITLE.format(member=other.user.display_name))
    check("their avatar is the thumbnail", shown.thumbnail.url, Avatar.url)
    check("the footer is their balance", shown.footer.text,
          uc.USER_UPGRADES_FOOTER.format(balance=ec.coins(250)))
    current = [line for line in shown.description.splitlines()
               if "*(сейчас)*" in line]
    check("every ladder marks its current tier", len(current), len(lv.CATEGORIES))
    check("their roll tier 2 is the current one",
          any("Ур. 2" in line and lv.effect("roll", 2) in line
              for line in current), True)
    check("their daily tier 1 is the current one",
          any("Ур. 1" in line and lv.effect("daily", 1) in line
              for line in current), True)
    check("their transfer ladder is still free",
          any(lv.effect("transfer", 0) in line for line in current), True)
    check("my own maxed roll tier is not shown as theirs",
          any(lv.effect("roll", 3) in line for line in current), False)
    check("looking at somebody is not ephemeral",
          interaction.response.ephemeral[-1], False)
    check("and hands out no buttons to spend with",
          interaction.response.sent[-1]["view"], None)
    check("and changes nothing of theirs",
          await db.get_user_upgrades(GUILD_ID, OTHER_ID),
          {"daily": 1, "roll": 2, "transfer": 0})

    # a member who never bought anything still gets an answer: all free tiers
    await cog.userupgrades.callback(cog, interaction, User(5555))
    fresh = interaction.response.sent[-1]["embed"]
    check("a member with no row shows the free tiers",
          all(lv.effect(c, 0) in fresh.description for c in lv.CATEGORIES), True)

    # naming nobody shows your own ladder
    await cog.userupgrades.callback(cog, interaction)
    mine = interaction.response.sent[-1]["embed"]
    check("no member means you", mine.title,
          uc.USER_UPGRADES_TITLE.format(member=interaction.user.display_name))
    check("your own balance is shown", mine.footer.text,
          uc.USER_UPGRADES_FOOTER.format(balance=ec.coins(await balance())))

    # and the feature flag closes the read-only view as well
    uc.UPGRADES_ENABLED = False
    await cog.userupgrades.callback(cog, interaction, other.user)
    check("a disabled ladder says so", last(interaction), uc.UPGRADES_DISABLED)
    uc.UPGRADES_ENABLED = True

    print("\n" + ("ALL GOOD" if not failures
                  else f"FAILURES ({len(failures)}):\n- "
                       + "\n- ".join(failures)))
    os.remove(DB_PATH)
    return 1 if failures else 0


def _raises(call):
    """True when `call` refuses instead of returning."""
    try:
        call()
    except ValueError:
        return True
    return False


async def _is_owner_only(view):
    """True when a button refuses a member it was not rendered for."""
    press = Interaction(999)
    try:
        await view.children[0].callback(press)
    except Exception:
        return False
    return last(press) == uc.UPGRADE_NOT_YOURS


class _AlwaysWin:
    """A rigged draw so /roll doubles every time."""

    def random(self):
        return 0.0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
