import time

import aiosqlite

DB_PATH = "bot.db"

# the catalogue the bot starts with; see the Economy section at the bottom
STARTING_ITEM_CODE = "1x6"
STARTING_ITEM_NAME = "1 игра в 1x6"
STARTING_ITEM_DESCRIPTION = "Одна игра в кастомку 1x6"
STARTING_ITEM_PRICE = 10


async def _ensure_column(db, table, column, definition):
    """Add `column` to `table` when a database predates it (idempotent)."""
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        columns = {row[1] for row in await cur.fetchall()}
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                guild_id INTEGER, user_id INTEGER,
                xp INTEGER DEFAULT 0, level INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0,
                last_daily REAL DEFAULT 0, last_xp REAL DEFAULT 0,
                last_voice_xp REAL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )""")
        # databases created before voice XP exist without this column
        await _ensure_column(db, "users", "last_voice_xp", "REAL DEFAULT 0")
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reaction_roles (
                guild_id INTEGER, channel_id INTEGER, message_id INTEGER,
                emoji TEXT, role_id INTEGER, created_by INTEGER,
                created_at REAL,
                PRIMARY KEY (message_id, emoji)
            )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_reaction_roles_guild"
                         " ON reaction_roles (guild_id)")
        # seeding by the unique code keeps this idempotent and never
        # overwrites a price that was changed later
        await db.execute(
            """INSERT OR IGNORE INTO shop_items (code, name, description, price)
               VALUES (?, ?, ?, ?)""",
            (STARTING_ITEM_CODE, STARTING_ITEM_NAME, STARTING_ITEM_DESCRIPTION,
             STARTING_ITEM_PRICE))
        await db.commit()

async def ensure_user(db, guild_id, user_id):
    await db.execute(
        "INSERT OR IGNORE INTO users (guild_id, user_id) VALUES (?, ?)",
        (guild_id, user_id))

async def get_user(guild_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, guild_id, user_id)
        await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)) as cur:
            return await cur.fetchone()

async def add_balance(guild_id, user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, guild_id, user_id)
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE guild_id=? AND user_id=?",
            (amount, guild_id, user_id))
        await db.commit()


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
    the (possible) update.
    """
    if column not in _XP_COOLDOWN_COLUMNS:
        raise ValueError(f"unknown XP cooldown column {column!r}")
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, guild_id, user_id)
        await db.execute(
            "UPDATE users SET last_voice_xp = ? WHERE guild_id=? AND user_id=?",
            (now, guild_id, user_id))
        await db.commit()


async def set_xp(guild_id, user_id, xp, level):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, guild_id, user_id)
        await db.execute(
            "UPDATE users SET xp = ?, level = ? WHERE guild_id=? AND user_id=?",
            (max(0, xp), max(0, level), guild_id, user_id))
        await db.commit()


async def set_level(guild_id, user_id, level):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, guild_id, user_id)
        await db.execute(
            "UPDATE users SET level = ? WHERE guild_id=? AND user_id=?",
            (max(0, level), guild_id, user_id))
        await db.commit()


async def reset_xp(guild_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, guild_id, user_id)
        await db.execute(
            """UPDATE users SET xp = 0, level = 0, last_xp = 0, last_voice_xp = 0
               WHERE guild_id=? AND user_id=?""",
            (guild_id, user_id))
        await db.commit()


async def get_leaderboard(guild_id, limit=10):
    """Top `limit` users of the guild, already sorted by XP."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT user_id, xp, level FROM users
               WHERE guild_id=? AND xp > 0
               ORDER BY xp DESC, user_id ASC LIMIT ?""",
            (guild_id, limit)) as cur:
            return await cur.fetchall()


