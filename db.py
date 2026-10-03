"""Database layer of the levels / economy bot (SQLite through aiosqlite).

Sections: setup, users, levels, economy, shop, reaction roles, guild settings.

Conventions
- Every function opens its own short-lived connection through `_connect()`.
- Anything that has to be all-or-nothing (buying, paying) happens inside ONE
  transaction, so a crash can never leave coins taken without an order.
- Coin changes are written to the `transactions` table in the same transaction
  as the balance change, so the log can never disagree with the balances.
- Timestamps are unix seconds (REAL).
"""
import time

import aiosqlite

import upgrade_levels as levels

DB_PATH = "bot.db"
DB_TIMEOUT = 10  # seconds to wait for a locked database before giving up

# the catalogue the bot starts with; see the Shop section
STARTING_ITEM_CODE = "1x6"
STARTING_ITEM_NAME = "1 игра в 1x6"
STARTING_ITEM_DESCRIPTION = "Одна игра в кастомку 1x6"
STARTING_ITEM_PRICE = 40

# statuses an order may have; extend this set if you need more
PURCHASE_STATUSES = {"pending", "delivered", "cancelled"}


def _connect():
    """A new connection. Use it as `async with _connect() as db:`."""
    return aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT)


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

async def _ensure_column(db, table, column, definition):
    """Add `column` to `table` when a database predates it (idempotent).

    Returns True when the column had to be added.
    """
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        columns = {row[1] for row in await cur.fetchall()}
    if column in columns:
        return False
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return True


async def init_db():
    async with _connect() as db:
        # WAL lets readers and a writer work at the same time; the setting is
        # stored in the database file, so it only has to be made once
        async with db.execute("PRAGMA journal_mode=WAL") as cur:
            await cur.fetchone()

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                guild_id INTEGER, user_id INTEGER,
                xp INTEGER DEFAULT 0, level INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0,
                last_daily REAL DEFAULT 0, last_xp REAL DEFAULT 0,
                last_voice_xp REAL DEFAULT 0,
                last_transfer REAL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )""")
        # databases created before voice XP exist without this column
        await _ensure_column(db, "users", "last_voice_xp", "REAL DEFAULT 0")
        # the transfer cooldown is newer than the databases already in the wild
        await _ensure_column(db, "users", "last_transfer", "REAL DEFAULT 0")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_guild_xp"
                         " ON users (guild_id, xp DESC)")

        # one row per member; every tier is 0 until it is bought, which is the
        # free level everybody starts on
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_upgrades (
                guild_id INTEGER, user_id INTEGER,
                daily_tier INTEGER NOT NULL DEFAULT 0,
                roll_tier INTEGER NOT NULL DEFAULT 0,
                transfer_tier INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )""")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS shop_items (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                price INTEGER NOT NULL,
                active INTEGER DEFAULT 1
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, user_id INTEGER, item_id INTEGER,
                price INTEGER, purchased_at REAL,
                status TEXT DEFAULT 'pending'
            )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_purchases_guild"
                         " ON purchases (guild_id, status, purchased_at)")

        # one row per coin movement: who, how much (negative = spent), why
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                tx_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                reason TEXT NOT NULL,
                other_user_id INTEGER,
                created_at REAL NOT NULL
            )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user"
                         " ON transactions (guild_id, user_id, created_at)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reaction_roles (
                guild_id INTEGER, channel_id INTEGER, message_id INTEGER,
                emoji TEXT, role_id INTEGER, created_by INTEGER,
                created_at REAL,
                PRIMARY KEY (message_id, emoji)
            )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_reaction_roles_guild"
                         " ON reaction_roles (guild_id)")

        # defaults_seeded = 1 once the built-in defaults were given to a server
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                level_up_channel_id INTEGER,
                level_up_everywhere INTEGER NOT NULL DEFAULT 0,
                updated_at REAL,
                defaults_seeded INTEGER NOT NULL DEFAULT 0
            )""")
        added = await _ensure_column(
            db, "guild_settings", "defaults_seeded", "INTEGER NOT NULL DEFAULT 0")
        if added:
            # servers that already exist were configured under the old rules:
            # they own their settings and must not be seeded again
            await db.execute("UPDATE guild_settings SET defaults_seeded = 1")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS ignored_channels (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ignored_roles (
                guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            )""")

        # the channels the bot is allowed to answer commands in; an empty
        # table means the rule is off and every channel is allowed
        await db.execute("""
            CREATE TABLE IF NOT EXISTS command_channels (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )""")

        # seeding by the unique code keeps this idempotent and never
        # overwrites a price that was changed later
        await db.execute(
            """INSERT OR IGNORE INTO shop_items (code, name, description, price)
               VALUES (?, ?, ?, ?)""",
            (STARTING_ITEM_CODE, STARTING_ITEM_NAME, STARTING_ITEM_DESCRIPTION,
             STARTING_ITEM_PRICE))
        await db.commit()


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

