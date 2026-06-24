"""
One-shot: clear ALL globally-registered slash commands for the bot.

Why this exists:
  The bot was originally synced globally (no DISCORD_GUILD_ID). Later we
  switched to guild-scoped sync for instant propagation. Both registrations
  are still live in Discord, which is why every command appears twice in
  the autocomplete.

  This script connects, calls bot.tree.sync(guild=None) with an empty tree,
  which removes all global commands. Guild commands are untouched.

Usage:
    docker compose exec discord_bot python scripts/clear_global_commands.py

After running once, autocomplete shows each command exactly once.
"""
from __future__ import annotations
import asyncio
import os
import sys

# Make the repo importable from /app/scripts/
sys.path.insert(0, "/app")

import discord
from discord.ext import commands


TOKEN = os.environ["DISCORD_BOT_TOKEN"]


async def main():
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        print(f"Logged in as {bot.user}")
        # Sync an empty global tree — wipes all global commands.
        bot.tree.clear_commands(guild=None)
        synced = await bot.tree.sync(guild=None)
        print(f"Global commands after wipe: {len(synced)}")
        await bot.close()

    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
