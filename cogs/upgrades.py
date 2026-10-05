"""Upgrades cog: the catalogue of coin upgrades and the buttons that buy them.

A member walks up a ladder in four categories - /daily, /roll, /transfer and
/chaosroll - and every tier is defined once in upgrade_levels.py, which the
economy cog imports too, so the limit a member paid for is exactly the limit
they get.

The catalogue is shown with buttons instead of a second command: buying is the
obvious follow-up to seeing the price, and one embed keeps both in one place.
The reply is ephemeral and the view only answers the member it was rendered
for, so nobody can ever spend somebody else`s mango.

`/userupgrades` looks at the same ladder for somebody else and is deliberately
read-only: it sends no buttons at all, because the buy view is bound to the one
member it was rendered for.

Buying goes through db.buy_upgrade, which takes the coins and raises the tier in
ONE transaction, so an upgrade can never be paid for and lost.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import upgrade_levels as levels
from db import buy_upgrade, get_user, get_user_upgrades
from cogs.economy import coins

# ------------------------------------------------------------------ settings
UPGRADES_ENABLED = True

# the ladders, in the order the embed lists them
CATEGORY_LABELS = {
    "daily": "🎁 Ежедневная награда — /daily",
    "roll": "🎲 /roll — ставка",
    "transfer": "💸 /transfer — лимит и перезарядка",
    "chaos": "🌀 /chaosroll — ставка, шансы и перезарядка",
}

# user facing texts
UPGRADES_TITLE = "⬆️ Улучшения"
UPGRADES_FOOTER = "Твой баланс: {balance} · купить кнопками ниже"
UPGRADES_DISABLED = "Улучшения отключены."
UPGRADE_DESCRIPTION = "Улучшить /daily, /roll, /transfer и /chaosroll за манго"
USER_UPGRADES_DESCRIPTION = "Показать улучшения участника"
USER_UPGRADES_MEMBER = "Чьи улучшения показать (по умолчанию — ваши)"
USER_UPGRADES_TITLE = "⬆️ Улучшения — {member}"
USER_UPGRADES_FOOTER = "Баланс: {balance}"

UPGRADE_BOUGHT = ("✅ Куплено ур. **{tier}**: {effect}. "
                  "Цена {price}. "
                  "Баланс: {balance}.")
UPGRADE_INSUFFICIENT = ("Не хватает {missing} "
                        "(нужно {price}, баланс {balance}).")
UPGRADE_SEQUENTIAL = "Сначала купи предыдущий уровень."
UPGRADE_OWNED = "Этот уровень уже куплен."
UPGRADE_MAXED = "Здесь выше ничего нет — максимальный уровень."
UPGRADE_NOT_YOURS = "Эта кнопка не для тебя."
UPGRADE_FAILED = "Не получилось купить улучшение."

# marks in the catalogue and on the buttons
UPGRADE_OWNED_MARK = "✅"
UPGRADE_NEXT_MARK = "💰"
UPGRADE_LOCKED_MARK = "🔒"

# what a button reads, e.g. "💰 Ур. 1 — 🥭 100 манго"; a label is plain text
# on Discord, so coins() must stay free of markdown
BUTTON_LABEL = "{mark} Ур. {tier} — {price}"
BUTTON_LABEL_OWNED = "✅ Ур. {tier}"


class Upgrades(commands.Cog):
    """The upgrade catalogue and the buttons that buy from it."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.log = logging.getLogger(__name__)

    # ---------------------------------------------------------------- command
    @app_commands.command(name="upgrades", description=UPGRADE_DESCRIPTION)
    @app_commands.guild_only()
    async def upgrades(self, interaction: discord.Interaction) -> None:
        """Show every upgrade you can still buy, and buy it with the buttons."""
        if not UPGRADES_ENABLED:
            await interaction.response.send_message(UPGRADES_DISABLED,
                                                    ephemeral=True)
            return
        tiers = await get_user_upgrades(interaction.guild.id,
                                        interaction.user.id)
        balance = (await get_user(interaction.guild.id,
                                  interaction.user.id))["balance"]
        await interaction.response.send_message(
            embed=self.build_embed(tiers, balance),
            view=UpgradeView(self, tiers, balance, interaction.user.id),
            ephemeral=True)

    @app_commands.command(name="userupgrades",
                          description=USER_UPGRADES_DESCRIPTION)
    @app_commands.guild_only()
    @app_commands.describe(member=USER_UPGRADES_MEMBER)
    async def userupgrades(self, interaction: discord.Interaction,
                           member: discord.Member | None = None) -> None:
        """Show the upgrades of a member (yours by default).

        Read-only on purpose: the buttons live on /upgrades, where the view is
        bound to the member it was rendered for. Sending a buy view here would
        let one member spend another member`s mango by accident.
        """
        if not UPGRADES_ENABLED:
            await interaction.response.send_message(UPGRADES_DISABLED,
                                                    ephemeral=True)
            return
        member = member or interaction.user
        tiers = await get_user_upgrades(interaction.guild.id, member.id)
        balance = (await get_user(interaction.guild.id, member.id))["balance"]
        embed = self.build_embed(
            tiers, balance,
            title=USER_UPGRADES_TITLE.format(member=member.display_name),
            footer=USER_UPGRADES_FOOTER)
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    # --------------------------------------------------------------- building
    def build_embed(self, tiers: dict, balance: int,
                    title: str = UPGRADES_TITLE,
                    footer: str = UPGRADES_FOOTER) -> discord.Embed:
        """The catalogue: one block per category, one line per tier.

        `title` and `footer` are swapped by the read-only view, which looks at
        somebody else`s ladder and must not promise buttons it does not send.
        """
        description = "\n\n".join(
            "**{}**\n{}".format(
                CATEGORY_LABELS[category],
                "\n".join(self.tier_line(category, tier, tiers[category])
                          for tier in range(levels.max_tier(category) + 1)))
            for category in levels.CATEGORIES)
        embed = discord.Embed(title=title, description=description,
                              color=discord.Color.purple())
        embed.set_footer(text=footer.format(balance=coins(balance)))
        return embed

    def tier_line(self, category: str, tier: int, owned: int) -> str:
        """One catalogue line: what the tier gives and whether it is reachable."""
        effect = levels.effect(category, tier)
        price = levels.price_for(category, tier)
        if tier == owned:
            return f"{UPGRADE_OWNED_MARK} **Ур. {tier}** — {effect} *(сейчас)*"
        if tier < owned:
            return f"{UPGRADE_OWNED_MARK} Ур. {tier} — {effect}"
        if tier == owned + 1:
            return f"{UPGRADE_NEXT_MARK} **Ур. {tier}** — {effect} — {coins(price)}"
        return f"{UPGRADE_LOCKED_MARK} Ур. {tier} — {effect} — {coins(price)}"

    # ----------------------------------------------------------------- buying
    async def buy(self, interaction: discord.Interaction, category: str,
                  tier: int) -> None:
        """Buy one tier and answer with either the refusal or the new embed."""
        price = levels.price_for(category, tier)
        if price is None:
            # a stale button from an older render: the answer only has to be
            # helpful, db.buy_upgrade would refuse it as well
            await interaction.response.send_message(
                UPGRADE_MAXED if tier > levels.max_tier(category)
                else UPGRADE_OWNED, ephemeral=True)
            return
        status, balance, tiers = await buy_upgrade(
            interaction.guild.id, interaction.user.id, category, tier)
        if status != "ok":
            await interaction.response.send_message(
                self.refusal_text(status, price, balance), ephemeral=True)
            return
        await interaction.response.send_message(
            UPGRADE_BOUGHT.format(tier=tier,
                                  effect=levels.effect(category, tier),
                                  price=coins(price),
                                  balance=coins(balance)),
            embed=self.build_embed(tiers, balance),
            view=UpgradeView(self, tiers, balance, interaction.user.id),
            ephemeral=True)

    def refusal_text(self, status: str, price: int, balance: int) -> str:
        """Why db.buy_upgrade said no, in the user's words."""
        if status == "insufficient":
            return UPGRADE_INSUFFICIENT.format(missing=coins(price - balance),
                                               price=coins(price),
                                               balance=coins(balance))
        if status == "sequential":
            return UPGRADE_SEQUENTIAL
        if status == "owned":
            return UPGRADE_OWNED
        if status == "maxed":
            return UPGRADE_MAXED
        self.log.warning("buy_upgrade refused with %r", status)
        return UPGRADE_FAILED