async def ensure_user(db, guild_id, user_id):
    """Create a blank row for a new user (no-op when it exists)."""
    await db.execute(
        "INSERT OR IGNORE INTO users (guild_id, user_id) VALUES (?, ?)",
        (guild_id, user_id))


async def get_user(guild_id, user_id):
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)) as cur:
            return await cur.fetchone()


# --------------------------------------------------------------------------
# Levels
# --------------------------------------------------------------------------

# columns allowed to store an XP cooldown timestamp
_XP_COOLDOWN_COLUMNS = {"last_xp", "last_voice_xp"}


async def add_xp_with_cooldown(guild_id, user_id, amount, cooldown, column="last_xp"):
    """Award XP unless the user is still on cooldown.

    `column` selects the timer to use: ``last_xp`` for messages,
    ``last_voice_xp`` for voice time. The cooldown is stored as a unix
    timestamp and the update is a single conditional statement, so two events
    arriving at the same time cannot award XP twice.

    Returns (awarded, xp, level) where xp/level are the stored values after
    the (possible) update. The level is NOT recalculated here: the caller
    computes it from the XP and stores it with `set_level`.
    """
    if column not in _XP_COOLDOWN_COLUMNS:
        raise ValueError(f"unknown XP cooldown column {column!r}")
    now = time.time()
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""UPDATE users SET xp = xp + ?, {column} = ?
               WHERE guild_id=? AND user_id=? AND ? - {column} >= ?""",
            (amount, now, guild_id, user_id, now, cooldown))
        awarded = cursor.rowcount > 0
        await db.commit()
        async with db.execute(
            "SELECT xp, level FROM users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)) as cur:
            row = await cur.fetchone()
    return awarded, row["xp"], row["level"]


async def mark_voice_join(guild_id, user_id):
    """Restart the voice XP timer of a member (they joined a new channel)."""
    now = time.time()
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        await db.execute(
            "UPDATE users SET last_voice_xp = ? WHERE guild_id=? AND user_id=?",
            (now, guild_id, user_id))
        await db.commit()


async def set_xp(guild_id, user_id, xp, level):
    """Set XP and level together. Use this for admin commands like /setlevel
    (pass the XP threshold of the level), so the two values never disagree."""
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        await db.execute(
            "UPDATE users SET xp = ?, level = ? WHERE guild_id=? AND user_id=?",
            (max(0, xp), max(0, level), guild_id, user_id))
        await db.commit()


async def set_level(guild_id, user_id, level):
    """Store a level that was calculated from the current XP (level-up path).

    This changes ONLY the level. To set a level by hand use `set_xp`, since the
    level is derived from XP and would be recalculated on the next message.
    """
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        await db.execute(
            "UPDATE users SET level = ? WHERE guild_id=? AND user_id=?",
            (max(0, level), guild_id, user_id))
        await db.commit()


async def reset_xp(guild_id, user_id):
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        await db.execute(
            """UPDATE users SET xp = 0, level = 0, last_xp = 0, last_voice_xp = 0
               WHERE guild_id=? AND user_id=?""",
            (guild_id, user_id))
        await db.commit()


async def get_leaderboard(guild_id, limit=10):
    """Top `limit` users of the guild, already sorted by XP."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT user_id, xp, level FROM users
               WHERE guild_id=? AND xp > 0
               ORDER BY xp DESC, user_id ASC LIMIT ?""",
            (guild_id, limit)) as cur:
            return await cur.fetchall()


async def get_rank_position(guild_id, user_id):
    """1-based position of the user on the leaderboard (ties share a spot).

    Works for users without a row too (they count as 0 XP) and does not
    create one.
    """
    async with _connect() as db:
        async with db.execute(
            """SELECT COUNT(*) + 1 FROM users
               WHERE guild_id=? AND xp > COALESCE(
                   (SELECT xp FROM users WHERE guild_id=? AND user_id=?), 0)""",
            (guild_id, guild_id, user_id)) as cur:
            row = await cur.fetchone()
    return row[0]


# --------------------------------------------------------------------------
# Economy
# --------------------------------------------------------------------------

async def _log_tx(db, guild_id, user_id, amount, reason, other_user_id=None):
    """Write one line of the coin log on an already open connection.

    Call it before the commit of the balance change it describes.
    """
    if amount == 0:
        return
    await db.execute(
        """INSERT INTO transactions
           (guild_id, user_id, amount, reason, other_user_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (guild_id, user_id, amount, reason, other_user_id, time.time()))


