"""
Loot Box Discord Bot
=====================

Reads Town-Hall-specific two-tier weighted-random loot tables from CSV files:

  data/rarities.csv                              -> category,weight
  Town Hall Loot Tables/Town Hall N/<rarity>.csv -> item_name,weight[,extra]
  Town Hall Loot Tables/images.csv               -> item_name,image_url
  data/xp.csv                                    -> level,cumulative_xp_required

Weight columns may be plain numbers ("638") or comma-grouped ("1,923") --
both are accepted. Item rows no longer carry their own image column --
every item's image comes from a case-sensitive lookup of its name in
Town Hall Loot Tables/images.csv.

The `extra` 3rd column is optional. If present, it is either:
  - a numeric range like "100-500": a random quantity is rolled from that
    range and shown in the output (e.g. "Gold x1,234"), or
  - text naming another shared loot CSV (without ".csv"), e.g. "heroskin":
    a random row is picked from that file (name, image), that
    row's image overrides this item's image, and ": <name>" is appended
    to this item's name (e.g. "Hero Equipment: Spiky Ball").

Referenced sub-table CSVs (e.g. heroskin.csv) and images.csv may
optionally start with a header row like "Item,Image" -- it's detected
and skipped automatically.

On the /chest command, the bot:
  1. Selects the loot folder matching the player's Town Hall level
  2. Picks a category using the weights in rarities.csv
  3. Picks an item from that category's CSV using the item weights
  4. Stores the reward and rarity XP in the player's ordered binary save
     file inside saves/
  5. Replies with an embed showing the category, item name, image, and
     XP gained -- with a separate follow-up message if this pushed the
     player up a level (see data/xp.csv)

The /profile command shows every saved value in a required category.

Setup
-----
1. pip install -r requirements.txt
2. Copy .env.example to .env and add your bot token:
       DISCORD_TOKEN=your-token-here
3. Keep data next to this script and put the Town Hall loot folder beside it or inside data.
4. python main.py

The bot needs the "applications.commands" and "bot" scopes when invited,
with at least the "Send Messages" and "Embed Links" permissions.
"""

import asyncio
import csv
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from magic_system import MagicRejected, MagicSystem
from player_saves import MigrationReport, SaveSchemaMismatch, SaveStore
from resource_system import RESOURCE_TYPES, TREASURY_FIELDS, ResourceReceipt, ResourceRejected, ResourceSystem
from upgrade_system import (
    RefreshReport,
    UpgradeCurrencyChoiceRequired,
    UpgradeOutcome,
    UpgradeRejected,
    UpgradeSystem,
    WorkerUnavailable,
)

# ---------------------------------------------------------------------------
# Configuration / constants
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("loot_bot")

SCRIPT_DIR = Path(__file__).parent
ENV_PATH = SCRIPT_DIR / ".env"
DATA_DIR = SCRIPT_DIR / "data"
RARITIES_FILE = DATA_DIR / "rarities.csv"
IMAGES_FILE = DATA_DIR / "images.csv"
XP_LEVELS_FILE = DATA_DIR / "xp.csv"
PROGRESSION_DIR = DATA_DIR / "progression"
SAVES_DIR = SCRIPT_DIR / "saves"
SAVE_BACKUPS_DIR = SCRIPT_DIR / "save-backups"
TOWN_HALL_LOOT_DIR = SCRIPT_DIR / "Town Hall Loot Tables"
COMMON_EQUIPMENT_MAX = 18
EPIC_EQUIPMENT_MAX = 27
CLAN_CASTLE_RESOURCE_RATIO = 0.05

# XP awarded to the player who rolled, per rarity of the item they got.
XP_REWARDS = {
    "legendary": 4640,
    "epic": 1160,
    "rare": 290,
    "common": 160,
}

# Accept a couple of common alternate names in case the hosting panel's
# environment variable was set up under a different key.
_TOKEN_ENV_KEYS = ("DISCORD_TOKEN", "TOKEN", "BOT_TOKEN", "DISCORD_BOT_TOKEN")

# Only a channel literally named this responds to commands.
CHESTER_CHANNEL_NAME = "chester"
VILLAGE_WELCOME_MESSAGE = (
    "Welcome, Chief! Your village has been created, have fun opening chests!"
)

# Matches a numeric range like "100-500", "1000 - 2000", or comma-grouped
# numbers like "800,000-1,200,000".
_QUANTITY_RANGE_RE = re.compile(r"^\s*([\d,]+)\s*-\s*([\d,]+)\s*$")

# Rarity -> embed color, purely cosmetic. Falls back to a neutral color
# if the category name isn't recognized.
RARITY_COLORS = {
    "common": discord.Color.light_gray(),
    "rare": discord.Color.blue(),
    "epic": discord.Color.purple(),
    "legendary": discord.Color.gold(),
}

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


if not any(os.getenv(k) for k in _TOKEN_ENV_KEYS):
    _manual_env_fallback(ENV_PATH)

DISCORD_TOKEN = next((os.getenv(k) for k in _TOKEN_ENV_KEYS if os.getenv(k)), None)

# Optional: set this to a specific server's ID to sync commands there
# instantly instead of globally. Global syncs (the default) are confirmed
# by Discord's API right away but can take up to an hour to actually show
# up in every client -- a guild-scoped sync appears immediately, which is
# much faster for testing. Global sync still runs either way so the
# commands eventually appear everywhere else too.
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
save_store = SaveStore(PROGRESSION_DIR, SAVES_DIR)
resource_system = ResourceSystem(PROGRESSION_DIR, save_store, CLAN_CASTLE_RESOURCE_RATIO)
upgrade_system = UpgradeSystem(
    PROGRESSION_DIR,
    save_store,
    COMMON_EQUIPMENT_MAX,
    EPIC_EQUIPMENT_MAX,
    resource_system,
)
magic_system = MagicSystem(PROGRESSION_DIR, save_store, upgrade_system, resource_system)
save_migration_active = False


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class LootItem:
    name: str
    image_url: str
    weight: float
    data_dir: Path
    # Optional third CSV column
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


@dataclass
class ResolvedLoot:
    display_name: str
    image_url: str
    reward_name: str
    reward_amount: int
    collection_category: Optional[str] = None


class LootRollRejected(Exception):
    pass


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


