"""Economy cog: coins, the daily reward and the shop.

Coins live in the users.balance column (see db.py), so the levels cog pays
into the same wallet that is spent here.
"""

import logging
import time

import discord
from discord import app_commands
from discord.abc import Messageable
from discord.ext import commands

from db import (
    claim_daily,
    get_purchase,
    get_recent_purchases,
    get_shop_item,
    get_shop_items,
    get_user,
    record_purchase,
    set_purchase_status,
    spend_balance,
)

# ------------------------------------------------------------------ settings
CURRENCY_NAME = "манго <:dota_mango:1554514974121009152>"                  
SHOP_LOG_CHANNEL_ID: int | None = None   # staff channel for new orders
DAILY_ENABLED = True
DAILY_AMOUNT = 5
DAILY_INTERVAL = 24 * 3600               # seconds between two claims
PENDING_STATUS = "pending"
DELIVERED_STATUS = "delivered"

# user facing texts
BALANCE_MESSAGE = "Баланс пользователя {mention}: **{balance}** {currency}."
SHOP_TITLE = "🛒 Магазин"
SHOP_EMPTY = "Магазин пуст."
SHOP_FOOTER = "Buy with `{prefix}shop buy <code>`."
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
PERMISSION_ERROR = "У тебя нет **Manage Server** прав для использования этой команды."
MEMBER_NOT_FOUND_ERROR = "Не нашел такого пользователя."
DATE_FORMAT = "%Y-%m-%d %H:%M"


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
    """Coins, the daily reward and the shop."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.log = logging.getLogger(__name__)

    # ---------------------------------------------------------------- wallet
    @commands.hybrid_command(name="balance",
                             description="Show how many coins someone has")
    @commands.guild_only()
    async def balance(self, ctx: commands.Context,
                      member: discord.Member | None = None) -> None:
        """Show the coin balance of a member (yours by default)."""
        member = member or ctx.author
        row = await get_user(ctx.guild.id, member.id)
        embed = discord.Embed(
            description=BALANCE_MESSAGE.format(mention=member.mention,
                                               balance=row["balance"],
                                               currency=CURRENCY_NAME),
            color=discord.Color.gold())
        embed.set_thumbnail(url=member.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="daily", description="Claim your daily coins")
    @commands.guild_only()
    async def daily(self, ctx: commands.Context) -> None:
        """Claim the once per day coin reward."""
        if not DAILY_ENABLED:
            await ctx.send(DAILY_DISABLED, ephemeral=True)
            return
        claimed, seconds_left, balance = await claim_daily(
            ctx.guild.id, ctx.author.id, DAILY_AMOUNT, DAILY_INTERVAL)
        if not claimed:
            hours, minutes = divmod(int(seconds_left) // 60, 60)
            await ctx.send(DAILY_WAITING.format(hours=hours, minutes=minutes),
                           ephemeral=True)
            return
        await ctx.send(DAILY_CLAIMED.format(amount=DAILY_AMOUNT,
                                            currency=CURRENCY_NAME,
                                            balance=balance))

    # ------------------------------------------------------------------ shop
    @commands.hybrid_group(name="shop", description="Browse and buy shop items")
    @commands.guild_only()
    async def shop(self, ctx: commands.Context) -> None:
        """Show the shop catalogue (same as `/shop list`)."""
        await self.send_catalogue(ctx)

    @shop.command(name="list", description="Show everything that is for sale")
    @commands.guild_only()
    async def shop_list(self, ctx: commands.Context) -> None:
        """Show everything that is for sale."""
        await self.send_catalogue(ctx)

    @shop.command(name="buy", description="Buy an item from the shop")
    @commands.guild_only()
    async def shop_buy(self, ctx: commands.Context, item: str) -> None:
        """Buy `item` (its code, e.g. `1x6`) with your coins."""
        shop_item, balance, order_id = await self.purchase(
            ctx.guild, ctx.author, item)
        await ctx.send(PURCHASE_MESSAGE.format(name=shop_item["name"],
                                               price=shop_item["price"],
                                               balance=balance,
                                               order_id=order_id),
                       ephemeral=True)
        await self.notify_purchase(ctx.guild, ctx.author, shop_item, order_id)

    @shop.command(name="orders", description="Show recent orders (staff)")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def shop_orders(self, ctx: commands.Context,
                          pending_only: bool = False) -> None:
        """List recent orders; `pending_only` shows undelivered ones only."""
        rows = await get_recent_purchases(
            ctx.guild.id, 10, PENDING_STATUS if pending_only else None)
        if not rows:
            await ctx.send(ORDERS_EMPTY, ephemeral=True)
            return
        lines = []
        for row in rows:
            member = ctx.guild.get_member(row["user_id"])
            mention = member.mention if member else f"<@{row['user_id']}>"
            when = time.strftime(DATE_FORMAT, time.localtime(row["purchased_at"]))
            lines.append(ORDER_LINE.format(
                order_id=row["purchase_id"], mention=mention,
                name=row["item_name"] or row["item_code"], price=row["price"],
                status=row["status"], when=when))
        embed = discord.Embed(title=ORDERS_TITLE, description="\n".join(lines),
                              color=discord.Color.dark_gold())
        await ctx.send(embed=embed, ephemeral=True)

    @shop.command(name="fulfil", description="Mark an order as delivered (staff)")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def shop_fulfil(self, ctx: commands.Context, order_id: int) -> None:
        """Mark order `order_id` as delivered."""
        order = await get_purchase(order_id)
        if order is None or order["guild_id"] != ctx.guild.id:
            await ctx.send(ORDER_NOT_FOUND.format(order_id=order_id),
                           ephemeral=True)
            return
        await set_purchase_status(order_id, DELIVERED_STATUS)
        await ctx.send(ORDER_UPDATED.format(order_id=order_id,
                                            status=DELIVERED_STATUS))

    # ------------------------------------------------------------- internals
    async def send_catalogue(self, ctx: commands.Context) -> None:
        """Post the list of active shop items."""
        items = await get_shop_items()
        if not items:
            await ctx.send(SHOP_EMPTY)
            return
        embed = discord.Embed(
            title=SHOP_TITLE,
            description="\n\n".join(
                SHOP_LINE.format(name=item["name"], price=item["price"],
                                 code=item["code"],
                                 description=item["description"])
                for item in items),
            color=discord.Color.gold())
        embed.set_footer(text=SHOP_FOOTER.format(prefix=ctx.clean_prefix or "/"))
        await ctx.send(embed=embed)

    async def purchase(self, guild: discord.Guild, member: discord.Member,
                       query: str) -> tuple:
        """Spend coins for `query` and record the order.

        Returns (item, new_balance, order_id). Raises ItemNotFound or
        InsufficientFunds so the command layer stays free of branch logic.
        """
        item = await get_shop_item(query)
        if item is None or not item["active"]:
            raise ItemNotFound(query)
        if not await spend_balance(guild.id, member.id, item["price"]):
            balance = (await get_user(guild.id, member.id))["balance"]
            raise InsufficientFunds(item["price"], balance)
        order_id = await record_purchase(guild.id, member.id,
                                        item["item_id"], item["price"])
        balance = (await get_user(guild.id, member.id))["balance"]
        return item, balance, order_id

    @staticmethod
    def shop_log_channel(guild: discord.Guild) -> Messageable | None:
        """SHOP_LOG_CHANNEL_ID when it is usable, else the system channel."""
        if SHOP_LOG_CHANNEL_ID:
            channel = guild.get_channel(SHOP_LOG_CHANNEL_ID)
            if isinstance(channel, Messageable):
                return channel
        return guild.system_channel

    async def notify_purchase(self, guild: discord.Guild, member: discord.Member,
                              item, order_id: int) -> None:
        """Tell the staff about a new order (best effort)."""
        channel = self.shop_log_channel(guild)
        if channel is None:
            return
        try:
            await channel.send(PURCHASE_LOG.format(
                mention=member.mention, name=item["name"], price=item["price"],
                order_id=order_id, status=PENDING_STATUS))
        except discord.HTTPException:
            pass  # the order is recorded, the log message is a bonus

    # ---------------------------------------------------------------- errors
    async def cog_command_error(self, ctx: commands.Context,
                                error: commands.CommandError) -> None:
        """Handles failures of both the prefix and the slash invocation."""
        text = await self.error_text(unwrap_error(error))
        if text is None:
            raise error
        await ctx.send(text, ephemeral=True)

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
