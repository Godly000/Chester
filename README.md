# Chest Discord Bot

Rolls a weighted-random "chest": first picks a **category** (rarity),
then picks an **item** within that category, and posts an embed with the
category, item name, and image.

## How it reads your data

* `data/rarities.csv` — one row per category: `category\_name,weight`
* `data/<category\_name>.csv` — one row per item in that category:
`item\_name,image\_url,weight`

Weights don't need to sum to 1 — they're just relative. For example,
your current `rarities.csv`:

```
common,.58
rare,.32
epic,.08
legendary,.02
```

means \~58% common, \~32% rare, \~8% epic, \~2% legendary. Within each
category, items are chosen the same way using that file's weights.

To add a new rarity tier, add a row to `rarities.csv` and create a
matching `<name>.csv` with its items.

## Setup

1. Install dependencies:

```
   pip install -r requirements.txt
   ```

2. Create a Discord application + bot at https://discord.com/developers/applications,
copy its token.
3. Copy `.env.example` to `.env` and paste in the token:

```
   cp .env.example .env
   ```

4. Invite the bot to your server using the OAuth2 URL generator with the
`bot` and `applications.commands` scopes, and at minimum the
**Send Messages** and **Embed Links** permissions.
5. Run the bot:

```
   python bot.py
   ```

## Usage

* `/chest` — slash command, opens a chest
* `!chest` — same thing as a classic prefix command
* `/reload\_loot` — re-reads the CSV files without restarting the bot
(handy after you edit weights or items)

## Notes

* CSV files are read with no header row — the first row is treated as data.
* If a category in `rarities.csv` has no matching `<name>.csv`, or that file
has no valid rows, the category is skipped (logged as a warning) rather
than crashing the bot.
* Image URLs are used directly in the embed via `set\_image`, so they must
be direct links to an image (ending in .png/.jpg/etc., or otherwise
served as an image).