def _find_data_csv(
    name: str,
    data_dir: Path = DATA_DIR,
    warn_missing: bool = True,
) -> Optional[Path]:
    """
    Look up data/<name>.csv, case-insensitively, since CSV text fields
    might not exactly match a file's on-disk casing.
    """
    search_filename = f"{name}.csv"
    exact = data_dir / search_filename
    if exact.is_file():
        return exact
    target = search_filename.lower()
    if data_dir.is_dir():
        for candidate in data_dir.iterdir():
            if candidate.is_file() and candidate.name.lower() == target:
                return candidate
    if warn_missing:
        log.warning(
            "Loot table file not found. Searched for '%s' (case-insensitive) in '%s'.",
            search_filename, data_dir,
        )
    return None


def load_images(images_file: Path = IMAGES_FILE) -> Dict[str, str]:
    """
    Load the item_name -> image_url lookup from images.csv. Lookups by
    item name are case-sensitive, matching the CSV exactly.
    """
    images: Dict[str, str] = {}
    try:
        rows = _strip_header_row(_read_weighted_csv_rows(images_file))
    except FileNotFoundError:
        log.warning(
            "Image lookup file not found. Searched for '%s' in '%s'.",
            images_file.name, images_file.parent,
        )
        return images
    for row in rows:
        if len(row) < 2:
            log.warning("Skipping malformed row in %s: %r", images_file.name, row)
            continue
        images[row[0]] = row[1]
    return images


def load_categories(
    loot_dir: Path = DATA_DIR,
    images_file: Path = IMAGES_FILE,
    rarities_file: Path = RARITIES_FILE,
    reference_dir: Optional[Path] = None,
) -> Dict[str, Category]:
    """
    Load rarities.csv (category,weight) and, for every category, its
    matching <category>.csv (item_name,weight[,extra]). Images come from
    the shared images.csv lookup, not from these files.
    """
    categories: Dict[str, Category] = {}
    reference_dir = reference_dir or loot_dir
    images = load_images(images_file)

    for row in _read_weighted_csv_rows(rarities_file):
        if len(row) < 2:
            log.warning("Skipping malformed rarities row: %r", row)
            continue

        name, weight_str = row[0], row[1]
        try:
            weight = _parse_weight(weight_str)
        except ValueError:
            log.warning("Skipping rarities row with bad weight: %r", row)
            continue
        if weight <= 0:
            log.warning("Skipping rarities row with non-positive weight: %r", row)
            continue

        item_file = _find_data_csv(name, loot_dir, warn_missing=False)
        if item_file is None:
            item_file = loot_dir / f"{name}.csv"
        items: List[LootItem] = []
        try:
            item_rows = _strip_header_row(_read_weighted_csv_rows(item_file))
            for item_row in item_rows:
                if len(item_row) < 2:
                    log.warning("Skipping malformed item row in %s: %r", item_file.name, item_row)
                    continue
                item_name, item_weight_str = item_row[0], item_row[1]
                try:
                    item_weight = _parse_weight(item_weight_str)
                except ValueError:
                    log.warning("Skipping item row with bad weight in %s: %r", item_file.name, item_row)
                    continue
                if item_weight <= 0:
                    log.warning(
                        "Skipping item row with non-positive weight in %s: %r",
                        item_file.name,
                        item_row,
                    )
                    continue
                # Optional 3rd column now (previously 4th, before the image
                # column was removed). If it's not present, this is simply
                # None and the item is used as-is (no quantity, no sub-roll).
                extra_field = item_row[2] if len(item_row) >= 3 else None

                image_url = images.get(item_name)
                if image_url is None:
                    uses_subtable = bool(
                        extra_field
                        and not _QUANTITY_RANGE_RE.match(extra_field.strip())
                    )
                    if not uses_subtable:
                        log.warning(
                            "No image found in images.csv for item '%s' "
                            "(case-sensitive lookup).",
                            item_name,
                        )
                    image_url = ""

                item_reference_dir = reference_dir
                if extra_field and not _QUANTITY_RANGE_RE.match(extra_field.strip()):
                    subtable_name = extra_field.strip()
                    if (
                        _find_data_csv(subtable_name, reference_dir, warn_missing=False) is None
                        and _find_data_csv(subtable_name, DATA_DIR, warn_missing=False) is not None
                    ):
                        item_reference_dir = DATA_DIR

                items.append(
                    LootItem(
                        name=item_name,
                        image_url=image_url,
                        weight=item_weight,
                        data_dir=item_reference_dir,
                        extra_field=extra_field,
                    )
                )
        except FileNotFoundError:
            log.warning(
                "Loot table file not found. Searched for '%s' in '%s' (category '%s').",
                item_file.name, loot_dir, name,
            )

        if not items:
            log.warning("Category '%s' has no valid items and will be skipped.", name)
            continue

        categories[name] = Category(name=name, weight=weight, items=items)

    if not categories:
        raise RuntimeError(f"No valid categories were loaded from {loot_dir}.")

    return categories


def _find_directory(name: str, parent: Path) -> Optional[Path]:
    exact = parent / name
    if exact.is_dir():
        return exact
    if parent.is_dir():
        for candidate in parent.iterdir():
            if candidate.is_dir() and candidate.name.casefold() == name.casefold():
                return candidate
    return None


