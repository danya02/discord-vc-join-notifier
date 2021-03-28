import discord
from discord.ext import commands

import os

TOKEN = os.getenv('DISCORD_TOKEN')

bot = commands.Bot(command_prefix=os.getenv('BOT_COMMAND_PREFIX') or '>')

@bot.command()
async def ping(ctx):
    await ctx.send('pong')


bot.run(TOKEN)