class UpgradeView(discord.ui.View):
    """A button per sellable tier, one row per category.

    Only `owner_id` is answered: the message is ephemeral, but the check keeps
    a copied custom_id from spending somebody else`s mango.
    """

    def __init__(self, cog: Upgrades, tiers: dict, balance: int,
                 owner_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.owner_id = owner_id
        row = 0
        for category in levels.CATEGORIES:
            owned = tiers[category]
            for tier in range(1, levels.max_tier(category) + 1):
                price = levels.price_for(category, tier)
                mark = (UPGRADE_NEXT_MARK if tier == owned + 1
                        else UPGRADE_LOCKED_MARK)
                button = discord.ui.Button(
                    label=BUTTON_LABEL.format(mark=mark, tier=tier,
                                            price=price),
                    style=discord.ButtonStyle.primary,
                    custom_id=f"upgrade:{category}:{tier}", row=row)
                if tier <= owned:
                    # already paid for: nothing left to do with this button
                    button.label = BUTTON_LABEL_OWNED.format(tier=tier)
                    button.style = discord.ButtonStyle.success
                    button.disabled = True
                elif tier > owned + 1:
                    # only the very next step is buyable right now
                    button.style = discord.ButtonStyle.secondary
                    button.disabled = True
                button.callback = self.make_callback(category, tier)
                self.add_item(button)
            row += 1   # one row per category; three rows fits in Discord`s five

    def make_callback(self, category: str, tier: int):
        """The click handler for one button, bound to its category and tier."""

        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(UPGRADE_NOT_YOURS,
                                                        ephemeral=True)
                return
            # buy_upgrade is one short transaction, so answering straight away
            # is fast enough and keeps the flow on `interaction.response`
            await self.cog.buy(interaction, category, tier)

        return callback


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Upgrades(bot))