def _resolve_town_hall_loot_root(
    expected_categories: List[str],
) -> Tuple[Path, Dict[int, Path]]:
    configured = os.getenv("TOWN_HALL_LOOT_DIR", "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute():
            configured_path = SCRIPT_DIR / configured_path
        candidates = [configured_path]
    else:
        candidates = [TOWN_HALL_LOOT_DIR, DATA_DIR / "Town Hall Loot Tables"]

    attempts: List[Tuple[Path, List[Path]]] = []
    for candidate in dict.fromkeys(candidates):
        root = _find_directory(candidate.name, candidate.parent) or candidate
        folders = {
            town_hall: _find_directory(f"Town Hall {town_hall}", root)
            or root / f"Town Hall {town_hall}"
            for town_hall in range(1, 19)
        }
        missing = [
            folder / f"{name}.csv"
            for folder in folders.values()
            for name in expected_categories
            if _find_data_csv(name, folder, warn_missing=False) is None
        ]
        if root.is_dir() and not missing:
            return root, folders
        attempts.append((root, missing))

    searched = "; ".join(str(root) for root, _ in attempts)
    best_root, missing = min(attempts, key=lambda attempt: len(attempt[1]))
    examples = ", ".join(str(path.relative_to(best_root)) for path in missing[:4])
    raise FileNotFoundError(
        f"Town Hall loot tables are missing or incomplete. Searched: {searched}. "
        f"Missing {len(missing)} required CSV files in {best_root}; examples: {examples}. "
        "Upload the complete 'Town Hall Loot Tables' folder beside main.py or inside data/. "
        "It must contain Town Hall 1 through Town Hall 18 with a CSV for each rarity. "
        "For a different location, set TOWN_HALL_LOOT_DIR to that folder."
    )


def load_town_hall_loot_tables() -> Dict[int, Dict[str, Category]]:
    loot_tables: Dict[int, Dict[str, Category]] = {}
    expected_categories = {
        row[0]
        for row in _read_weighted_csv_rows(RARITIES_FILE)
        if len(row) >= 2
    }
    loot_root, town_hall_folders = _resolve_town_hall_loot_root(sorted(expected_categories))
    images_file = (
        _find_data_csv("images", loot_root, warn_missing=False)
        or _find_data_csv("images", DATA_DIR, warn_missing=False)
        or loot_root / "images.csv"
    )
    log.info("Loading Town Hall loot tables from %s", loot_root)
    for town_hall, loot_dir in town_hall_folders.items():
        categories = load_categories(
            loot_dir=loot_dir,
            images_file=images_file,
            rarities_file=RARITIES_FILE,
            reference_dir=loot_root,
        )
        missing = expected_categories - categories.keys()
        if missing:
            raise RuntimeError(
                f"Town Hall {town_hall} is missing valid loot tables for: "
                f"{', '.join(sorted(missing))}"
            )
        loot_tables[town_hall] = categories
    return loot_tables


COLLECTION_REWARD_CATEGORIES = {
    "Capital House": "Clan Capital House Part",
    "Decoration": "Decoration",
    "Hero Equipment": "Hero Equipment",
    "Hero Skin": "Hero Skin",
}


def resolve_item_display(item: LootItem) -> ResolvedLoot:
    """
    Apply the optional third CSV column and return display and save values.

      - No third column        -> unchanged name/image and quantity one.
      - Numeric range ("A-B")  -> roll a random quantity in [A, B] and
                                   append it to the name, comma-formatted
                                   (e.g. "Gold x1,234").
      - Any other text ("X")   -> look up the shared X.csv, pick a random row
                                   from it (name, image), override this
                                   item's image with that row's image, and
                                   append ": <name>" to this item's name
                                   (e.g. "Hero Equipment: Spiky Ball").
    """
    display_name = item.name
    image_url = item.image_url

    if not item.extra_field:
        return ResolvedLoot(display_name, image_url, item.name, 1)

    extra = item.extra_field.strip()
    range_match = _QUANTITY_RANGE_RE.match(extra)

    if range_match:
        low, high = int(range_match.group(1).replace(",", "")), int(range_match.group(2).replace(",", ""))
        if low > high:
            low, high = high, low
        quantity = random.randint(low, high)
        display_name = f"{display_name} x{quantity:,}"
        return ResolvedLoot(display_name, image_url, item.name, quantity)

    # Otherwise, treat the field as a reference to another CSV in data/.
    sub_path = _find_data_csv(extra, item.data_dir)
    if sub_path is None:
        return ResolvedLoot(display_name, image_url, item.name, 1)

    try:
        sub_rows = _strip_header_row(_read_weighted_csv_rows(sub_path))
    except Exception as e:
        log.warning("Failed to read sub-table %s: %s", sub_path, e)
        return ResolvedLoot(display_name, image_url, item.name, 1)

    if not sub_rows:
        log.warning("Sub-table %s has no rows; using the item as-is.", sub_path)
        return ResolvedLoot(display_name, image_url, item.name, 1)

    chosen = random.choice(sub_rows)
    sub_name = chosen[0]
    sub_image = chosen[1] if len(chosen) >= 2 else image_url

    display_name = f"{display_name}: {sub_name}"
    image_url = sub_image
    return ResolvedLoot(
        display_name=display_name,
        image_url=image_url,
        reward_name=sub_name,
        reward_amount=1,
        collection_category=COLLECTION_REWARD_CATEGORIES.get(item.name),
    )


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


async def build_loot_embed(
    category: Category,
    item: LootItem,
    xp_reward: Optional[int] = None,
    resolved: Optional[ResolvedLoot] = None,
) -> discord.Embed:
    resolved = resolved or resolve_item_display(item)
    display_name = resolved.display_name
    image_url = resolved.image_url
    color = RARITY_COLORS.get(category.name.lower(), discord.Color.green())
    embed = discord.Embed(
        title=display_name,
        description=f"Rarity: **{category.name.capitalize()}**",
        color=color,
    )

    if not image_url:
        # No image at all -- the name wasn't found in images.csv (or in the
        # sub-table it was rerolled into), rather than a slow/broken URL.
        embed.add_field(
            name="❌ Image Error",
            value=f"No image could be found for \"{display_name}\".",
            inline=False,
        )
    else:
        embed.set_image(url=image_url)

    # xp_reward is only passed in for real /chest rolls, not /test (which is
    # an admin debugging tool, not real gameplay, so it doesn't grant XP).
    if xp_reward is not None:
        embed.add_field(name="XP Gained", value=f"+{xp_reward:,} XP", inline=False)

    embed.set_footer(text="Made by __godly__")
    return embed


# ---------------------------------------------------------------------------
# XP and leveling system
# ---------------------------------------------------------------------------


def load_xp_levels() -> List[int]:
    """
    Load data/xp.csv into a list where levels[i] is the cumulative XP
    required to reach level (i + 1). Returns [] if the file is missing
    or empty, in which case leveling is effectively disabled.
    """
    try:
        rows = _strip_header_row(_read_weighted_csv_rows(XP_LEVELS_FILE))
    except FileNotFoundError:
        log.warning(
            "XP level table not found. Searched for '%s' in '%s'.",
            XP_LEVELS_FILE.name, DATA_DIR,
        )
        return []

    parsed: List[Tuple[int, int]] = []
    for row in rows:
        if len(row) < 2:
            log.warning("Skipping malformed row in %s: %r", XP_LEVELS_FILE.name, row)
            continue
        try:
            level = int(row[0].strip())
            threshold = int(row[1].strip().replace(",", ""))
        except ValueError:
            log.warning("Skipping non-numeric row in %s: %r", XP_LEVELS_FILE.name, row)
            continue
        parsed.append((level, threshold))

    parsed.sort(key=lambda pair: pair[0])
    return [threshold for _level, threshold in parsed]


# Loaded once at startup; refreshed by /reload_loot alongside the loot tables.
xp_levels: List[int] = []


def xp_to_level(xp: int) -> int:
    """Return the level (1-indexed) reached by a given amount of total XP."""
    if not xp_levels:
        return 1
    level = 1
    for i, threshold in enumerate(xp_levels):
        if xp >= threshold:
            level = i + 1
        else:
            break
    return level


# ---------------------------------------------------------------------------
# #chester channel restriction
# ---------------------------------------------------------------------------
#
# Commands only respond inside a channel named "chester". Elsewhere:
#   - if a #chester channel exists in the server, the user gets a private,
#     dismissable notice telling them to use it there
#   - if no #chester channel exists at all, they get a private notice
#     telling an admin to create one


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


async def enforce_chester_channel(
    interaction: discord.Interaction,
    initialize_save: bool = True,
) -> bool:
    """
    For slash commands. Sends an ephemeral notice (private to the user,
    with Discord's built-in dismiss button) and returns False if this
    isn't the #chester channel.
    """
    if save_migration_active:
        message = "⚠️ Player saves are being updated. Please try again shortly."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    if initialize_save:
        try:
            save_store.ensure_player(interaction.user.id)
        except SaveSchemaMismatch:
            message = (
                "⚠️ Player saves need to be updated for the new progression files. "
                "A moderator must run `/update_saves`."
            )
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return False
        except Exception as e:
            log.exception("Failed to initialize player save for %s: %s", interaction.user.id, e)
            message = "⚠️ Your player save could not be initialized. Please contact an admin."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return False

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

# Loaded once at startup; use the reload command to refresh without restarting.
town_hall_loot_tables: Dict[int, Dict[str, Category]] = {}


@bot.event
async def on_ready():
    global town_hall_loot_tables, xp_levels

    try:
        town_hall_loot_tables = load_town_hall_loot_tables()
        log.info("Loaded loot tables for %d Town Hall levels.", len(town_hall_loot_tables))
    except Exception as e:
        log.exception("Failed to load loot tables: %s", e)

    try:
        save_store.load_schema()
        resource_system.load()
        upgrade_system.load()
        magic_system.load()
        xp_levels = load_xp_levels()
        log.info(
            "Loaded %d save fields across %d profile categories.",
            len(save_store.fields),
            len(save_store.categories),
        )
        log.info("Loaded %d XP level thresholds.", len(xp_levels))
    except Exception as e:
        log.exception("Failed to set up save system: %s", e)

    try:
        synced = await bot.tree.sync()
        log.info(
            "Synced %d global slash command(s) (can take up to 1 hour to "
            "appear in every server's client).",
            len(synced),
        )
    except Exception as e:
        log.exception("Failed to sync global slash commands: %s", e)

    if DISCORD_GUILD_ID:
        try:
            guild_obj = discord.Object(id=int(DISCORD_GUILD_ID))
            bot.tree.copy_global_to(guild=guild_obj)
            guild_synced = await bot.tree.sync(guild=guild_obj)
            log.info(
                "Synced %d slash command(s) instantly to guild %s.",
                len(guild_synced), DISCORD_GUILD_ID,
            )
        except ValueError:
            log.warning("DISCORD_GUILD_ID='%s' is not a valid integer guild ID.", DISCORD_GUILD_ID)
        except Exception as e:
            log.exception("Failed to sync commands to guild %s: %s", DISCORD_GUILD_ID, e)

    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id if bot.user else "?")