async def get_transactions(guild_id, user_id, limit=10):
    """Newest coin movements of a user."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM transactions WHERE guild_id=? AND user_id=?
               ORDER BY created_at DESC, tx_id DESC LIMIT ?""",
            (guild_id, user_id, limit)) as cur:
            return await cur.fetchall()


async def add_balance(guild_id, user_id, amount, reason="add"):
    """Give coins. Negative amounts are rejected: use `spend_balance` (only
    when the user can afford it) or `remove_balance` (admin, stops at zero)."""
    if amount < 0:
        raise ValueError("amount must not be negative")
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
            (amount, guild_id, user_id))
        await _log_tx(db, guild_id, user_id, amount, reason)
        await db.commit()


async def remove_balance(guild_id, user_id, amount, reason="remove"):
    """Take up to `amount` coins, never going below zero (for admin commands).

    Returns how many coins were actually removed.
    """
    if amount < 0:
        raise ValueError("amount must not be negative")
    async with _connect() as db:
        # take the write lock first so the balance cannot change between the
        # read and the update
        await db.execute("BEGIN IMMEDIATE")
        await ensure_user(db, guild_id, user_id)
        async with db.execute(
            "SELECT balance FROM users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)) as cur:
            row = await cur.fetchone()
        removed = min(amount, row[0])
        await db.execute(
            "UPDATE users SET balance = balance - ? WHERE guild_id=? AND user_id=?",
            (removed, guild_id, user_id))
        await _log_tx(db, guild_id, user_id, -removed, reason)
        await db.commit()
    return removed


async def spend_balance(guild_id, user_id, amount, reason="spend"):
    """Take `amount` coins when the user can afford it.

    The affordability check and the update are one statement, so two purchases
    arriving at the same time cannot push the balance below zero. Returns True
    when the coins were taken. For shop purchases use `purchase_item`, which
    also records the order in the same transaction.
    """
    if amount < 0:
        raise ValueError("amount must not be negative")
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        cursor = await db.execute(
            """UPDATE users SET balance = balance - ?
               WHERE guild_id=? AND user_id=? AND balance >= ?""",
            (amount, guild_id, user_id, amount))
        spent = cursor.rowcount > 0
        if spent:
            await _log_tx(db, guild_id, user_id, -amount, reason)
        await db.commit()
    return spent


async def transfer(guild_id, from_id, to_id, amount):
    """Move coins from one member to another in a single transaction.

    Returns "ok", "invalid_amount", "self" or "insufficient". Either both
    balances change or neither does.
    """
    if amount <= 0:
        return "invalid_amount"
    if from_id == to_id:
        return "self"
    async with _connect() as db:
        await ensure_user(db, guild_id, from_id)
        await ensure_user(db, guild_id, to_id)
        cursor = await db.execute(
            """UPDATE users SET balance = balance - ?
               WHERE guild_id=? AND user_id=? AND balance >= ?""",
            (amount, guild_id, from_id, amount))
        if cursor.rowcount == 0:
            await db.rollback()
            return "insufficient"
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
            (amount, guild_id, to_id))
        await _log_tx(db, guild_id, from_id, -amount, "pay", to_id)
        await _log_tx(db, guild_id, to_id, amount, "pay", from_id)
        await db.commit()
    return "ok"


