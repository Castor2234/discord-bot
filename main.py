import os, discord
from discord.ext import commands
from dotenv import load_dotenv
import logging
from db import init_db

load_dotenv()
token = os.getenv('DISCORD_TOKEN')

COGS = ("cogs.levels", "cogs.economy")

handler = logging.FileHandler(filename='discordbot.log', encoding='utf-8', mode='a')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

guild = discord.Object(id=1554266264149696573)

@bot.event
async def setup_hook():
    await init_db()
    for cog in COGS:
        try:
            await bot.load_extension(cog)
        except Exception as error:  # a broken cog must not take the whole bot down
            print(f"Cog {cog} failed to load: {error!r}")
    #await bot.tree.sync()
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

@bot.event
async def on_ready():
    print(f"{bot.user.name} готов")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if "пасхалка" in message.content.lower():
        await message.channel.send(f"{message.author.mention} - сам ты пасхалка")
    
    await bot.process_commands(message)

@bot.command()
@commands.has_permissions(administrator = True)
async def admin(ctx):
    await ctx.send(f"Hello admin {ctx.author.mention}")

@admin.error
async def admin_error(ctx,error):
    if isinstance(error,commands.MissingPermissions):
        await ctx.send("You do not have admin permissions")



bot.run(token, log_handler=handler, log_level=logging.INFO)