async def _do_loot_roll(user_id: int) -> Tuple[discord.Embed, Optional[int]]:
    """
    Perform a real /chest roll: pick loot, award XP for it, and return
    (embed, new_level_if_leveled_up_else_None).
    """
    if not town_hall_loot_tables:
        raise RuntimeError("Loot tables are not loaded. Try `/reload_loot` or restart the bot.")
    upgrade_system.refresh(user_id)
    village, _ = save_store.player_values(user_id)
    town_hall = village["Town Hall"]
    categories = town_hall_loot_tables.get(town_hall)
    if categories is None:
        raise LootRollRejected(
            f"No chest loot table is available for Town Hall level {town_hall}."
        )
    category, item = roll_loot(categories)
    resolved = resolve_item_display(item)

    xp_reward = XP_REWARDS.get(category.name.lower(), 0)
    old_xp = village["Experience"]
    village_additions = {
        "Experience": xp_reward,
        "Total Opened Chests": 1,
    }
    collection_unlocks: List[str] = []

    if resolved.collection_category:
        if save_store.has_collection_field(resolved.reward_name):
            collection_unlocks.append(resolved.reward_name)
        else:
            log.warning(
                "Chest reward '%s' is missing from collection.csv category '%s'.",
                resolved.reward_name,
                resolved.collection_category,
            )
    elif save_store.has_village_field(resolved.reward_name):
        village_additions[resolved.reward_name] = resolved.reward_amount
        total_name = f"Total {resolved.reward_name}"
        if save_store.has_village_field(total_name):
            village_additions[total_name] = resolved.reward_amount
    else:
        log.warning("Chest reward '%s' is missing from village.csv.", resolved.reward_name)

    resource_amounts = {
        resource: village_additions.pop(resource)
        for resource in RESOURCE_TYPES if resource in village_additions
    }
    magic_awards = []
    with save_store.transaction(user_id) as (values, collection):
        received = resource_system.deposit(values, resource_amounts)
        for name, amount in village_additions.items():
            field = save_store.village_by_name[name]
            if field.category == "Magic Item":
                stored, sold, gems = magic_system.award(values, name, amount)
                magic_awards.append((name, stored, sold, gems))
            else:
                values[name] = save_store._bounded_add(values[name], amount, field.data_type)
        for name in collection_unlocks:
            collection[name] = 1
        new_xp = values["Experience"]
    old_level = xp_to_level(old_xp)
    new_level = xp_to_level(new_xp)

    embed = await build_loot_embed(
        category,
        item,
        xp_reward=xp_reward,
        resolved=resolved,
    )
    if resource_amounts:
        embed.add_field(name="Stored resources", value=_format_resource_receipt(received), inline=False)
    for name, stored, sold, gems in magic_awards:
        description = f"Stored {stored:,} {name}."
        if sold:
            description += f" Automatically sold {sold:,} excess for {gems:,} Gems."
        embed.add_field(name="Magic item inventory", value=description, inline=False)
    leveled_up_to = new_level if new_level > old_level else None
    return embed, leveled_up_to


