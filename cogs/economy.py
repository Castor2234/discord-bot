"""Economy cog: coins, the daily reward, the shop and the /upgrade gamble.

Coins live in the users.balance column (see db.py), so the levels cog pays
into the same wallet that is spent here.
"""

import logging
import random
import time

import discord
from discord import app_commands
from discord.abc import Messageable
from discord.ext import commands

from db import (
    add_balance,
    add_shop_item,
    claim_daily,
    gamble,
    get_recent_purchases,
    get_shop_item,
    get_shop_items,
    get_user,
    hide_shop_item,
    purchase_item,
    remove_balance,
    restore_shop_item,
    set_purchase_status,
    set_shop_item_price,
)

# ------------------------------------------------------------------ settings
CURRENCY_NAME = "манго <:dota_mango:1554514974121009152>"
SHOP_LOG_CHANNEL_ID: int | None = None   # staff channel for new orders
SHOP_MAX_PRICE = 1_000_000               # largest price /shop setprice accepts
SHOP_CODE_MAX = 32                       # longest code /shop add accepts
SHOP_NAME_MAX = 100                      # longest name /shop add accepts
DAILY_ENABLED = True
DAILY_AMOUNT = 5
DAILY_INTERVAL = 24 * 3600               # seconds between two claims
PENDING_STATUS = "pending"
DELIVERED_STATUS = "delivered"

# /upgrade: bet UPGRADE_DEFAULT_BET (up to UPGRADE_MAX_BET), win it once more
# with UPGRADE_WIN_CHANCE, otherwise lose it
UPGRADE_ENABLED = True
UPGRADE_DEFAULT_BET = 5
UPGRADE_MAX_BET = 100
UPGRADE_WIN_CHANCE = 0.5
UPGRADE_COOLDOWN = 3.0                   # seconds between two tries of one member

# user facing texts
BALANCE_MESSAGE = "Баланс пользователя {mention}: **{balance}** {currency}."
SHOP_TITLE = "🛒 Магазин"
SHOP_EMPTY = "Магазин пуст."
SHOP_FOOTER = "Buy with `{prefix}shop buy <code>`."
SHOP_ITEM_ADDED = "✅ Товар **{name}** (`{code}`) добавлен за 🪙 {price}."
SHOP_ITEM_EXISTS = ("Товар с кодом `{code}` уже существует. Если он скрыт, "
                    "верните его командой `restore`.")
SHOP_ITEM_PRICED = "✅ Цена **{name}** (`{code}`): 🪙 {old} → 🪙 {new}."
SHOP_ITEM_HIDDEN = "🗑 Товар **{name}** (`{code}`) убран из магазина."
SHOP_ITEM_ALREADY_HIDDEN = "Товар **{name}** (`{code}`) и так не в магазине."
SHOP_ITEM_RESTORED = "♻️ Товар **{name}** (`{code}`) снова в магазине за 🪙 {price}."
SHOP_ITEM_ALREADY_ON_SALE = "Товар **{name}** (`{code}`) и так в магазине."
SHOP_ITEM_NOT_FOUND = "В магазине нет товара `{query}`."
SHOP_BAD_CODE = ("Код должен быть 1-{max} символов, без пробелов и не только "
                 "из цифр.")
SHOP_LINE = "**{name}** — 🪙 {price} (`{code}`)\n{description}"
PURCHASE_MESSAGE = ("🛒 You bought **{name}** for 🪙 {price}. "
                    "Balance: 🪙 {balance}. Order **#{order_id}**.")
PURCHASE_LOG = ("🛒 {mention} bought **{name}** for 🪙 {price} - "
                "order **#{order_id}** ({status})")
INSUFFICIENT_FUNDS = "Не хватает еще **{missing}** {currency} (цена: {price})."
ITEM_NOT_FOUND = "В магазине нет товара `{query}`. Доступные товары: {available}"
ORDERS_TITLE = "🧾 Последние заказы"
ORDERS_EMPTY = "Нет заказов."
ORDER_LINE = "**#{order_id}** {mention} — {name}, 🪙 {price}, `{status}`, {when}"
ORDER_UPDATED = "Заказ **#{order_id}** теперь `{status}`."
ORDER_NOT_FOUND = "Заказа **#{order_id}** не существует."
DAILY_CLAIMED = "<:pudge:1554517617434296320> Ежедневная награда получена: **{amount}** {currency}. Баланс: **{balance}** {currency}."
DAILY_WAITING = "Вы уже забрали ежедневную награду. Вернитесь спустя **{hours}ч {minutes}м**."
DAILY_DISABLED = "Ежедневная награда отключена."
UPGRADE_DESCRIPTION = (f"Рискни манго: "
                       f"{UPGRADE_WIN_CHANCE:.0%} удвоить, иначе потерять")
