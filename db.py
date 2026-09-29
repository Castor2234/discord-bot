import aiosqlite

DB_PATH = "bot.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                guild_id INTEGER, user_id INTEGER,
                xp INTEGER DEFAULT 0, level INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0,
                last_daily REAL DEFAULT 0, last_xp REAL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )""")
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