@bot.tree.command(name="chest", description="Open a chest and get a random item!")
@app_commands.checks.cooldown(1, 2.0)  # 1 use per 2 seconds, per user
async def chest_slash(interaction: discord.Interaction):
    if not await enforce_chester_channel(interaction):
        return
    try:
        village, _ = save_store.player_values(interaction.user.id)
        if village["Town Hall"] == 0:
            save_store.update_village_values(
                interaction.user.id, set_values={"Town Hall": 1}
            )
            await interaction.response.send_message(
                VILLAGE_WELCOME_MESSAGE, ephemeral=True
            )
        embed, leveled_up_to = await _do_loot_roll(interaction.user.id)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=False)
        else:
            await interaction.response.send_message(embed=embed)
        if leveled_up_to is not None:
            level_up_embed = discord.Embed(
                description=f"🎉 {interaction.user.mention} leveled up to **Level {leveled_up_to}**!",
                color=discord.Color.gold(),
            )
            await interaction.followup.send(embed=level_up_embed)
    except LootRollRejected as e:
        if interaction.response.is_done():
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
    except Exception as e:
        log.exception("Error rolling loot: %s", e)
        if interaction.response.is_done():
            await interaction.followup.send(f"⚠️ Something went wrong: {e}", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ Something went wrong: {e}", ephemeral=True)


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
    rarity_names = sorted(
        {
            name
            for categories in town_hall_loot_tables.values()
            for name in categories
        }
    )
    return [
        app_commands.Choice(name=name, value=name)
        for name in rarity_names
        if current_lower in name.lower()
    ][:25]


@bot.tree.command(name="test", description="[Admin] Force a roll from a specific rarity/category.")
@app_commands.describe(
    rarity="Which rarity/category to force a roll from",
    town_hall="Which Town Hall level's loot table to test",
)
@app_commands.autocomplete(rarity=rarity_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def test_slash(
    interaction: discord.Interaction,
    rarity: str,
    town_hall: app_commands.Range[int, 1, 18],
):
    if not await enforce_chester_channel(interaction):
        return
    categories = town_hall_loot_tables.get(town_hall)
    if categories is None:
        await interaction.response.send_message(
            "⚠️ Town Hall must be from 1 through 18 and its loot table must be loaded.",
            ephemeral=True,
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
        chosen_item = roll_item_from_category(chosen_cat)
        embed = await build_loot_embed(chosen_cat, chosen_item)
        await interaction.response.send_message(embed=embed)
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


async def upgrade_item_autocomplete(interaction: discord.Interaction, current: str):
    current_key = current.casefold()
    names = [field.name for field in upgrade_system.level_fields]
    names.extend(f"Builder #{number}" for number in range(1, 7))
    names.extend(f"Researcher #{number}" for number in range(1, 4))
    return [
        app_commands.Choice(name=name, value=name)
        for name in names
        if current_key in name.casefold()
    ][:25]


def _format_upgrade_costs(costs: Dict[str, int]) -> str:
    if not costs:
        return "Free"
    return "\n".join(
        f"{resource}: **{amount:,}**" for resource, amount in costs.items()
    )


def _build_upgrade_embed(outcome: UpgradeOutcome, refreshed: RefreshReport) -> discord.Embed:
    title = "Upgrade complete" if outcome.instant else "Upgrade started"
    embed = discord.Embed(
        title=title,
        description=(
            f"**{outcome.item}**\n"
            f"Level {outcome.previous_level:,} → {outcome.target_level:,}"
        ),
        color=discord.Color.green(),
    )
    embed.add_field(
        name="Cost",
        value=_format_upgrade_costs(outcome.costs),
        inline=False,
    )
    if not outcome.instant and outcome.slot and outcome.finish_time:
        embed.add_field(
            name=outcome.slot.removesuffix(" Upgrade"),
            value=(
                f"Finishes <t:{outcome.finish_time}:F> "
                f"(<t:{outcome.finish_time}:R>)"
            ),
            inline=False,
        )
    if refreshed.completed:
        finished = "\n".join(
            f"{entry.item} reached level {entry.new_level:,}"
            for entry in refreshed.completed
        )
        embed.add_field(name="Also completed", value=finished, inline=False)
    if refreshed.warnings:
        embed.add_field(
            name="Save warnings",
            value="\n".join(refreshed.warnings),
            inline=False,
        )
    embed.set_footer(text="Made by __godly__")
    return embed


class UpgradeCurrencyView(discord.ui.View):
    def __init__(self, user_id: int, choice: UpgradeCurrencyChoiceRequired):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.choice = choice
        self.used = False
        self.message: Optional[discord.InteractionMessage] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the player who requested this upgrade can choose its currency.",
                ephemeral=True,
            )
            return False
        if self.used or self.is_finished():
            await interaction.response.send_message(
                "This currency choice is no longer active. Use /upgrade again.",
                ephemeral=True,
            )
            return False
        return True

    async def _choose(self, interaction: discord.Interaction, currency: str):
        if not await self.interaction_check(interaction):
            return
        self.used = True
        self.stop()
        if not await enforce_chester_channel(interaction):
            if self.message is not None:
                await self.message.edit(view=None)
            return
        try:
            outcome, refreshed = upgrade_system.start_upgrade(
                self.user_id,
                self.choice.item,
                currency=currency,
                expected_level=self.choice.current_level,
                expected_price=self.choice.price,
            )
        except WorkerUnavailable as error:
            await interaction.response.edit_message(content=str(error), embed=None, view=None)
            return
        except UpgradeRejected as error:
            await interaction.response.edit_message(
                content=f"⚠️ Upgrade rejected: {error}", embed=None, view=None
            )
            return
        except Exception as error:
            log.exception("Unexpected error choosing upgrade currency: %s", error)
            await interaction.response.edit_message(
                content=f"⚠️ The upgrade could not be started: {error}",
                embed=None,
                view=None,
            )
            return
        combined = RefreshReport(
            completed=self.choice.refreshed.completed + refreshed.completed,
            active=refreshed.active,
            warnings=list(dict.fromkeys(self.choice.refreshed.warnings + refreshed.warnings)),
        )
        await interaction.response.edit_message(
            content=None, embed=_build_upgrade_embed(outcome, combined), view=None
        )

    @discord.ui.button(label="Use Gold", style=discord.ButtonStyle.primary)
    async def use_gold(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "Gold")

    @discord.ui.button(label="Use Elixir", style=discord.ButtonStyle.primary)
    async def use_elixir(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "Elixir")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.interaction_check(interaction):
            return
        self.used = True
        self.stop()
        await interaction.response.edit_message(
            content="Upgrade cancelled. No upgrade resources were spent.",
            embed=None,
            view=None,
        )

    async def on_timeout(self):
        if self.used:
            return
        self.used = True
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="This currency choice expired. Use /upgrade again.",
                    embed=None,
                    view=None,
                )
            except discord.HTTPException:
                log.debug("Could not clear an expired upgrade currency prompt")


@bot.tree.command(name="upgrade", description="Upgrade an unlocked village item.")
@app_commands.describe(item="An upgradeable item or a Builder number to unlock")
@app_commands.autocomplete(item=upgrade_item_autocomplete)
async def upgrade_slash(interaction: discord.Interaction, item: str):
    if not await enforce_chester_channel(interaction):
        return
    try:
        outcome, refreshed = upgrade_system.start_upgrade(
            interaction.user.id,
            item,
        )
    except UpgradeCurrencyChoiceRequired as choice:
        view = UpgradeCurrencyView(interaction.user.id, choice)
        embed = discord.Embed(
            title="Choose upgrade currency",
            description=(
                f"**{choice.item}**\n"
                f"Level {choice.current_level:,} → {choice.current_level + 1:,}\n\n"
                f"Pay **{choice.price.choice_cost:,} Gold** or "
                f"**{choice.price.choice_cost:,} Elixir**.\n"
                "Choose one below. This choice expires in two minutes."
            ),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()
        return
    except WorkerUnavailable as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    except UpgradeRejected as error:
        await interaction.response.send_message(
            f"⚠️ Upgrade rejected: {error}", ephemeral=True
        )
        return
    except Exception as error:
        log.exception("Unexpected error in /upgrade: %s", error)
        await interaction.response.send_message(
            f"⚠️ The upgrade could not be started: {error}", ephemeral=True
        )
        return
    embed = _build_upgrade_embed(outcome, refreshed)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="refresh_upgrades",
    description="Finish completed upgrades and show upgrades still in progress.",
)
async def refresh_upgrades_slash(interaction: discord.Interaction):
    if not await enforce_chester_channel(interaction):
        return
    try:
        report = upgrade_system.refresh(interaction.user.id)
    except Exception as e:
        log.exception("Unexpected error in /refresh_upgrades: %s", e)
        await interaction.response.send_message(
            f"⚠️ Upgrades could not be refreshed: {e}",
            ephemeral=True,
        )
        return

    embed = discord.Embed(title="Upgrade status", color=discord.Color.blue())
    if report.completed:
        embed.add_field(
            name="Completed",
            value="\n".join(
                f"**{entry.item}:** level {entry.new_level:,}"
                for entry in report.completed
            ),
            inline=False,
        )
    if report.active:
        embed.add_field(
            name="In progress",
            value="\n".join(
                f"**{entry.item}:** <t:{entry.finish_time}:R> "
                f"({entry.slot.removesuffix(' Upgrade')})"
                for entry in report.active
            ),
            inline=False,
        )
    if report.warnings:
        embed.add_field(
            name="Save warnings",
            value="\n".join(report.warnings),
            inline=False,
        )
    if not report.completed and not report.active and not report.warnings:
        embed.description = "No upgrades are currently in progress."
    embed.set_footer(text="Made by __godly__")
    await interaction.response.send_message(embed=embed)


def _magic_result_embed(result):
    return discord.Embed(
        title=result.item,
        description="\n".join(result.lines)[:4000],
        color=discord.Color.green(),
    )


async def magic_item_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=name, value=name)
        for name in magic_system.items if current.casefold() in name.casefold()
    ][:25]