UPGRADE_BET = f"Сколько манго поставить (1-{UPGRADE_MAX_BET})"
UPGRADE_WIN = ("🎉 {mention} рискнул {bet} {currency} и **удвоил**! "
               "Выигрыш: **+{bet}** {currency}. Баланс: **{balance}** {currency}.")
UPGRADE_LOSE = ("💥 {mention} рискнул {bet} {currency} и **проиграл**. "
                "Потеряно: **-{bet}** {currency}. Баланс: **{balance}** {currency}.")
UPGRADE_DISABLED = "Улучшение отключено."
UPGRADE_COOLDOWN_MESSAGE = "Не так быстро! Попробуй ещё раз через **{seconds}** с."
ADMIN_MAX_AMOUNT = 1_000_000             # largest single /addcoins or /removecoins
ADMIN_ADDED = "✅ {mention} получает **{amount}** {currency}. Баланс: **{balance}** {currency}."
ADMIN_REMOVED = "✅ У {mention} снято **{removed}** {currency}. Баланс: **{balance}** {currency}."
ADMIN_REMOVED_PARTLY = ("✅ У {mention} было меньше, чем {amount}, снято только "
                        "**{removed}** {currency}. Баланс: **{balance}** {currency}.")
ADMIN_NOT_FOR_BOTS = "Ботам нельзя выдавать или снимать монеты."
PERMISSION_ERROR = "У тебя нет **Manage Server** прав для использования этой команды."
MEMBER_NOT_FOUND_ERROR = "Не нашел такого пользователя."
DATE_FORMAT = "%Y-%m-%d %H:%M"

# the draw comes from the operating system's randomness, not a seeded generator
_rng = random.SystemRandom()


def coins(amount: int) -> str:
    """Format a coin amount, e.g. ``🪙 10 coins``."""
    return f"🪙 {amount} {CURRENCY_NAME}"


class InsufficientFunds(commands.CommandError):
    """Raised when a member cannot afford a purchase."""

    def __init__(self, price: int, balance: int) -> None:
        self.price = price
        self.balance = balance
        super().__init__(f"balance {balance} is smaller than the price {price}")


class ItemNotFound(commands.CommandError):
    """Raised when a shop code does not exist."""

    def __init__(self, query: str) -> None:
        self.query = query
        super().__init__(f"unknown shop item {query!r}")