async def gamble(guild_id, user_id, stake, won, reason="roll"):
    """Settle a coin flip: +stake when `won`, -stake otherwise.

    The user must own at least `stake` coins. The check and the balance change
    are one statement, so fast repeated calls can never overdraw the wallet.
    The random draw itself is made by the caller.

    Returns (played, balance): played is False (and nothing changed) when the
    user could not afford the stake; balance is the wallet after the call.
    """
    if stake <= 0:
        raise ValueError("stake must be positive")
    delta = stake if won else -stake
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        cursor = await db.execute(
            """UPDATE users SET balance = balance + ?
               WHERE guild_id=? AND user_id=? AND balance >= ?""",
            (delta, guild_id, user_id, stake))
        played = cursor.rowcount > 0
        if played:
            await _log_tx(db, guild_id, user_id, delta, reason)
        await db.commit()
        async with db.execute(
            "SELECT balance FROM users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)) as cur:
            row = await cur.fetchone()
    return played, row[0]


async def claim_daily(guild_id, user_id, amount, cooldown):
    """Pay the daily coins unless the user already claimed them.

    Returns (claimed, seconds_left, new_balance).
    """
    now = time.time()
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """UPDATE users SET balance = balance + ?, last_daily = ?
               WHERE guild_id=? AND user_id=? AND ? - last_daily >= ?""",
            (amount, now, guild_id, user_id, now, cooldown))
        claimed = cursor.rowcount > 0
        if claimed:
            await _log_tx(db, guild_id, user_id, amount, "daily")
        await db.commit()
        async with db.execute(
            "SELECT balance, last_daily FROM users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)) as cur:
            row = await cur.fetchone()
    seconds_left = max(0.0, cooldown - (now - row["last_daily"]))
    return claimed, seconds_left, row["balance"]


async def spend_transfer_cooldown(guild_id, user_id, cooldown):
    """Take the transfer cooldown slot unless the member is still on cooldown.

    The timestamp is one conditional statement, so two transfers arriving at the
    same time cannot both pass. Returns (allowed, seconds_left).
    """
    now = time.time()
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        cursor = await db.execute(
            """UPDATE users SET last_transfer = ?
               WHERE guild_id=? AND user_id=? AND ? - last_transfer >= ?""",
            (now, guild_id, user_id, now, cooldown))
        allowed = cursor.rowcount > 0
        await db.commit()
        async with db.execute(
                "SELECT last_transfer FROM users WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)) as cur:
            row = await cur.fetchone()
    return allowed, max(0.0, cooldown - (now - row[0]))


async def refund_transfer_cooldown(guild_id, user_id):
    """Give the cooldown slot back when the transfer itself did not happen.

    Without this a refused transfer (not enough coins, sending to yourself)
    would still burn the wait, which reads as a bug to the member.
    """
    async with _connect() as db:
        await db.execute(
            "UPDATE users SET last_transfer = 0 WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        await db.commit()


# --------------------------------------------------------------------------
# Upgrades
#
# A member's tiers live in user_upgrades (one row per member, tier 0 = the free
# level). The catalogue itself - what a tier gives and what it costs - lives in
# upgrade_levels.py, so the command layer never hard-codes a limit.
# --------------------------------------------------------------------------

async def get_user_upgrades(guild_id, user_id):
    """The tiers of one member: {"daily": n, "roll": n, "transfer": n}.

    A member without a row has no upgrades at all, so a missing row means all
    zeros rather than an error. The row is created, because buying needs it.
    """
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        await db.execute(
            "INSERT OR IGNORE INTO user_upgrades (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id))
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM user_upgrades WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)) as cur:
            row = await cur.fetchone()
        await db.commit()
    return {name: row[column] for name, column in levels.COLUMNS.items()}


async def buy_upgrade(guild_id, user_id, category, tier):
    """Buy the step into `tier` of `category` for one member.

    The balance check, the deduction and the new tier are ONE transaction, so a
    crash can never take the coins without granting the upgrade. The tier only
    moves by one step, which is what makes the upgrade path sequential.

    Returns (status, balance, tiers). status is one of:
    - "ok"          - paid for and the tier is now `tier`
    - "unknown"     - no such category
    - "maxed"       - already at the top of the ladder
    - "owned"       - already owns that tier
    - "sequential"  - the tier before it has to be bought first
    - "insufficient"- not enough mango; nothing changed

    balance and tiers are the real stored values on every path, so a caller can
    report the outcome without asking again.
    """
    if category not in levels.COLUMNS:
        return "unknown", 0, {}
    column = levels.COLUMNS[category]      # a fixed name, never user input
    top = levels.max_tier(category)
    price = levels.price_for(category, tier)
    if price is None:
        # tier 0 is not sellable and anything past the top does not exist; the
        # caller tells those two apart with max_tier()
        status = "maxed" if tier > top else "owned"
        return status, 0, await get_user_upgrades(guild_id, user_id)
    status = "ok"
    async with _connect() as db:
        await ensure_user(db, guild_id, user_id)
        await db.execute(
            "INSERT OR IGNORE INTO user_upgrades (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id))
        db.row_factory = aiosqlite.Row
        async with db.execute(
                f"SELECT {column} AS tier FROM user_upgrades "
                "WHERE guild_id=? AND user_id=?", (guild_id, user_id)) as cur:
            row = await cur.fetchone()
        current = levels.clamp_tier(category, row["tier"] if row else 0)
        if current >= tier:
            status = "maxed" if current >= top else "owned"
            await db.commit()
        elif current != tier - 1:
            status = "sequential"
            await db.commit()
        else:
            cursor = await db.execute(
                f"""UPDATE users SET balance = balance - ?
                    WHERE guild_id=? AND user_id=? AND balance >= ?""",
                (price, guild_id, user_id, price))
            if cursor.rowcount == 0:
                status = "insufficient"
                await db.commit()
            else:
                await db.execute(
                    f"UPDATE user_upgrades SET {column} = ? "
                    "WHERE guild_id=? AND user_id=?",
                    (tier, guild_id, user_id))
                await _log_tx(db, guild_id, user_id, -price,
                              f"upgrade:{category}")
                await db.commit()
        async with db.execute(
                "SELECT balance FROM users WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)) as cur:
            balance = (await cur.fetchone())["balance"]
    return status, balance, await get_user_upgrades(guild_id, user_id)


# --------------------------------------------------------------------------
# Shop
# --------------------------------------------------------------------------

async def get_shop_items(active_only=True):
    """The catalogue, cheapest first."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM shop_items"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY price ASC, item_id ASC"
        async with db.execute(query) as cur:
            return await cur.fetchall()


async def get_shop_item(query, active_only=True):
    """Find an item by numeric id or by code (case-insensitive).

    Hidden (inactive) items are not found unless `active_only=False`.
    """
    text = str(query).strip()
    active_sql = " AND active = 1" if active_only else ""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        if text.isdigit():
            async with db.execute(
                "SELECT * FROM shop_items WHERE item_id=?" + active_sql,
                (int(text),)) as cur:
                row = await cur.fetchone()
            if row is not None:
                return row
        async with db.execute(
            "SELECT * FROM shop_items WHERE lower(code)=?" + active_sql,
            (text.lower(),)) as cur:
            return await cur.fetchone()


async def add_shop_item(code, name, description="", price=0):
    """Add an item to the catalogue and return the stored row.

    Returns None when `code` is already used. An item that was hidden by
    `hide_shop_item` still holds its code, so re-adding it fails too - use
    `restore_shop_item` for that one instead.
    """
    async with _connect() as db:
        try:
            cursor = await db.execute(
                """INSERT INTO shop_items (code, name, description, price,
                                           active)
                   VALUES (?, ?, ?, ?, 1)""",
                (code, name, description, price))
            await db.commit()
        except aiosqlite.IntegrityError:
            await db.rollback()
            return None
        item_id = cursor.lastrowid
    return await get_shop_item(item_id, active_only=False)


async def set_shop_item_price(query, price):
    """Change the price of an item found by code or numeric id.

    Returns the row as it was BEFORE the change (so the caller still sees the
    old price), or None when there is no such item.
    """
    item = await get_shop_item(query, active_only=False)
    if item is None:
        return None
    async with _connect() as db:
        await db.execute("UPDATE shop_items SET price=? WHERE item_id=?",
                         (price, item["item_id"]))
        await db.commit()
    return item


async def hide_shop_item(query):
    """Take an item off the shelf (active=0) by code or numeric id.

    The row is kept, so past orders still show the name and code. Returns the
    row as it was BEFORE the change (an `active` of 0 means it was already
    hidden), or None when there is no such item.
    """
    item = await get_shop_item(query, active_only=False)
    if item is None:
        return None
    async with _connect() as db:
        await db.execute("UPDATE shop_items SET active=0 WHERE item_id=?",
                         (item["item_id"],))
        await db.commit()
    return item


async def restore_shop_item(query):
    """Put a hidden item back on sale (active=1) by code or numeric id.

    Returns the row as it was BEFORE the change (an `active` of 1 means it was
    already on sale), or None when there is no such item.
    """
    item = await get_shop_item(query, active_only=False)
    if item is None:
        return None
    async with _connect() as db:
        await db.execute("UPDATE shop_items SET active=1 WHERE item_id=?",
                         (item["item_id"],))
        await db.commit()
    return item


async def purchase_item(guild_id, user_id, item_id):
    """Buy an item: take the coins AND store the order in one transaction.

    The price and the `active` flag are read inside the transaction, so a
    price change or a hidden item cannot slip through, and a crash cannot
    take coins without creating an order.

    Returns (status, order_id, price) where status is "ok", "not_found" or
    "insufficient". order_id is None unless the purchase succeeded.
    """
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT price FROM shop_items WHERE item_id=? AND active=1",
            (item_id,)) as cur:
            item = await cur.fetchone()
        if item is None:
            await db.rollback()
            return "not_found", None, None
        price = item["price"]
        await ensure_user(db, guild_id, user_id)
        cursor = await db.execute(
            """UPDATE users SET balance = balance - ?
               WHERE guild_id=? AND user_id=? AND balance >= ?""",
            (price, guild_id, user_id, price))
        if cursor.rowcount == 0:
            await db.rollback()
            return "insufficient", None, price
        cursor = await db.execute(
            """INSERT INTO purchases (guild_id, user_id, item_id, price,
                                      purchased_at, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (guild_id, user_id, item_id, price, time.time()))
        order_id = cursor.lastrowid
        await _log_tx(db, guild_id, user_id, -price, f"shop:{order_id}")
        await db.commit()
    return "ok", order_id, price


async def record_purchase(guild_id, user_id, item_id, price):
    """Store an order and return its number.

    Does not touch any balance. For real purchases use `purchase_item`.
    """
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO purchases (guild_id, user_id, item_id, price,
                                      purchased_at, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (guild_id, user_id, item_id, price, time.time()))
        await db.commit()
        return cursor.lastrowid


async def get_recent_purchases(guild_id, limit=10, status=None):
    """Newest orders of a guild, optionally filtered by status."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        query = """SELECT p.*, s.name AS item_name, s.code AS item_code
                   FROM purchases p
                   LEFT JOIN shop_items s ON s.item_id = p.item_id
                   WHERE p.guild_id = ?"""
        params: list = [guild_id]
        if status is not None:
            query += " AND p.status = ?"
            params.append(status)
        query += " ORDER BY p.purchased_at DESC, p.purchase_id DESC LIMIT ?"
        params.append(limit)
        async with db.execute(query, params) as cur:
            return await cur.fetchall()


async def get_purchase(purchase_id, guild_id=None):
    """One order. Pass `guild_id` so a server can only see its own orders."""
    query = """SELECT p.*, s.name AS item_name, s.code AS item_code
               FROM purchases p
               LEFT JOIN shop_items s ON s.item_id = p.item_id
               WHERE p.purchase_id = ?"""
    params: list = [purchase_id]
    if guild_id is not None:
        query += " AND p.guild_id = ?"
        params.append(guild_id)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            return await cur.fetchone()


async def set_purchase_status(purchase_id, status, guild_id=None):
    """Mark an order as e.g. 'delivered'. True when a row was changed.

    `status` must be one of PURCHASE_STATUSES. Pass `guild_id` so a server can
    only change its own orders.
    """
    if status not in PURCHASE_STATUSES:
        raise ValueError(f"unknown purchase status {status!r}")
    query = "UPDATE purchases SET status = ? WHERE purchase_id = ?"
    params: list = [status, purchase_id]
    if guild_id is not None:
        query += " AND guild_id = ?"
        params.append(guild_id)
    async with _connect() as db:
        cursor = await db.execute(query, params)
        await db.commit()
        return cursor.rowcount > 0


# --------------------------------------------------------------------------
# Reaction roles
# --------------------------------------------------------------------------

async def add_reaction_role(guild_id, channel_id, message_id, emoji, role_id,
                            created_by=None):
    """Map `emoji` of `message_id` to `role_id`, replacing an old mapping."""
    async with _connect() as db:
        await db.execute(
            """INSERT OR REPLACE INTO reaction_roles
               (guild_id, channel_id, message_id, emoji, role_id, created_by,
                created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, channel_id, message_id, emoji, role_id, created_by,
             time.time()))
        await db.commit()


async def get_reaction_role(message_id, emoji):
    """Mapping of one emoji on one message, None when there is none."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reaction_roles WHERE message_id=? AND emoji=?",
            (message_id, emoji)) as cur:
            return await cur.fetchone()


async def get_reaction_menu(message_id):
    """Every mapping of one message, ordered by emoji."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reaction_roles WHERE message_id=? ORDER BY emoji",
            (message_id,)) as cur:
            return await cur.fetchall()


async def get_reaction_menus(guild_id):
    """Every mapping of a guild, grouped menu by menu."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reaction_roles WHERE guild_id=?"
            " ORDER BY message_id ASC, emoji ASC", (guild_id,)) as cur:
            return await cur.fetchall()


async def delete_reaction_role(message_id, emoji):
    """Unmap one emoji. True when a mapping was removed."""
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM reaction_roles WHERE message_id=? AND emoji=?",
            (message_id, emoji))
        await db.commit()
        return cursor.rowcount > 0


async def delete_reaction_menu(message_id):
    """Unmap a whole message, returns how many mappings were dropped."""
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM reaction_roles WHERE message_id=?", (message_id,))
        await db.commit()
        return cursor.rowcount


async def delete_reaction_roles_for_role(guild_id, role_id):
    """Drop the mappings of a role that is gone, returns how many."""
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM reaction_roles WHERE guild_id=? AND role_id=?",
            (guild_id, role_id))
        await db.commit()
        return cursor.rowcount


# --------------------------------------------------------------------------
# Guild level settings
#
# One guild_settings row per server holds the level up channel, the two
# ignored_* tables hold the sets that mute XP. Everything is stored per server,
# so a second server the bot joins starts with a clean slate.
# --------------------------------------------------------------------------

async def ensure_guild_settings(guild_id, level_up_channel_id=None,
                                ignored_role_ids=(), ignored_channel_ids=()):
    """Give a server its starting settings, once, and never touch them again.

    A server whose `defaults_seeded` flag is 0 has never received the built-in
    defaults, so the first call seeds them. After that the server owns its
    settings: an empty ignore table means "cleared on purpose" and the
    defaults must not creep back in.

    The flag is flipped with a conditional UPDATE inside one transaction, so
    two calls racing each other seed the defaults exactly once, and a row that
    was created earlier by `set_level_up_channel` is still seeded (an
    explicitly chosen channel is kept).
    """
    async with _connect() as db:
        # fast path, no write lock: the common case after the first call
        async with db.execute(
            "SELECT defaults_seeded FROM guild_settings WHERE guild_id=?",
            (guild_id,)) as cur:
            row = await cur.fetchone()
        if row is not None and row[0]:
            return

        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """INSERT OR IGNORE INTO guild_settings
               (guild_id, level_up_channel_id, level_up_everywhere, updated_at)
               VALUES (?, ?, 0, ?)""",
            (guild_id, level_up_channel_id, time.time()))
        cursor = await db.execute(
            """UPDATE guild_settings SET defaults_seeded = 1
               WHERE guild_id=? AND defaults_seeded = 0""", (guild_id,))
        if cursor.rowcount == 0:
            await db.rollback()  # someone else seeded it in the meantime
            return
        for role_id in ignored_role_ids:
            await db.execute(
                "INSERT OR IGNORE INTO ignored_roles (guild_id, role_id)"
                " VALUES (?, ?)", (guild_id, role_id))
        for channel_id in ignored_channel_ids:
            await db.execute(
                "INSERT OR IGNORE INTO ignored_channels (guild_id, channel_id)"
                " VALUES (?, ?)", (guild_id, channel_id))
        await db.commit()


async def get_guild_settings(guild_id):
    """The settings row of a server, None while the server has nothing stored."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM guild_settings WHERE guild_id=?",
            (guild_id,)) as cur:
            return await cur.fetchone()


async def set_level_up_channel(guild_id, channel_id, everywhere=False):
    """Send level up messages to `channel_id`, None resets to no channel.

    `everywhere` (move the level ups earned in chat there too) only means
    something while a channel is set, so it is dropped with the channel.
    """
    async with _connect() as db:
        await db.execute(
            """INSERT INTO guild_settings
               (guild_id, level_up_channel_id, level_up_everywhere, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (guild_id) DO UPDATE
               SET level_up_channel_id = excluded.level_up_channel_id,
                   level_up_everywhere = excluded.level_up_everywhere,
                   updated_at = excluded.updated_at""",
            (guild_id, channel_id,
             int(bool(channel_id is not None and everywhere)), time.time()))
        await db.commit()


async def clear_level_up_channel(guild_id, channel_id):
    """Forget a level up channel that was deleted. True when it was set."""
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE guild_settings
               SET level_up_channel_id = NULL, level_up_everywhere = 0,
                   updated_at = ?
               WHERE guild_id=? AND level_up_channel_id=?""",
            (time.time(), guild_id, channel_id))
        await db.commit()
        return cursor.rowcount > 0


async def _add_ignored(table, column, guild_id, target_id):
    """Put one id into an ignore table. False when it was already there."""
    # the table and column names come from the helpers below, never from a user
    async with _connect() as db:
        cursor = await db.execute(
            f"INSERT OR IGNORE INTO {table} (guild_id, {column}) VALUES (?, ?)",
            (guild_id, target_id))
        await db.commit()
        return cursor.rowcount > 0


async def _remove_ignored(table, column, guild_id, target_id):
    """Take one id out of an ignore table. False when it was not there."""
    async with _connect() as db:
        cursor = await db.execute(
            f"DELETE FROM {table} WHERE guild_id=? AND {column}=?",
            (guild_id, target_id))
        await db.commit()
        return cursor.rowcount > 0


async def _list_ignored(table, column, guild_id):
    """The ids of one ignore table as a set, so the XP checks stay O(1)."""
    async with _connect() as db:
        async with db.execute(
                f"SELECT {column} FROM {table} WHERE guild_id=?",
                (guild_id,)) as cur:
            return {row[0] for row in await cur.fetchall()}


async def list_ignored_channels(guild_id):
    """Channels that earn no XP, both text and voice ones."""
    return await _list_ignored("ignored_channels", "channel_id", guild_id)


async def add_ignored_channel(guild_id, channel_id):
    """Stop XP in a channel. False when it was ignored already."""
    return await _add_ignored("ignored_channels", "channel_id",
                              guild_id, channel_id)


async def remove_ignored_channel(guild_id, channel_id):
    """Let a channel earn XP again. False when it was not ignored.

    Also call this from `on_guild_channel_delete` to drop stale ids.
    """
    return await _remove_ignored("ignored_channels", "channel_id",
                                 guild_id, channel_id)


async def list_ignored_roles(guild_id):
    """Roles whose holders earn no XP."""
    return await _list_ignored("ignored_roles", "role_id", guild_id)


async def add_ignored_role(guild_id, role_id):
    """Stop XP for a role. False when it was ignored already."""
    return await _add_ignored("ignored_roles", "role_id", guild_id, role_id)




# --------------------------------------------------------------------------
# Command channels
#
# The allow list of the channels the bot answers commands in. An empty set
# means the rule is off: the bot answers everywhere, so a server that never
# picked a channel keeps working exactly as before.
# --------------------------------------------------------------------------

async def list_command_channels(guild_id):
    """Channels the bot may answer commands in. Empty = the rule is off."""
    return await _list_ignored("command_channels", "channel_id", guild_id)


async def add_command_channel(guild_id, channel_id):
    """Let the bot answer commands in a channel. False when it already could."""
    return await _add_ignored("command_channels", "channel_id",
                              guild_id, channel_id)


async def remove_command_channel(guild_id, channel_id):
    """Stop answering commands in a channel. False when it was not allowed.

    Also call this from `on_guild_channel_delete` to drop stale ids.
    """
    return await _remove_ignored("command_channels", "channel_id",
                                 guild_id, channel_id)


async def clear_command_channels(guild_id):
    """Forget the whole allow list, which turns the rule off again.

    Returns how many channels were dropped.
    """
    async with _connect() as db:
        cursor = await db.execute(
            "DELETE FROM command_channels WHERE guild_id=?", (guild_id,))
        await db.commit()
        return cursor.rowcount

async def remove_ignored_role(guild_id, role_id):
    """Let a role earn XP again. False when it was not ignored.

    Also call this from `on_guild_role_delete` to drop stale ids.
    """
    return await _remove_ignored("ignored_roles", "role_id",
                                 guild_id, role_id)