class MagicItemView(discord.ui.View):
    def __init__(self, user_id, item, options):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.item = item
        self.options = {option.value: option for option in options}
        self.used = False
        self.message = None
        self.selection = discord.ui.Select(
            placeholder="Choose a Wall level" if item.target == "Wall" else "Choose a worker",
            options=[discord.SelectOption(label=option.label[:100], value=option.value, description=option.description[:100]) for option in options],
        )
        self.selection.callback = self.choose
        self.add_item(self.selection)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the player who opened this dialog can use it.", ephemeral=True)
            return False
        if self.used or self.is_finished():
            await interaction.response.send_message("This selection has expired. Run /use again.", ephemeral=True)
            return False
        return True

    async def choose(self, interaction):
        if not await self.interaction_check(interaction):
            return
        self.used = True
        self.stop()
        if not await enforce_chester_channel(interaction):
            if self.message is not None:
                await self.message.edit(view=None)
            return
        try:
            option = self.options.get(self.selection.values[0])
            if option is None:
                raise MagicRejected("Invalid selection. Run /use again")
            result = magic_system.use(self.user_id, self.item.name, option=option, expected_item=self.item)
        except (MagicRejected, UpgradeRejected, ResourceRejected) as error:
            await interaction.response.edit_message(content=str(error), embed=None, view=None)
            return
        except Exception as error:
            log.exception("Magic item selection failed: %s", error)
            await interaction.response.edit_message(content="The magic item could not be used.", embed=None, view=None)
            return
        await interaction.response.edit_message(content=None, embed=_magic_result_embed(result), view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.interaction_check(interaction):
            return
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Cancelled. No magic item was used.", embed=None, view=None)

    async def on_timeout(self):
        if self.used:
            return
        self.used = True
        if self.message is not None:
            try:
                await self.message.edit(content="Selection expired. No magic item was used.", embed=None, view=None)
            except discord.HTTPException:
                log.debug("Could not clear an expired magic item dialog")


@bot.tree.command(name="use", description="Use a magic item from your inventory.")
@app_commands.describe(item="The type of magic item to use")
@app_commands.autocomplete(item=magic_item_autocomplete)
async def use_slash(interaction: discord.Interaction, item: str):
    if not await enforce_chester_channel(interaction):
        return
    try:
        definition, options, status = magic_system.prepare(interaction.user.id, item)
        if options:
            view = MagicItemView(interaction.user.id, definition, options)
            embed = discord.Embed(
                title=f"Use {definition.name}", description="\n".join(status), color=discord.Color.blue()
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            view.message = await interaction.original_response()
            return
        result = magic_system.use(interaction.user.id, item)
    except (MagicRejected, UpgradeRejected, ResourceRejected) as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    except Exception as error:
        log.exception("Unexpected error in /use: %s", error)
        await interaction.response.send_message("The magic item could not be used.", ephemeral=True)
        return
    await interaction.response.send_message(embed=_magic_result_embed(result), ephemeral=True)


@bot.tree.command(name="sell", description="Sell one magic item from your inventory for Gems.")
@app_commands.describe(item="The type of magic item to sell")
@app_commands.autocomplete(item=magic_item_autocomplete)
async def sell_slash(interaction: discord.Interaction, item: str):
    if not await enforce_chester_channel(interaction):
        return
    try:
        result = magic_system.sell(interaction.user.id, item)
    except MagicRejected as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    except Exception as error:
        log.exception("Unexpected error in /sell: %s", error)
        await interaction.response.send_message("The magic item could not be sold.", ephemeral=True)
        return
    await interaction.response.send_message(embed=_magic_result_embed(result), ephemeral=True)


def _format_resource_receipt(receipt: ResourceReceipt) -> str:
    if not receipt.main:
        return "No resources were ready to collect."
    return "\n".join(
        f"**{resource}:** {amount:,} to storages, {receipt.treasury.get(resource, 0):,} to treasury"
        for resource, amount in receipt.main.items()
    )


async def cancel_upgrade_autocomplete(interaction: discord.Interaction, current: str):
    if save_migration_active:
        return []
    try:
        village, _ = save_store.player_values(interaction.user.id)
    except Exception:
        return []
    choices = []
    for slot, _, serial, finish in upgrade_system._slot_values(village):
        item = upgrade_system.name_by_serial.get(serial)
        if finish <= int(time.time()) or item is None:
            continue
        label = f"{slot.removesuffix(' Upgrade')}: {item}"
        if current.casefold() in label.casefold():
            choices.append(app_commands.Choice(name=label, value=slot))
    return choices[:25]


@bot.tree.command(name="cancel_upgrade", description="Cancel an active upgrade and refund half its cost.")
@app_commands.describe(item="An upgrading item or its Builder or Researcher slot")
@app_commands.autocomplete(item=cancel_upgrade_autocomplete)
async def cancel_upgrade_slash(interaction: discord.Interaction, item: str):
    if not await enforce_chester_channel(interaction):
        return
    try:
        outcome = upgrade_system.cancel_upgrade(interaction.user.id, item)
    except (UpgradeRejected, ResourceRejected) as error:
        await interaction.response.send_message(f"Cancellation rejected: {error}", ephemeral=True)
        return
    except Exception as error:
        log.exception("Unexpected error in /cancel_upgrade: %s", error)
        await interaction.response.send_message("The upgrade could not be cancelled.", ephemeral=True)
        return
    embed = discord.Embed(
        title="Upgrade cancelled",
        description=f"**{outcome.item}**\n{outcome.slot.removesuffix(' Upgrade')} is now free.",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Half cost refund", value=_format_upgrade_costs(outcome.refunds), inline=False)
    embed.add_field(name="Received after storage limits", value=_format_resource_receipt(outcome.received), inline=False)
    if outcome.warnings:
        embed.add_field(name="Refund information", value="\n".join(outcome.warnings), inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="collect_loot", description="Collect resources generated by your Mines, Collectors, and Drills.")
async def collect_loot_slash(interaction: discord.Interaction):
    if not await enforce_chester_channel(interaction):
        return
    try:
        now = int(time.time())
        upgrade_system.refresh(interaction.user.id, now)
        with save_store.transaction(interaction.user.id) as (village, collection):
            received = resource_system.collect(village, now)
    except ResourceRejected as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    except Exception as error:
        log.exception("Unexpected error in /collect_loot: %s", error)
        await interaction.response.send_message("Your resources could not be collected.", ephemeral=True)
        return
    embed = discord.Embed(title="Resources collected", description=_format_resource_receipt(received), color=discord.Color.green())
    embed.set_footer(text="Overflow fills the treasury at five percent efficiency")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="collect_treasury", description="Move treasury resources into available main storage space.")
async def collect_treasury_slash(interaction: discord.Interaction):
    if not await enforce_chester_channel(interaction):
        return
    try:
        upgrade_system.refresh(interaction.user.id)
        with save_store.transaction(interaction.user.id) as (village, collection):
            collected = resource_system.collect_treasury(village)
    except Exception as error:
        log.exception("Unexpected error in /collect_treasury: %s", error)
        await interaction.response.send_message("Your treasury could not be collected.", ephemeral=True)
        return
    description = "\n".join(f"**{resource}:** {amount:,}" for resource, amount in collected.items())
    embed = discord.Embed(title="Treasury collected", description=description, color=discord.Color.green())
    embed.set_footer(text="Resources that did not fit remain in your treasury")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="view_collectors", description="View producer rates, stored loot, and capacities.")
async def view_collectors_slash(interaction: discord.Interaction):
    if not await enforce_chester_channel(interaction):
        return
    try:
        now = int(time.time())
        upgrade_system.refresh(interaction.user.id, now)
        village, _ = save_store.player_values(interaction.user.id)
        statuses = resource_system.collector_status(village, now)
    except Exception as error:
        log.exception("Unexpected error in /view_collectors: %s", error)
        await interaction.response.send_message("Your collectors could not be displayed.", ephemeral=True)
        return
    embed = discord.Embed(title="Resource collectors", color=discord.Color.blue())
    if not statuses:
        embed.description = "Build a Gold Mine, Elixir Collector, or Dark Elixir Drill to begin producing resources."
    for status in statuses:
        percent = 100 * status.stored / status.capacity if status.capacity else 0
        embed.add_field(
            name=f"{status.item} | Level {status.level}",
            value=(f"{status.hourly_rate:,} {status.resource} per hour\n"
                   f"{status.stored:,} / {status.capacity:,} stored ({percent:.1f}%)"),
            inline=False,
        )
    embed.set_footer(text="Use collect_loot to move these resources into storage")
    await interaction.response.send_message(embed=embed, ephemeral=True)


def _migrate_save_files() -> MigrationReport:
    old_village = PROGRESSION_DIR / "village-old.csv"
    old_collection = PROGRESSION_DIR / "collection-old.csv"
    if not old_village.is_file() and not old_collection.is_file():
        raise FileNotFoundError("Provide village-old.csv or collection-old.csv for each changed schema")
    if not old_village.is_file():
        old_village = PROGRESSION_DIR / "village.csv"
    if not old_collection.is_file():
        old_collection = PROGRESSION_DIR / "collection.csv"

    old_store = SaveStore(PROGRESSION_DIR, SAVES_DIR)
    old_store.load_schema(
        village_path=old_village,
        collection_path=old_collection,
    )
    new_store = SaveStore(PROGRESSION_DIR, SAVES_DIR)
    new_store.load_schema()
    new_resource_system = ResourceSystem(PROGRESSION_DIR, new_store, CLAN_CASTLE_RESOURCE_RATIO)
    new_resource_system.load()
    new_upgrade_system = UpgradeSystem(
        PROGRESSION_DIR,
        new_store,
        COMMON_EQUIPMENT_MAX,
        EPIC_EQUIPMENT_MAX,
        new_resource_system,
    )
    new_upgrade_system.load()
    MagicSystem(PROGRESSION_DIR, new_store, new_upgrade_system, new_resource_system).load()
    return new_store.migrate_from(old_store, SAVE_BACKUPS_DIR)


@bot.tree.command(
    name="update_saves",
    description="[Moderator] Migrate player saves after progression CSV changes.",
)
@app_commands.checks.has_permissions(moderate_members=True)
async def update_saves_slash(interaction: discord.Interaction):
    global save_migration_active

    if not await enforce_chester_channel(interaction, initialize_save=False):
        return

    save_migration_active = True
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        report = await asyncio.to_thread(_migrate_save_files)
        save_store.load_schema()
        resource_system.load()
        upgrade_system.load()
        magic_system.load()
        save_store.ensure_player(interaction.user.id)
        await interaction.followup.send(
            "✅ Save migration completed.\n"
            f"Players checked: **{report.total_saves:,}**\n"
            f"Saves migrated: **{report.migrated_saves:,}**\n"
            f"Already current: **{report.current_saves:,}**\n"
            f"Fields added: **{report.added_fields:,}**\n"
            f"Fields removed: **{report.removed_fields:,}**\n"
            f"Fields moved: **{report.moved_fields:,}**\n"
            f"Types changed: **{report.changed_types:,}**\n"
            f"Categories changed: **{report.changed_categories:,}**\n"
            f"Backup: `{report.backup_path.name}`",
            ephemeral=True,
        )
    except Exception as e:
        log.exception("Save migration failed: %s", e)
        await interaction.followup.send(
            f"⚠️ Save migration failed. Existing data was preserved or backed up: {e}",
            ephemeral=True,
        )
    finally:
        save_migration_active = False


@update_saves_slash.error
async def update_saves_slash_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, app_commands.MissingPermissions):
        message = "⚠️ You need Moderator permissions to use `/update_saves`."
    else:
        log.exception("Unexpected error in /update_saves: %s", error)
        message = f"⚠️ Something went wrong: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="reload_loot", description="Reload the loot tables from CSV without restarting the bot.")
