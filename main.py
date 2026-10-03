import os, discord
from discord.ext import commands
from dotenv import load_dotenv
import logging
from db import init_db
import asyncio, json

load_dotenv()
token = os.getenv('DISCORD_TOKEN')
guild_id = int(os.getenv('GUILD_ID'))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESTART_FILE = os.path.join(BASE_DIR, "restart.json")

COGS = ("cogs.levels", "cogs.economy", "cogs.roles", "cogs.command_channels",
        "cogs.upgrades")

handler = logging.FileHandler(filename='discordbot.log', encoding='utf-8', mode='a')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="-", intents=intents)

guild = discord.Object(id=guild_id)

@bot.event
async def setup_hook():
    await init_db()
    for cog in COGS:
        try:
            await bot.load_extension(cog)
        except Exception as error:  # a broken cog must not take the whole bot down
            print(f"Cog {cog} failed to load: {error!r}")

@bot.event
async def on_ready():
    print(f"{bot.user.name} готов")

    if os.path.exists(RESTART_FILE):
        try:
            with open(RESTART_FILE) as f:
                channel_id = json.load(f)["channel_id"]
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            await channel.send(f"{bot.user.name} снова в строю")
        except Exception as error:
            print(f"Не смог отправить сообщние. Ошибка: {error!r}")
        finally:
            os.remove(RESTART_FILE)

@bot.command()
@commands.has_permissions(administrator = True)
async def admin(ctx):
    await ctx.send(f"Привет админ {ctx.author.mention}")

@admin.error
async def admin_error(ctx,error):
    if isinstance(error,commands.MissingPermissions):
        await ctx.send("Вы не админ")

@bot.command()
@commands.is_owner()
async def sync(ctx):
    """Sync slash commands to the guild."""
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    await ctx.send(f"Synced {len(synced)} commands.")

@bot.command()
@commands.is_owner()
async def gitpull(ctx):
    """Run git pull in the folder where main.py lives."""
    async with ctx.typing():
        proc = await asyncio.create_subprocess_exec(
            "git", "pull",
            cwd=BASE_DIR,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # fail instead of hanging on a password prompt
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            return await ctx.send("git pull timed out.")

    text = out.decode("utf-8", errors="replace").strip() or "(no output)"
    await ctx.send(f"```\n{text[:1900]}\n```")

@bot.command()
@commands.is_owner()
async def restart(ctx):
    await ctx.send("Перезапуск...")
    with open(RESTART_FILE, "w") as f:
        json.dump({"channel_id": ctx.channel.id}, f)
    await bot.close()


bot.run(token, log_handler=handler, log_level=logging.INFO)