class Economy(commands.Cog):
    """Coins, the daily reward, the shop and /upgrade."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.log = logging.getLogger(__name__)

    # ---------------------------------------------------------------- wallet
    @app_commands.command(name="balance",
                          description="Show how many coins someone has")
    @app_commands.guild_only()
    async def balance(self, interaction: discord.Interaction,
                      member: discord.Member | None = None) -> None:
        """Show the coin balance of a member (yours by default)."""
        member = member or interaction.user
        row = await get_user(interaction.guild.id, member.id)
        embed = discord.Embed(
            description=BALANCE_MESSAGE.format(mention=member.mention,
                                               balance=row["balance"],
                                               currency=CURRENCY_NAME),
            color=discord.Color.gold())
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="daily", description="Claim your daily coins")
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction) -> None:
        """Claim the once per day coin reward."""
        if not DAILY_ENABLED:
            await interaction.response.send_message(DAILY_DISABLED,
                                                    ephemeral=True)
            return
        claimed, seconds_left, balance = await claim_daily(
            interaction.guild.id, interaction.user.id, DAILY_AMOUNT,
            DAILY_INTERVAL)
        if not claimed:
            hours, minutes = divmod(int(seconds_left) // 60, 60)
            await interaction.response.send_message(
                DAILY_WAITING.format(hours=hours, minutes=minutes),
                ephemeral=True)
            return
        await interaction.response.send_message(DAILY_CLAIMED.format(
            amount=DAILY_AMOUNT, currency=CURRENCY_NAME, balance=balance))

    # --------------------------------------------------------------- upgrade
    @app_commands.command(name="upgrade", description=UPGRADE_DESCRIPTION)
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, UPGRADE_COOLDOWN)
    @app_commands.describe(bet=UPGRADE_BET)
    async def upgrade(self, interaction: discord.Interaction,
                      bet: app_commands.Range[int, 1, UPGRADE_MAX_BET]
                      = UPGRADE_DEFAULT_BET) -> None:
        """Risk `bet` coins: double them or lose them."""
        if not UPGRADE_ENABLED:
            await interaction.response.send_message(UPGRADE_DISABLED,
                                                    ephemeral=True)
            return
        won = _rng.random() < UPGRADE_WIN_CHANCE
        played, balance = await gamble(interaction.guild.id,
                                       interaction.user.id, bet, won)
        if not played:
            raise InsufficientFunds(bet, balance)
        template = UPGRADE_WIN if won else UPGRADE_LOSE
        await interaction.response.send_message(template.format(
            mention=interaction.user.mention, bet=bet,
            currency=CURRENCY_NAME, balance=balance))

    # ----------------------------------------------------------------- admin
    @app_commands.command(name="addcoins",
                          description="Give coins to a member (staff)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def addcoins(
            self, interaction: discord.Interaction, member: discord.Member,
            amount: app_commands.Range[int, 1, ADMIN_MAX_AMOUNT]) -> None:
        """Give `amount` coins to `member`."""
        if member.bot:
            await interaction.response.send_message(ADMIN_NOT_FOR_BOTS,
                                                    ephemeral=True)
            return
        # the reason ends up in the coin log, so it shows who did it
        await add_balance(interaction.guild.id, member.id, amount,
                          reason=f"admin:{interaction.user.id}")
        balance = (await get_user(interaction.guild.id, member.id))["balance"]
        await interaction.response.send_message(ADMIN_ADDED.format(
            mention=member.mention, amount=amount, currency=CURRENCY_NAME,
            balance=balance))

    @app_commands.command(name="removecoins",
                          description="Take coins from a member (staff)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def removecoins(
            self, interaction: discord.Interaction, member: discord.Member,
            amount: app_commands.Range[int, 1, ADMIN_MAX_AMOUNT]) -> None:
        """Take up to `amount` coins from `member` (the balance stops at 0)."""
        if member.bot:
            await interaction.response.send_message(ADMIN_NOT_FOR_BOTS,
                                                    ephemeral=True)
            return
        removed = await remove_balance(interaction.guild.id, member.id, amount,
                                       reason=f"admin:{interaction.user.id}")
        balance = (await get_user(interaction.guild.id, member.id))["balance"]
        template = ADMIN_REMOVED if removed == amount else ADMIN_REMOVED_PARTLY
        await interaction.response.send_message(template.format(
            mention=member.mention, amount=amount, removed=removed,
            currency=CURRENCY_NAME, balance=balance))

    # ------------------------------------------------------------------ shop
    shop = app_commands.Group(name="shop",
                              description="Browse and buy shop items",
                              guild_only=True)

    @shop.command(name="list", description="Show everything that is for sale")
    async def shop_list(self, interaction: discord.Interaction) -> None:
        """Show everything that is for sale."""
        await self.send_catalogue(interaction)

    @shop.command(name="buy", description="Buy an item from the shop")
    async def shop_buy(self, interaction: discord.Interaction,
                       item: str) -> None:
        """Buy `item` (its code, e.g. `1x6`) with your coins."""
        shop_item, price, balance, order_id = await self.purchase(
            interaction.guild, interaction.user, item)
        await interaction.response.send_message(PURCHASE_MESSAGE.format(
            name=shop_item["name"], price=price, balance=balance,
            order_id=order_id), ephemeral=True)
        await self.notify_purchase(interaction.guild, interaction.user,
                                   shop_item, price, order_id)

    @shop.command(name="orders", description="Show recent orders (staff)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def shop_orders(self, interaction: discord.Interaction,
                          pending_only: bool = False) -> None:
        """List recent orders; `pending_only` shows undelivered ones only."""
        rows = await get_recent_purchases(
            interaction.guild.id, 10,
            PENDING_STATUS if pending_only else None)
        if not rows:
            await interaction.response.send_message(ORDERS_EMPTY,
                                                    ephemeral=True)
            return
        lines = []
        for row in rows:
            member = interaction.guild.get_member(row["user_id"])
            mention = member.mention if member else f"<@{row['user_id']}>"
            when = time.strftime(DATE_FORMAT,
                                 time.localtime(row["purchased_at"]))
            lines.append(ORDER_LINE.format(
                order_id=row["purchase_id"], mention=mention,
                name=row["item_name"] or row["item_code"], price=row["price"],
                status=row["status"], when=when))
        embed = discord.Embed(title=ORDERS_TITLE, description="\n".join(lines),
                              color=discord.Color.dark_gold())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @shop.command(name="fulfil",
                  description="Mark an order as delivered (staff)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def shop_fulfil(self, interaction: discord.Interaction,
                          order_id: int) -> None:
        """Mark order `order_id` as delivered."""
        # passing the guild id means a server can only touch its own orders
        if not await set_purchase_status(order_id, DELIVERED_STATUS,
                                         interaction.guild.id):
            await interaction.response.send_message(
                ORDER_NOT_FOUND.format(order_id=order_id), ephemeral=True)
            return
        await interaction.response.send_message(
            ORDER_UPDATED.format(order_id=order_id, status=DELIVERED_STATUS))

    @shop.command(name="add", description="Add an item to the shop (staff)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        code="Short unique code members type to buy it, e.g. 1x6",
        name="Display name of the item",
        price="Price in coins",
        description="Optional line shown under the item",
    )
    async def shop_add(self, interaction: discord.Interaction,
                       code: app_commands.Range[str, 1, SHOP_CODE_MAX],
                       name: app_commands.Range[str, 1, SHOP_NAME_MAX],
                       price: app_commands.Range[int, 1, SHOP_MAX_PRICE],
                       description: str = "") -> None:
        """Add a new item to the catalogue."""
        code = code.strip().lower()
        if not code or any(char.isspace() for char in code) or code.isdigit():
            await interaction.response.send_message(
                SHOP_BAD_CODE.format(max=SHOP_CODE_MAX), ephemeral=True)
            return
        row = await add_shop_item(code, name.strip(), description.strip(),
                                  price)
        if row is None:
            await interaction.response.send_message(
                SHOP_ITEM_EXISTS.format(code=code), ephemeral=True)
            return
        await interaction.response.send_message(SHOP_ITEM_ADDED.format(
            name=row["name"], code=row["code"], price=row["price"]),
            ephemeral=True)

    @shop.command(name="setprice",
                  description="Change the price of an item (staff)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(code="Code (or numeric id) of the item",
                           price="New price in coins")
    async def shop_setprice(
            self, interaction: discord.Interaction, code: str,
            price: app_commands.Range[int, 1, SHOP_MAX_PRICE]) -> None:
        """Change the price of an existing item, hidden or not."""
        query = code.strip()
        row = await set_shop_item_price(query, price)
        if row is None:
            await interaction.response.send_message(
                SHOP_ITEM_NOT_FOUND.format(query=query), ephemeral=True)
            return
        await interaction.response.send_message(SHOP_ITEM_PRICED.format(
            name=row["name"], code=row["code"], old=row["price"], new=price),
            ephemeral=True)

    @shop.command(name="hide",
                  description="Remove an item from the shop (staff)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(code="Code (or numeric id) of the item")
    async def shop_hide(self, interaction: discord.Interaction,
                        code: str) -> None:
        """Hide an item so it can no longer be bought (orders keep it)."""
        query = code.strip()
        row = await hide_shop_item(query)
        if row is None:
            await interaction.response.send_message(
                SHOP_ITEM_NOT_FOUND.format(query=query), ephemeral=True)
            return
        if not row["active"]:
            await interaction.response.send_message(
                SHOP_ITEM_ALREADY_HIDDEN.format(name=row["name"],
                                                code=row["code"]),
                ephemeral=True)
            return
        await interaction.response.send_message(SHOP_ITEM_HIDDEN.format(
            name=row["name"], code=row["code"]), ephemeral=True)

    @shop.command(name="restore",
                  description="Put a hidden item back on sale (staff)")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(code="Code (or numeric id) of the item")
    async def shop_restore(self, interaction: discord.Interaction,
                           code: str) -> None:
        """Bring a hidden item back to the catalogue."""
        query = code.strip()
        row = await restore_shop_item(query)
        if row is None:
            await interaction.response.send_message(
                SHOP_ITEM_NOT_FOUND.format(query=query), ephemeral=True)
            return
        if row["active"]:
            await interaction.response.send_message(
                SHOP_ITEM_ALREADY_ON_SALE.format(name=row["name"],
                                                 code=row["code"]),
                ephemeral=True)
            return
        await interaction.response.send_message(SHOP_ITEM_RESTORED.format(
            name=row["name"], code=row["code"], price=row["price"]),
            ephemeral=True)

    # ------------------------------------------------------------- internals
    async def send_catalogue(self, interaction: discord.Interaction) -> None:
        """Post the list of active shop items."""
        items = await get_shop_items()
        if not items:
            await interaction.response.send_message(SHOP_EMPTY)
            return
        embed = discord.Embed(
            title=SHOP_TITLE,
            description="\n\n".join(
                SHOP_LINE.format(name=item["name"], price=item["price"],
                                 code=item["code"],
                                 description=item["description"])
                for item in items),
            color=discord.Color.gold())
        embed.set_footer(text=SHOP_FOOTER.format(prefix="/"))
        await interaction.response.send_message(embed=embed)

    async def purchase(self, guild: discord.Guild, member: discord.Member,
                       query: str) -> tuple:
        """Buy `query` for `member`: coins and order change in ONE transaction.

        Returns (item, price, new_balance, order_id). Raises ItemNotFound or
        InsufficientFunds so the command layer stays free of branch logic.
        """
        item = await get_shop_item(query)   # active items only
        if item is None:
            raise ItemNotFound(query)
        status, order_id, price = await purchase_item(
            guild.id, member.id, item["item_id"])
        if status == "not_found":           # hidden or removed just now
            raise ItemNotFound(query)
        balance = (await get_user(guild.id, member.id))["balance"]
        if status == "insufficient":
            raise InsufficientFunds(price, balance)
        return item, price, balance, order_id

    @staticmethod
    def shop_log_channel(guild: discord.Guild) -> Messageable | None:
        """SHOP_LOG_CHANNEL_ID when it is usable, else the system channel."""
        if SHOP_LOG_CHANNEL_ID:
            channel = guild.get_channel(SHOP_LOG_CHANNEL_ID)
            if isinstance(channel, Messageable):
                return channel
        return guild.system_channel

    async def notify_purchase(self, guild: discord.Guild, member: discord.Member,
                              item, price: int, order_id: int) -> None:
        """Tell the staff about a new order (best effort)."""
        channel = self.shop_log_channel(guild)
        if channel is None:
            return
        try:
            await channel.send(PURCHASE_LOG.format(
                mention=member.mention, name=item["name"], price=price,
                order_id=order_id, status=PENDING_STATUS))
        except discord.HTTPException:
            pass  # the order is recorded, the log message is a bonus

    # ---------------------------------------------------------------- errors
    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError) -> None:
        """Handles the application command errors the tree forwards to the cog."""
        text = await self.error_text(unwrap_error(error))
        if text is None:
            raise error
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def error_text(self, error: BaseException) -> str | None:
        """Turn a command error into a message, or None when unexpected."""
        if isinstance(error, InsufficientFunds):
            return INSUFFICIENT_FUNDS.format(missing=error.price - error.balance,
                                            price=error.price,
                                            currency=CURRENCY_NAME)
        if isinstance(error, ItemNotFound):
            items = await get_shop_items()
            available = ", ".join(f"`{item['code']}`" for item in items) or "-"
            return ITEM_NOT_FOUND.format(query=error.query, available=available)
        # must come before the CheckFailure branch: a cooldown is a CheckFailure
        if isinstance(error, app_commands.CommandOnCooldown):
            return UPGRADE_COOLDOWN_MESSAGE.format(
                seconds=max(1, round(error.retry_after)))
        if isinstance(error, (commands.MissingPermissions, commands.CheckFailure,
                              app_commands.CheckFailure)):
            return PERMISSION_ERROR
        if isinstance(error, commands.MemberNotFound):
            return MEMBER_NOT_FOUND_ERROR
        if isinstance(error, commands.BadArgument):
            return f"Invalid argument: {error}"
        return None


def unwrap_error(error: BaseException) -> BaseException:
    """Peel CommandInvokeError/HybridCommandError wrappers off an error."""
    original = error
    for _ in range(5):
        inner = getattr(original, "original", None)
        if inner is None:
            break
        original = inner
    return original


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Economy(bot))