async def reload_loot(interaction: discord.Interaction):
    if not await enforce_chester_channel(interaction):
        return
    global town_hall_loot_tables, xp_levels
    try:
        town_hall_loot_tables = load_town_hall_loot_tables()
        xp_levels = load_xp_levels()
        await interaction.response.send_message(
            f"✅ Reloaded loot for {len(town_hall_loot_tables)} Town Hall levels "
            f"and {len(xp_levels)} XP level thresholds.",
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
            "/test: Admin only. Tests a rarity from a selected Town Hall loot table.\n"
            "/upgrade: Starts or instantly applies an eligible item upgrade.\n"
            "/use: Uses a magic item and prompts for a target when required.\n"
            "/sell: Sells one magic item for Gems.\n"
            "/refresh_upgrades: Applies finished upgrades and shows active upgrade slots.\n"
            "/cancel_upgrade: Cancels an active item or slot and refunds half its cost.\n"
            "/collect_loot: Collects resources produced by Mines, Collectors, and Drills.\n"
            "/collect_treasury: Moves treasury loot into available main storage space.\n"
            "/view_collectors: Shows producer rates, fill progress, and capacities.\n"
            "/reload_loot: Admin only. Reloads the loot table, needed when the rewards "
            "change or mistakes are found in the files.\n"
            "/update_saves: Moderator only. Migrates saves after progression CSV changes.\n"
            "/about: Shows bot version, version release date, and bot author information.\n"
            "/support: Gives a donation link which helps support the bot.\n"
            "/profile: Shows every saved value in a selected category. Moderators and "
            "above can check anyone's profile by providing them as an argument."
        ),
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(embed=embed)


