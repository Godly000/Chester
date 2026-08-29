"""
Loot Box Discord Bot
=====================

Reads a two-tier weighted-random loot table from CSV files:

  data/rarities.csv       -> category,weight
  data/<category>.csv     -> item_name,image_url,weight   (one file per category)

On the /chest command (or !chest), the bot:
  1. Picks a category using the weights in rarities.csv
  2. Picks an item from that category's CSV using the item weights
  3. Replies with an embed showing the category, item name, and image

Setup
-----
1. pip install -r requirements.txt
2. Copy .env.example to .env and add your bot token:
       DISCORD_TOKEN=your-token-here
3. Make sure the `data/` folder (with rarities.csv and the category CSVs)
   sits next to bot.py.
4. python bot.py

The bot needs the "applications.commands" and "bot" scopes when invited,
with at least the "Send Messages" and "Embed Links" permissions.
"""

import csv
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

DISCORD_TOKEN = os.getenv("MTU0MzExMjk5MDQ2MTY2MTIwNA.G7kdC6.A9T9GSFTuL-VNAKuZ93dUPhLr0pcsgIVuFpWeo")
DATA_DIR = Path(__file__).parent / "data"
RARITIES_FILE = DATA_DIR / "rarities.csv"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("loot_bot")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class LootItem:
    name: str
    image_url: str
    weight: float


@dataclass
class Category:
    name: str
    weight: float
    items: List[LootItem]


def _read_weighted_csv_rows(path: Path) -> List[List[str]]:
    """Read a CSV file with no header row, ignoring blank lines."""
    if not path.exists():
        raise FileNotFoundError(f"Expected data file not found: {path}")

    rows: List[List[str]] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            row = [cell.strip() for cell in row if cell.strip() != ""]
            if not row:
                continue
            rows.append(row)
    return rows


def load_categories() -> Dict[str, Category]:
    """
    Load rarities.csv (category,weight) and, for every category, its
    matching <category>.csv (item_name,image_url,weight).
    """
    categories: Dict[str, Category] = {}

    for row in _read_weighted_csv_rows(RARITIES_FILE):
        if len(row) < 2:
            log.warning("Skipping malformed rarities row: %r", row)
            continue

        name, weight_str = row[0], row[1]
        try:
            weight = float(weight_str)
        except ValueError:
            log.warning("Skipping rarities row with bad weight: %r", row)
            continue

        item_file = DATA_DIR / f"{name}.csv"
        items: List[LootItem] = []
        try:
            for item_row in _read_weighted_csv_rows(item_file):
                if len(item_row) < 3:
                    log.warning("Skipping malformed item row in %s: %r", item_file.name, item_row)
                    continue
                item_name, image_url, item_weight_str = item_row[0], item_row[1], item_row[2]
                try:
                    item_weight = float(item_weight_str)
                except ValueError:
                    log.warning("Skipping item row with bad weight in %s: %r", item_file.name, item_row)
                    continue
                items.append(LootItem(name=item_name, image_url=image_url, weight=item_weight))
        except FileNotFoundError:
            log.warning("No item file found for category '%s' (expected %s)", name, item_file)

        if not items:
            log.warning("Category '%s' has no valid items and will be skipped.", name)
            continue

        categories[name] = Category(name=name, weight=weight, items=items)

    if not categories:
        raise RuntimeError("No valid categories were loaded. Check your CSV files in data/.")

    return categories


def weighted_choice(names: List[str], weights: List[float]) -> str:
    """Pick a single element from `names` using `weights` as relative weights."""
    return random.choices(names, weights=weights, k=1)[0]


def roll_loot(categories: Dict[str, Category]):
    """Perform the two-stage weighted roll and return (Category, LootItem)."""
    cat_names = list(categories.keys())
    cat_weights = [categories[n].weight for n in cat_names]
    chosen_cat_name = weighted_choice(cat_names, cat_weights)
    chosen_cat = categories[chosen_cat_name]

    item_names = [item.name for item in chosen_cat.items]
    item_weights = [item.weight for item in chosen_cat.items]
    chosen_item_name = weighted_choice(item_names, item_weights)
    chosen_item = next(i for i in chosen_cat.items if i.name == chosen_item_name)

    return chosen_cat, chosen_item


# Rarity -> embed color, purely cosmetic. Falls back to a neutral color
# if the category name isn't recognized.
RARITY_COLORS = {
    "common": discord.Color.light_gray(),
    "rare": discord.Color.blue(),
    "epic": discord.Color.purple(),
    "legendary": discord.Color.gold(),
}


def build_loot_embed(category: Category, item: LootItem) -> discord.Embed:
    color = RARITY_COLORS.get(category.name.lower(), discord.Color.green())
    embed = discord.Embed(
        title=item.name,
        description=f"Rarity: **{category.name.capitalize()}**",
        color=color,
    )
    if item.image_url:
        embed.set_image(url=item.image_url)
    embed.set_footer(text="🎁 Chest Opened")
    return embed


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Loaded once at startup; use the reload command to refresh without restarting.
categories: Dict[str, Category] = {}


@bot.event
async def on_ready():
    global categories
    try:
        categories = load_categories()
        log.info("Loaded categories: %s", ", ".join(categories.keys()))
    except Exception as e:
        log.exception("Failed to load loot tables: %s", e)

    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s).", len(synced))
    except Exception as e:
        log.exception("Failed to sync slash commands: %s", e)

    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id if bot.user else "?")


async def _do_loot_roll() -> discord.Embed:
    if not categories:
        raise RuntimeError("Loot tables are not loaded. Try `/reload_loot` or restart the bot.")
    category, item = roll_loot(categories)
    return build_loot_embed(category, item)


@bot.tree.command(name="chest", description="Open a chest and get a random item!")
async def chest_slash(interaction: discord.Interaction):
    try:
        embed = await _do_loot_roll()
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        log.exception("Error rolling loot: %s", e)
        await interaction.response.send_message(f"⚠️ Something went wrong: {e}", ephemeral=True)


@bot.command(name="chest")
async def chest_prefix(ctx: commands.Context):
    try:
        embed = await _do_loot_roll()
        await ctx.send(embed=embed)
    except Exception as e:
        log.exception("Error rolling loot: %s", e)
        await ctx.send(f"⚠️ Something went wrong: {e}")


@bot.tree.command(name="reload_loot", description="Reload the loot tables from CSV without restarting the bot.")
async def reload_loot(interaction: discord.Interaction):
    global categories
    try:
        categories = load_categories()
        await interaction.response.send_message(
            f"✅ Reloaded {len(categories)} categories: {', '.join(categories.keys())}",
            ephemeral=True,
        )
    except Exception as e:
        log.exception("Error reloading loot tables: %s", e)
        await interaction.response.send_message(f"⚠️ Failed to reload: {e}", ephemeral=True)


def main():
    if not DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Create a .env file (see .env.example) "
            "or set the DISCORD_TOKEN environment variable."
        )
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
