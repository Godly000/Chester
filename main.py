"""
Loot Box Discord Bot
=====================

Reads a two-tier weighted-random loot table from CSV files:

  data/rarities.csv       -> category,weight
  data/<category>.csv     -> item_name,image_url,weight[,extra]  (one file per category)

Weight columns may be plain numbers ("638") or comma-grouped ("1,923") --
both are accepted.

The `extra` 4th column is optional. If present, it is either:
  - a numeric range like "100-500": a random quantity is rolled from that
    range and shown in the output (e.g. "Gold x1,234"), or
  - text naming another CSV in data/ (without ".csv"), e.g. "heroskin":
    a random row is picked from data/heroskin.csv (name, image) and the
    ENTIRE entry is rerolled to that row -- both the name and the image
    are replaced outright (not merged/appended). For example, an item
    row "Hero Skin,placeholder.png,4211,heroskin" becomes whatever
    specific skin was drawn from heroskin.csv, e.g. "Gladiator King"
    with that skin's own image, not "Hero Skin: Gladiator King".

Referenced sub-table CSVs (e.g. heroskin.csv) may optionally start with
a header row like "Item,Image" -- it's detected and skipped automatically.

On the /chest command, the bot:
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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
ENV_PATH = SCRIPT_DIR / ".env"

# Load .env from the same folder as this script, regardless of the
# working directory the process was launched from (panels sometimes
# launch from a different cwd than the file's location).
try:
    load_dotenv(dotenv_path=ENV_PATH)
except UnicodeDecodeError:
    # Most likely the file was saved in a non-UTF-8 encoding (e.g. UTF-16
    # from some Windows editors). Fall through to the manual parser below.
    pass


def _manual_env_fallback(path: Path) -> None:
    """
    Last-resort .env parser, used only if python-dotenv didn't pick
    anything up (e.g. due to encoding quirks like a UTF-16/BOM save
    from some text editors, or python-dotenv not being installed).
    Sets plain KEY=VALUE lines into os.environ if not already set.
    """
    if not path.exists():
        return
    try:
        raw = path.read_bytes()
        # Strip a UTF-8 BOM if present, and try to decode generously.
        for encoding in ("utf-8-sig", "utf-8", "utf-16"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as e:
        log.warning("Manual .env fallback parse failed: %s", e)


# Accept a couple of common alternate names in case the hosting panel's
# environment variable was set up under a different key.
_TOKEN_ENV_KEYS = ("DISCORD_TOKEN", "TOKEN", "BOT_TOKEN", "DISCORD_BOT_TOKEN")

if not any(os.getenv(k) for k in _TOKEN_ENV_KEYS):
    _manual_env_fallback(ENV_PATH)

DISCORD_TOKEN = next((os.getenv(k) for k in _TOKEN_ENV_KEYS if os.getenv(k)), None)

DATA_DIR = SCRIPT_DIR / "data"
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
    # Optional 4th CSV column. Either:
    #  - a numeric range like "100-500" -> a random quantity is rolled and
    #    shown in the output, or
    #  - the name of another CSV in data/ (without ".csv") -> a random row
    #    from that file is picked and merged into this item's display.
    extra_field: Optional[str] = None


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


def _strip_header_row(rows: List[List[str]]) -> List[List[str]]:
    """
    Some referenced sub-table CSVs (e.g. heroskin.csv) start with a
    header row like "Item,Image". Detect and drop it so it isn't
    treated as an actual entry. Only triggers on an exact, case-insensitive
    match against common header labels, so real item rows are never
    mistaken for a header.
    """
    if not rows:
        return rows
    first = rows[0]
    if len(first) >= 2:
        name_hdr = first[0].strip().lower() in ("item", "name")
        image_hdr = first[1].strip().lower() in ("image", "image_url", "img")
        if name_hdr and image_hdr:
            return rows[1:]
    return rows


def _parse_weight(weight_str: str) -> float:
    """Parse a weight value that may use commas as thousands separators, e.g. '1,923'."""
    return float(weight_str.replace(",", "").strip())


def _find_data_csv(name: str) -> Optional[Path]:
    """
    Look up data/<name>.csv, case-insensitively, since CSV text fields
    might not exactly match a file's on-disk casing.
    """
    search_filename = f"{name}.csv"
    exact = DATA_DIR / search_filename
    if exact.exists():
        return exact
    target = search_filename.lower()
    if DATA_DIR.exists():
        for candidate in DATA_DIR.iterdir():
            if candidate.name.lower() == target:
                return candidate
    log.warning(
        "Loot table file not found. Searched for '%s' (case-insensitive) in '%s'.",
        search_filename, DATA_DIR,
    )
    return None


# Matches a numeric range like "100-500", "1000 - 2000", or comma-grouped
# numbers like "800,000-1,200,000".
_QUANTITY_RANGE_RE = re.compile(r"^\s*([\d,]+)\s*-\s*([\d,]+)\s*$")


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
            weight = _parse_weight(weight_str)
        except ValueError:
            log.warning("Skipping rarities row with bad weight: %r", row)
            continue

        item_file = DATA_DIR / f"{name}.csv"
        items: List[LootItem] = []
        try:
            item_rows = _strip_header_row(_read_weighted_csv_rows(item_file))
            for item_row in item_rows:
                if len(item_row) < 3:
                    log.warning("Skipping malformed item row in %s: %r", item_file.name, item_row)
                    continue
                item_name, image_url, item_weight_str = item_row[0], item_row[1], item_row[2]
                try:
                    item_weight = _parse_weight(item_weight_str)
                except ValueError:
                    log.warning("Skipping item row with bad weight in %s: %r", item_file.name, item_row)
                    continue
                # Optional 4th column. If it's not present, this is simply None
                # and the item is used as-is (no quantity, no sub-roll).
                extra_field = item_row[3] if len(item_row) >= 4 else None
                items.append(
                    LootItem(
                        name=item_name,
                        image_url=image_url,
                        weight=item_weight,
                        extra_field=extra_field,
                    )
                )
        except FileNotFoundError:
            log.warning(
                "Loot table file not found. Searched for '%s' in '%s' (category '%s').",
                item_file.name, DATA_DIR, name,
            )

        if not items:
            log.warning("Category '%s' has no valid items and will be skipped.", name)
            continue

        categories[name] = Category(name=name, weight=weight, items=items)

    if not categories:
        raise RuntimeError("No valid categories were loaded. Check your CSV files in data/.")

    return categories


def resolve_item_display(item: LootItem) -> "tuple[str, str]":
    """
    Apply the optional 4th CSV column, if present, and return the final
    (display_name, image_url) to show for this item.

      - No 4th column          -> unchanged name/image.
      - Numeric range ("A-B")  -> roll a random quantity in [A, B] and
                                   append it to the name, comma-formatted
                                   (e.g. "Gold x1,234").
      - Any other text ("X")   -> look up data/X.csv, pick a random row
                                   from it (name, image), override this
                                   item's image with that row's image, and
                                   append ": <name>" to this item's name
                                   (e.g. "Hero Equipment: Spiky Ball").
    """
    display_name = item.name
    image_url = item.image_url

    if not item.extra_field:
        return display_name, image_url

    extra = item.extra_field.strip()
    range_match = _QUANTITY_RANGE_RE.match(extra)

    if range_match:
        low, high = int(range_match.group(1).replace(",", "")), int(range_match.group(2).replace(",", ""))
        if low > high:
            low, high = high, low
        quantity = random.randint(low, high)
        display_name = f"{display_name} x{quantity:,}"
        return display_name, image_url

    # Otherwise, treat the field as a reference to another CSV in data/.
    sub_path = _find_data_csv(extra)
    if sub_path is None:
        # _find_data_csv already logged exactly what it searched for.
        return display_name, image_url

    try:
        sub_rows = _strip_header_row(_read_weighted_csv_rows(sub_path))
    except Exception as e:
        log.warning("Failed to read sub-table %s: %s", sub_path, e)
        return display_name, image_url

    if not sub_rows:
        log.warning("Sub-table %s has no rows; using the item as-is.", sub_path)
        return display_name, image_url

    chosen = random.choice(sub_rows)
    sub_name = chosen[0]
    sub_image = chosen[1] if len(chosen) >= 2 else image_url

    display_name = f"{display_name}: {sub_name}"
    image_url = sub_image
    return display_name, image_url


def weighted_choice(names: List[str], weights: List[float]) -> str:
    """Pick a single element from `names` using `weights` as relative weights."""
    return random.choices(names, weights=weights, k=1)[0]


def roll_item_from_category(category: Category) -> LootItem:
    """Perform a weighted item roll within a single, already-chosen category."""
    item_names = [item.name for item in category.items]
    item_weights = [item.weight for item in category.items]
    chosen_item_name = weighted_choice(item_names, item_weights)
    return next(i for i in category.items if i.name == chosen_item_name)


def roll_loot(categories: Dict[str, Category]):
    """Perform the two-stage weighted roll and return (Category, LootItem)."""
    cat_names = list(categories.keys())
    cat_weights = [categories[n].weight for n in cat_names]
    chosen_cat_name = weighted_choice(cat_names, cat_weights)
    chosen_cat = categories[chosen_cat_name]
    chosen_item = roll_item_from_category(chosen_cat)
    return chosen_cat, chosen_item


# Rarity -> embed color, purely cosmetic. Falls back to a neutral color
# if the category name isn't recognized.
RARITY_COLORS = {
    "common": discord.Color.light_gray(),
    "rare": discord.Color.blue(),
    "epic": discord.Color.purple(),
    "legendary": discord.Color.gold(),
}

# Shared HTTP session for pre-flight image checks, created once the bot
# is ready. NOTE ON LIMITATIONS: Discord's own client fetches and renders
# embed images itself once the message is delivered -- a bot has no way
# to force that client-side render to finish before the message appears.
# What we *can* do is verify the image URL is reachable and looks like an
# image before we send the embed at all, and let the user know in the
# reply if that check failed (likely a slow host or a dead link), rather
# than silently sending an embed with an image that may never load.
http_session: Optional[aiohttp.ClientSession] = None
IMAGE_CHECK_TIMEOUT = 4.0  # seconds


async def check_image_loads(url: str) -> bool:
    """Best-effort check that `url` responds quickly with image content."""
    if not url or http_session is None:
        return False
    try:
        timeout = aiohttp.ClientTimeout(total=IMAGE_CHECK_TIMEOUT)
        async with http_session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            content_type = resp.headers.get("Content-Type", "")
            return content_type.startswith("image/") or content_type == "application/octet-stream"
    except Exception:
        return False


async def build_loot_embed(category: Category, item: LootItem) -> discord.Embed:
    display_name, image_url = resolve_item_display(item)
    color = RARITY_COLORS.get(category.name.lower(), discord.Color.green())
    embed = discord.Embed(
        title=display_name,
        description=f"Rarity: **{category.name.capitalize()}**",
        color=color,
    )

    image_ok = True
    if image_url:
        image_ok = await check_image_loads(image_url)
        embed.set_image(url=image_url)

    if not image_ok:
        embed.add_field(
            name="⚠️ Heads up",
            value=(
                "This item's image couldn't be preloaded in time and may "
                "load slowly or fail to display."
            ),
            inline=False,
        )

    embed.set_footer(text="Made by __godly__")
    return embed


# ---------------------------------------------------------------------------
# #chester channel restriction
# ---------------------------------------------------------------------------
#
# Commands only respond inside a channel named "chester". Elsewhere:
#   - if a #chester channel exists in the server, the user gets a private,
#     dismissable notice telling them to use it there
#   - if no #chester channel exists at all, they get a private notice
#     telling an admin to create one

CHESTER_CHANNEL_NAME = "chester"


def _find_chester_channel(guild: Optional[discord.Guild]):
    """Look for a channel literally named 'chester' (case-insensitive) in the guild."""
    if guild is None:
        return None
    for ch in guild.channels:
        if getattr(ch, "name", "").lower() == CHESTER_CHANNEL_NAME:
            return ch
    return None


def _channel_gate_message(guild: Optional[discord.Guild], channel) -> Optional[str]:
    """
    Returns None if `channel` is the #chester channel (command allowed to
    proceed). Otherwise returns the notice message that should be shown.
    """
    current_name = getattr(channel, "name", "").lower()
    if current_name == CHESTER_CHANNEL_NAME:
        return None
    if _find_chester_channel(guild) is not None:
        return "Please use Chester only in the #chester channel"
    return "As an Admin to set up Chester by creating a #chester channel."


async def enforce_chester_channel(interaction: discord.Interaction) -> bool:
    """
    For slash commands. Sends an ephemeral notice (private to the user,
    with Discord's built-in dismiss button) and returns False if this
    isn't the #chester channel.
    """
    message = _channel_gate_message(interaction.guild, interaction.channel)
    if message is None:
        return True
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Loaded once at startup; use the reload command to refresh without restarting.
categories: Dict[str, Category] = {}


@bot.event
async def on_ready():
    global categories, http_session
    if http_session is None:
        http_session = aiohttp.ClientSession()

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
    return await build_loot_embed(category, item)


@bot.tree.command(name="chest", description="Open a chest and get a random item!")
@app_commands.checks.cooldown(1, 2.0)  # 1 use per 2 seconds, per user
async def chest_slash(interaction: discord.Interaction):
    if not await enforce_chester_channel(interaction):
        return
    try:
        # Defer immediately: the image pre-check below can take a couple of
        # seconds, and Discord requires an ack within 3 seconds of the
        # interaction arriving.
        await interaction.response.defer()
        embed = await _do_loot_roll()
        await interaction.followup.send(embed=embed)
    except Exception as e:
        log.exception("Error rolling loot: %s", e)
        await interaction.followup.send(f"⚠️ Something went wrong: {e}", ephemeral=True)


@chest_slash.error
async def chest_slash_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏳ Slow down! You can open another chest in {error.retry_after:.1f}s.",
            ephemeral=True,
        )
    else:
        log.exception("Unexpected error in /chest: %s", error)
        if interaction.response.is_done():
            await interaction.followup.send(f"⚠️ Something went wrong: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ Something went wrong: {error}", ephemeral=True)


async def rarity_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest rarity names currently loaded from rarities.csv."""
    current_lower = current.lower()
    return [
        app_commands.Choice(name=name, value=name)
        for name in categories.keys()
        if current_lower in name.lower()
    ][:25]


@bot.tree.command(name="test", description="[Admin] Force a roll from a specific rarity/category.")
@app_commands.describe(rarity="Which rarity/category to force a roll from")
@app_commands.autocomplete(rarity=rarity_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def test_slash(interaction: discord.Interaction, rarity: str):
    if not await enforce_chester_channel(interaction):
        return
    if not categories:
        await interaction.response.send_message(
            "⚠️ Loot tables are not loaded. Try `/reload_loot` first.", ephemeral=True
        )
        return

    # Case-insensitive match in case the user typed past the autocomplete list.
    match = next((name for name in categories if name.lower() == rarity.lower()), None)
    if match is None:
        await interaction.response.send_message(
            f"⚠️ Unknown rarity '{rarity}'. Valid options: {', '.join(categories.keys())}",
            ephemeral=True,
        )
        return

    chosen_cat = categories[match]
    try:
        await interaction.response.defer()
        chosen_item = roll_item_from_category(chosen_cat)
        embed = await build_loot_embed(chosen_cat, chosen_item)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        log.exception("Error forcing roll for rarity '%s': %s", match, e)
        if interaction.response.is_done():
            await interaction.followup.send(f"⚠️ Something went wrong: {e}", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ Something went wrong: {e}", ephemeral=True)


@test_slash.error
async def test_slash_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⚠️ You need Administrator permission to use `/test`.", ephemeral=True
        )
    else:
        log.exception("Unexpected error in /test: %s", error)
        await interaction.response.send_message(f"⚠️ Something went wrong: {error}", ephemeral=True)


@bot.tree.command(name="reload_loot", description="Reload the loot tables from CSV without restarting the bot.")
async def reload_loot(interaction: discord.Interaction):
    if not await enforce_chester_channel(interaction):
        return
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


@bot.tree.command(name="support", description="Show donation/support info.")
async def support_slash(interaction: discord.Interaction):
    if not await enforce_chester_channel(interaction):
        return
    # Plain message content can't render "[text](url)" as a clickable link --
    # only embeds can -- so this uses an embed description for a real
    # masked hyperlink. Sent ephemeral so it's private to the user and
    # shows Discord's built-in dismiss button.
    embed = discord.Embed(
        description=(
            "I'm accepting donations on [KoFi](https://ko-fi.com/god_ly). "
            "Your support helps fund hosting subscriptions to make me more powerful."
        ),
        color=discord.Color.pink(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="about", description="About Chester")
async def about_slash(interaction: discord.Interaction):
    if not await enforce_chester_channel(interaction):
        return
    embed = discord.Embed(
        description=(
            "Chester Alpha v0.0.1 created by __godly__ on August 30, 2026. "
            "Please submit bugs through her direct messages and use the "
            "/help command to see a list of all commands"
        ),
        color=discord.Color.blue(),
    )
    embed.set_image(url="https://media.ffycdn.net/eu/supercell/cE9WaY3WgjeuJ9ChgYkU.png?width=2400")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="help", description="Shows list of commands")
async def help_slash(interaction: discord.Interaction):
    if not await enforce_chester_channel(interaction):
        return
    embed = discord.Embed(
        description=(
            "/chest: Simulates opening one Treasure Chest from Clash of Clans.\n"
            "/test: Admin only. Forces an opening of a Treasure Chest at a specific rarity.\n"
            "/reload_loot: Admin only. Reloads the loot table, needed when the rewards "
            "change or mistakes are found in the files.\n"
            "/about: Shows bot version, version release date, and bot author information.\n"
            "/support: Gives a donation link which helps support the bot."
        ),
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(embed=embed)


def main():
    if not DISCORD_TOKEN:
        exists = ENV_PATH.exists()
        size = ENV_PATH.stat().st_size if exists else 0
        env_var_hits = [k for k in _TOKEN_ENV_KEYS if os.getenv(k)]
        raise SystemExit(
            "DISCORD_TOKEN is not set.\n"
            f"  - main.py is running from: {SCRIPT_DIR}\n"
            f"  - Looked for .env at:      {ENV_PATH}\n"
            f"      exists: {exists}, size: {size} bytes\n"
            f"  - Environment variables found among {_TOKEN_ENV_KEYS}: {env_var_hits or 'none'}\n"
            "\n"
            "Things to check:\n"
            "  1. The .env file must be in the SAME folder as main.py (see path above).\n"
            "  2. Open the .env file and confirm it looks exactly like:\n"
            "         DISCORD_TOKEN=your-actual-token\n"
            "     with no quotes, no 'export', and no leading/trailing spaces.\n"
            "  3. If size is 0 bytes, the file is empty — the token wasn't actually saved into it.\n"
            "  4. If your panel sets the token as a 'Variable' instead of a .env file, confirm the\n"
            "     key is spelled exactly DISCORD_TOKEN and that the panel injects it as a real\n"
            "     process environment variable (some panels only substitute variables into the\n"
            "     startup command string, not into os.environ)."
        )
    bot.run(DISCORD_TOKEN)
if __name__ == "__main__":
    main()