async def get_rank_position(guild_id, user_id):
    """1-based position of the user on the leaderboard (ties share a spot)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, guild_id, user_id)
        await db.commit()
        async with db.execute(
            """SELECT COUNT(*) + 1 FROM users
               WHERE guild_id=? AND xp > (
                   SELECT xp FROM users WHERE guild_id=? AND user_id=?)""",
            (guild_id, guild_id, user_id)) as cur:
            row = await cur.fetchone()
    return row[0]

# --------------------------------------------------------------------------
# Economy
# --------------------------------------------------------------------------

async def spend_balance(guild_id, user_id, amount):
    """Take `amount` coins when the user can afford it.

    The affordability check and the update are one statement, so two purchases
    arriving at the same time cannot push the balance below zero. Returns True
    when the coins were taken.
    """
    if amount < 0:
        raise ValueError("amount must not be negative")
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, guild_id, user_id)
        cursor = await db.execute(
            """UPDATE users SET balance = balance - ?
               WHERE guild_id=? AND user_id=? AND balance >= ?""",
            (amount, guild_id, user_id, amount))
        spent = cursor.rowcount > 0
        await db.commit()
    return spent


async def claim_daily(guild_id, user_id, amount, cooldown):
    """Pay the daily coins unless the user already claimed them.

    Returns (claimed, seconds_left, new_balance).
    """
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, guild_id, user_id)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """UPDATE users SET balance = balance + ?, last_daily = ?
               WHERE guild_id=? AND user_id=? AND ? - last_daily >= ?""",
            (amount, now, guild_id, user_id, now, cooldown))
        claimed = cursor.rowcount > 0
        await db.commit()
        async with db.execute(
            "SELECT balance, last_daily FROM users WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)) as cur:
            row = await cur.fetchone()
    seconds_left = max(0.0, cooldown - (now - row["last_daily"]))
    return claimed, seconds_left, row["balance"]


# --------------------------------------------------------------------------
# Shop
# --------------------------------------------------------------------------

async def get_shop_items(active_only=True):
    """The catalogue, cheapest first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM shop_items"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY price ASC, item_id ASC"
        async with db.execute(query) as cur:
            return await cur.fetchall()


async def get_shop_item(query):
    """Find an item by numeric id or by code (case-insensitive)."""
    text = str(query).strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if text.isdigit():
            async with db.execute(
                "SELECT * FROM shop_items WHERE item_id=?", (int(text),)) as cur:
                row = await cur.fetchone()
            if row is not None:
                return row
        async with db.execute(
            "SELECT * FROM shop_items WHERE lower(code)=?", (text.lower(),)) as cur:
            return await cur.fetchone()


async def record_purchase(guild_id, user_id, item_id, price):
    """Store a purchase and return its order number."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO purchases (guild_id, user_id, item_id, price,
                                      purchased_at, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (guild_id, user_id, item_id, price, time.time()))
        await db.commit()
        return cursor.lastrowid


async def get_recent_purchases(guild_id, limit=10, status=None):
    """Newest orders of a guild, optionally filtered by status."""
    async with aiosqlite.connect(DB_PATH) as db:
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


async def get_purchase(purchase_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT p.*, s.name AS item_name, s.code AS item_code
               FROM purchases p
               LEFT JOIN shop_items s ON s.item_id = p.item_id
               WHERE p.purchase_id = ?""",
            (purchase_id,)) as cur:
            return await cur.fetchone()


async def set_purchase_status(purchase_id, status):
    """Mark an order as e.g. 'delivered'. True when a row was changed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE purchases SET status = ? WHERE purchase_id = ?",
            (status, purchase_id))
        await db.commit()
        return cursor.rowcount > 0


# --------------------------------------------------------------------------
# Reaction roles
# --------------------------------------------------------------------------

async def add_reaction_role(guild_id, channel_id, message_id, emoji, role_id,
                            created_by=None):
    """Map `emoji` of `message_id` to `role_id`, replacing an old mapping."""
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reaction_roles WHERE message_id=? AND emoji=?",
            (message_id, emoji)) as cur:
            return await cur.fetchone()


async def get_reaction_menu(message_id):
    """Every mapping of one message, ordered by emoji."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reaction_roles WHERE message_id=? ORDER BY emoji",
            (message_id,)) as cur:
            return await cur.fetchall()


async def get_reaction_menus(guild_id):
    """Every mapping of a guild, grouped menu by menu."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reaction_roles WHERE guild_id=?"
            " ORDER BY message_id ASC, emoji ASC", (guild_id,)) as cur:
            return await cur.fetchall()


async def delete_reaction_role(message_id, emoji):
    """Unmap one emoji. True when a mapping was removed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM reaction_roles WHERE message_id=? AND emoji=?",
            (message_id, emoji))
        await db.commit()
        return cursor.rowcount > 0


async def delete_reaction_menu(message_id):
    """Unmap a whole message, returns how many mappings were dropped."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM reaction_roles WHERE message_id=?", (message_id,))
        await db.commit()
        return cursor.rowcount


async def delete_reaction_roles_for_role(guild_id, role_id):
    """Drop the mappings of a role that is gone, returns how many."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM reaction_roles WHERE guild_id=? AND role_id=?",
            (guild_id, role_id))
        await db.commit()
        return cursor.rowcount
