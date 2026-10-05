"""Offline checks for the /chaosroll command and the ladder it is sold on.

Run with:  python check_chaosroll.py   (prints "ALL GOOD" and exits 0 when green)

Covers the catalogue itself (prices 500/1500/5000, bets 100/200/300,
cooldowns 180/120/60 s, the chance tables and that the rest of the probability
is a lost bet), the locked tier 0, the buying through the /upgrades buttons,
and - the part that is easy to get wrong - that the drawn multiplier is settled
exactly once (0x loses, 1x is a net zero, Nx pays the net), that the per-tier
cooldown is spent, refused and handed back on a refusal, and that a bet over
the bought ceiling never reaches the wallet.
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

DB_PATH = os.path.join(tempfile.gettempdir(), "check_chaosroll.db")
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
    """Replaces the SystemRandom draw so one multiplier is predictable."""

    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


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


def upgrade_id(category, tier):
    return f"upgrade:{category}:{tier}"


async def balance(user_id=USER_ID):
    return (await db.get_user(GUILD_ID, user_id))["balance"]


async def give(amount, user_id=USER_ID):
    await db.add_balance(GUILD_ID, user_id, amount)


async def drain(user_id=USER_ID):
    await db.remove_balance(GUILD_ID, user_id, 1_000_000)


async def tiers(user_id=USER_ID):
    return await db.get_user_upgrades(GUILD_ID, user_id)


async def click(view, category, tier, user_id=USER_ID):
    """Press one button of a rendered catalogue, the way a member would."""
    for button in view.children:
        if button.custom_id == upgrade_id(category, tier):
            press = Interaction(user_id)
            await button.callback(press)
            return press
    raise AssertionError(f"no button {upgrade_id(category, tier)}")


def lose_text(bet, balance):
    return ec.ROLL_LOSE.format(mention=f"<@{USER_ID}>", bet=ec.coins(bet),
                               lost=f"-{ec.coins(bet)}",
                               balance=ec.coins(balance))


def return_text(bet, balance):
    return ec.CHAOS_RETURN.format(mention=f"<@{USER_ID}>", bet=ec.coins(bet),
                                  balance=ec.coins(balance))


def win_text(bet, multiplier, balance):
    return ec.CHAOS_WIN.format(mention=f"<@{USER_ID}>", bet=ec.coins(bet),
                               multiplier=multiplier,
                               won=f"+{ec.coins(bet * (multiplier - 1))}",
                               balance=ec.coins(balance))


async def main():
    await db.init_db()
    bot = Bot()
    eco = ec.Economy(bot)
    ucog = uc.Upgrades(bot)
    await bot.add_cog(eco)
    await bot.add_cog(ucog)
    interaction = Interaction()

    # ------------------------------------------------- the catalogue itself
    check("chaos prices", lv.PRICES["chaos"], (500, 1500, 5000))
    check("chaos has three tiers to buy", lv.max_tier("chaos"), 3)
    check("chaos is one of the categories", "chaos" in lv.CATEGORIES, True)
    check("chaos column", lv.COLUMNS["chaos"], "chaos_tier")
    check("locked bet is 0", [lv.chaos_max_bet(t) for t in range(4)],
          [0, 100, 200, 300])
    check("chaos cooldowns", [lv.chaos_cooldown(t) for t in range(4)],
          [180.0, 180.0, 120.0, 60.0])
    check("tier 1 prices", lv.price_for("chaos", 1), 500)
    check("tier 3 prices", lv.price_for("chaos", 3), 5000)
    check("tier 0 is not sellable", lv.price_for("chaos", 0), None)

    # what each tier pays: the table holds the listed chances, whatever it
    # does not spend is a lost bet (0x)
    check("tier 1 chances", lv.CHAOS_CHANCES[1],
          {1: 0.05, 2: 0.25, 3: 0.03, 4: 0.02, 5: 0.01})
    check("tier 2 chances", lv.CHAOS_CHANCES[2],
          {1: 0.08, 2: 0.25, 3: 0.04, 4: 0.03, 5: 0.02})
    check("tier 3 chances", lv.CHAOS_CHANCES[3],
          {1: 0.10, 2: 0.25, 3: 0.05, 4: 0.03, 5: 0.02, 10: 0.001})
    check("tier 1 spends 36%", round(sum(lv.CHAOS_CHANCES[1].values()), 10),
          0.36)
    check("tier 2 spends 42%", round(sum(lv.CHAOS_CHANCES[2].values()), 10),
          0.42)
    check("tier 3 spends 45.1%", round(sum(lv.CHAOS_CHANCES[3].values()), 10),
          0.451)
    check("no table spends more than it has",
          all(sum(c.values()) <= 1 for c in lv.CHAOS_CHANCES), True)
    check("the locked tier has no chances", lv.chaos_chances(0), {})

    # the draw: every roll inside a band pays that multiplier, the rest loses
    check("0x is what the leftovers pay", lv.chaos_outcome(1, 0.5), 0)
    check("a high roll still loses", lv.chaos_outcome(1, 0.99), 0)
    check("tier 1 1x", lv.chaos_outcome(1, 0.02), 1)
    check("tier 1 2x", lv.chaos_outcome(1, 0.15), 2)
    check("tier 1 3x", lv.chaos_outcome(1, 0.31), 3)
    check("tier 1 4x", lv.chaos_outcome(1, 0.34), 4)
    check("tier 1 5x", lv.chaos_outcome(1, 0.355), 5)
    check("tier 2 1x", lv.chaos_outcome(2, 0.04), 1)
    check("tier 2 2x", lv.chaos_outcome(2, 0.20), 2)
    check("tier 3 5x", lv.chaos_outcome(3, 0.44), 5)
    check("tier 3 10x", lv.chaos_outcome(3, 0.4505), 10)
    check("tier 3 loses past its table", lv.chaos_outcome(3, 0.5), 0)
    check("the locked tier can never win", lv.chaos_outcome(0, 0.0), 0)
    check("an out-of-range tier is clamped, not crashed",
          lv.chaos_outcome(99, 0.02), 1)

    # what the catalogue embed shows for each tier
    check("locked effect", lv.effect("chaos", 0), "ставка недоступна")
    check("tier 1 effect", lv.effect("chaos", 1),
          "ставка до 100 манго, перезарядка 180 с")
    check("tier 3 effect", lv.effect("chaos", 3),
          "ставка до 300 манго, перезарядка 60 с")

    # -------------------------------------------------- the slash option shape
    command = bot.tree.get_command("chaosroll")
    check("the command exists", command is not None, True)
    check("it is guild only", command.guild_only, True)
    check("its description fits Discord", len(command.description) <= 100,
          True)
    params = {param.name: param for param in command.parameters}
    check("only bet is asked for", sorted(params), ["bet"])
    check("bet optional", params["bet"].required, False)
    check("bet default", params["bet"].default, ec.CHAOS_DEFAULT_BET)
    check("bet default is 5", ec.CHAOS_DEFAULT_BET, 5)
    check("bet range", (params["bet"].min_value, params["bet"].max_value),
          (1, 300))
    check("the Range advertises the top of the ladder",
          ec.CHAOS_MAX_BET_LIMIT, 300)
    check("the command is gated by a cooldown", bool(command.checks), True)
    check("the fastest cooldown is the top tier's",
          ec.CHAOS_COOLDOWN_FASTEST, 60.0)

    # ------------------------------------------------------- locked at tier 0
    check("a fresh member starts locked", (await tiers())["chaos"], 0)
    try:
        await eco.chaosroll.callback(eco, interaction, ec.CHAOS_DEFAULT_BET)
        check("tier 0 is refused", "no error", "ChaosLocked")
    except ec.ChaosLocked:
        check("tier 0 is refused", True, True)
    check("and the refusal is in the member's words",
          await eco.error_text(ec.ChaosLocked()), ec.CHAOS_LOCKED)

    # ---------------------------------------------- buying it through buttons
    await give(10_000)
    await ucog.upgrades.callback(ucog, interaction)
    embed = interaction.response.sent[-1]["embed"]
    view = interaction.response.sent[-1]["view"]
    check("the catalogue lists the chaos block",
          uc.CATEGORY_LABELS["chaos"] in embed.description, True)
    check("every chaos tier line is drawn",
          all(lv.effect("chaos", t) in embed.description for t in range(4)),
          True)
    check("a button per sellable tier of every ladder", len(view.children),
          sum(lv.max_tier(c) for c in lv.CATEGORIES))
    check("chaos tier 1 is buyable",
          [b.disabled for b in view.children
           if b.custom_id == upgrade_id("chaos", 1)], [False])
    check("chaos tier 2 is locked until tier 1",
          [b.disabled for b in view.children
           if b.custom_id == upgrade_id("chaos", 2)], [True])

    press = await click(view, "chaos", 1)
    check("the click answers with the bought text", last(press),
          uc.UPGRADE_BOUGHT.format(tier=1, effect=lv.effect("chaos", 1),
                                   price=ec.coins(500),
                                   balance=ec.coins(9_500)))
    check("the click bought the tier", (await tiers())["chaos"], 1)
    check("the click charged the price", await balance(), 9_500)
    check("the coin log names the category",
          [r["reason"] for r in
           await db.get_transactions(GUILD_ID, USER_ID, 1)],
          ["upgrade:chaos"])

    # ------------------------------------------------ the money actually moves
    # a loss takes the whole stake; the draw 0.5 is past tier 1's 36%
    ec._rng = Rigged(0.5)
    await eco.chaosroll.callback(eco, interaction, 100)
    check("0x message", last(interaction), lose_text(100, 9_400))
    check("0x takes the stake", await balance(), 9_400)
    check("the roll is in the coin log",
          [(r["reason"], r["amount"])
           for r in await db.get_transactions(GUILD_ID, USER_ID, 1)],
          [("chaos", -100)])

    # an immediate second roll is refused by the tier's cooldown
    try:
        await eco.chaosroll.callback(eco, interaction, 100)
        check("the cooldown refuses a second roll", "no error",
              "ChaosOnCooldown")
    except ec.ChaosOnCooldown as error:
        check("the cooldown refuses a second roll", True, True)
        check("and answers with the usual wait text",
              await eco.error_text(error),
              ec.COOLDOWN_MESSAGE.format(
                  seconds=max(1, round(error.seconds_left))))

    # a 1x hands the very same bet back and changes nothing
    await db.refund_chaos_cooldown(GUILD_ID, USER_ID)
    ec._rng = Rigged(0.02)
    await eco.chaosroll.callback(eco, interaction, 50)
    check("1x message", last(interaction), return_text(50, 9_400))
    check("1x is a net zero", await balance(), 9_400)

    # 2x..5x pay the net part of the multiplier
    for roll, multiplier, gained in ((0.15, 2, 50), (0.31, 3, 100),
                                     (0.34, 4, 150), (0.355, 5, 200)):
        await db.refund_chaos_cooldown(GUILD_ID, USER_ID)
        before = await balance()
        ec._rng = Rigged(roll)
        await eco.chaosroll.callback(eco, interaction, 50)
        check(f"{multiplier}x message", last(interaction),
              win_text(50, multiplier, before + gained))
        check(f"{multiplier}x pays the net {gained}", await balance(),
              before + gained)

    # a bet over the bought ceiling never reaches the cooldown or the wallet
    # (the check runs first, so even a spent cooldown answers with the ceiling)
    try:
        await eco.chaosroll.callback(eco, interaction, 101)
        check("a bet over tier 1 is refused", "no error", "BetTooHigh")
    except ec.BetTooHigh as error:
        check("a bet over tier 1 is refused", error.limit, 100)
        check("and names the ceiling",
              await eco.error_text(error),
              ec.ROLL_BET_TOO_HIGH.format(limit=ec.coins(100)))
    check("the refused bet took nothing", await balance(), 9_900)

    # a member who cannot afford the stake is refused AND gets the wait back
    await db.refund_chaos_cooldown(GUILD_ID, USER_ID)
    await drain()
    try:
        await eco.chaosroll.callback(eco, interaction, 10)
        check("a poor roll is refused", "no error", "InsufficientFunds")
    except ec.InsufficientFunds as error:
        check("a poor roll is refused", error.price, 10)
    check("a poor roll takes nothing", await balance(), 0)
    allowed, seconds_left = await db.spend_chaos_cooldown(
        GUILD_ID, USER_ID, lv.chaos_cooldown(1))
    check("a refused roll left the cooldown free", allowed, True)
    check("the next roll waits the whole cooldown", round(seconds_left), 180)
    await db.refund_chaos_cooldown(GUILD_ID, USER_ID)

    # ---------------------------------------------------- climbing the ladder
    await give(10_000)
    check("chaos tier 2 bought",
          (await db.buy_upgrade(GUILD_ID, USER_ID, "chaos", 2))[0], "ok")
    check("tier 2 raised the ceiling",
          lv.chaos_max_bet((await tiers())["chaos"]), 200)
    ec._rng = Rigged(0.5)
    await eco.chaosroll.callback(eco, interaction, 150)
    check("the bought ceiling is playable", await balance(), 8_350)
    try:
        await eco.chaosroll.callback(eco, interaction, 201)
        check("over tier 2 is refused", "no error", "BetTooHigh")
    except ec.BetTooHigh as error:
        check("over tier 2 is refused", error.limit, 200)

    check("chaos tier 3 bought",
          (await db.buy_upgrade(GUILD_ID, USER_ID, "chaos", 3))[0], "ok")
    await db.refund_chaos_cooldown(GUILD_ID, USER_ID)
    # tier 3 owns the 0.1% jackpot: 300 x 10 = 3000, a net of 2700
    ec._rng = Rigged(0.4505)
    await eco.chaosroll.callback(eco, interaction, 300)
    check("10x message", last(interaction), win_text(300, 10, 6_050))
    check("10x pays the net 2700", await balance(), 6_050)
    try:
        await eco.chaosroll.callback(eco, interaction, 301)
        check("over tier 3 is refused", "no error", "BetTooHigh")
    except ec.BetTooHigh as error:
        check("over tier 3 is refused", error.limit, 300)
    # tier 3 really rolls every 60 seconds, not the ladder's slowest wait
    allowed, seconds_left = await db.spend_chaos_cooldown(
        GUILD_ID, USER_ID, lv.chaos_cooldown(3))
    check("tier 3 cooldown is the fast one", allowed, False)
    check("and it lasts a minute", round(seconds_left), 60)
    await db.refund_chaos_cooldown(GUILD_ID, USER_ID)

    # --------------------------------------------- the feature flag closes it
    ec.CHAOS_ENABLED = False
    await eco.chaosroll.callback(eco, interaction, 10)
    check("a disabled chaosroll says so", last(interaction), ec.CHAOS_DISABLED)
    check("and takes nothing", await balance(), 6_050)
    ec.CHAOS_ENABLED = True

    print("\n" + ("ALL GOOD" if not failures
                  else f"FAILURES ({len(failures)}):\n- "
                       + "\n- ".join(failures)))
    os.remove(DB_PATH)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))