async def profile_category_autocomplete(interaction: discord.Interaction, current: str):
    current_key = current.casefold()
    return [
        app_commands.Choice(name=category, value=category)
        for category in save_store.categories
        if current_key in category.casefold()
    ][:25]


def _profile_pages(values: List[Tuple[str, int]], maximum_length: int = 3800) -> List[str]:
    pages: List[str] = []
    lines: List[str] = []
    length = 0
    for name, value in values:
        line = f"**{name}:** {value:,}"
        added = len(line) + (1 if lines else 0)
        if lines and length + added > maximum_length:
            pages.append("\n".join(lines))
            lines = []
            length = 0
            added = len(line)
        lines.append(line)
        length += added
    if lines:
        pages.append("\n".join(lines))
    return pages


@bot.tree.command(name="profile", description="Show a player's saved values by category.")
@app_commands.describe(
    category="The save category to display.",
    member="View someone else's profile instead of your own (Moderator permission or higher required)."
)
@app_commands.autocomplete(category=profile_category_autocomplete)
async def profile_slash(
    interaction: discord.Interaction,
    category: str,
    member: Optional[discord.Member] = None,
):
    if not await enforce_chester_channel(interaction):
        return

    target = member or interaction.user

    if member is not None and member.id != interaction.user.id:
        if not interaction.permissions.moderate_members:
            await interaction.response.send_message(
                "⚠️ You need Moderator permissions (Moderate Members) or higher to "
                "view someone else's profile.",
                ephemeral=True,
            )
            return

    try:
        resolved_category = save_store.resolve_category(category)
        values = save_store.category_values(target.id, resolved_category)
        if resolved_category.casefold() == "currency":
            village, _ = save_store.player_values(target.id)
            listed_names = {name for name, _ in values}
            values.extend(
                (name, village[name])
                for name in TREASURY_FIELDS.values()
                if name in village and name not in listed_names
            )
    except KeyError:
        await interaction.response.send_message(
            f"⚠️ Unknown category '{category}'. Valid categories: "
            f"{', '.join(save_store.categories)}",
            ephemeral=True,
        )
        return

    pages = _profile_pages(values)
    for page_number, page in enumerate(pages, start=1):
        page_label = f" ({page_number}/{len(pages)})" if len(pages) > 1 else ""
        embed = discord.Embed(
            title=f"{target.display_name} — {resolved_category}{page_label}",
            description=page,
            color=discord.Color.blue(),
        )
        if page_number == 1:
            embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="Made by __godly__")
        if page_number == 1:
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.followup.send(embed=embed)


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
