# Chester Discord Bot

Stores each player's village, runs timed upgrades, and opens weighted-random
chests from the loot table for that player's Town Hall level.

## How it reads your data

* `data/rarities.csv` — one row per category: `category\_name,weight`
* `Town Hall Loot Tables/Town Hall <level>/<rarity>.csv` — one row per reward:
`item\_name,weight[,quantity\_range\_or\_subtable]`
* `Town Hall Loot Tables/images.csv` and the shared subtable CSVs — reward
images and collection rolls
* `data/progression/village.csv` — ordered numeric save fields
* `data/progression/collection.csv` — ordered collection save fields
* `data/progression/counts.csv` — unlock and building-count requirements
* `data/progression/levels.csv` — prerequisite-based maximum levels
* `data/progression/upgrades.csv` — standard costs and durations
* `data/progression/equipmentcost.csv` — equipment ore costs
* `data/progression/upgrade_ids.csv` — stable numeric IDs stored in upgrade slots

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

To add a new rarity tier, add a row to `rarities.csv` and create a matching
`<name>.csv` inside every Town Hall folder that should offer it.

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
   python main.py
```

## Usage

* `/chest` — opens a chest using the player's current Town Hall loot table
* `/upgrade item:<name>` — validates and starts or applies an upgrade
* `/refresh\_upgrades` — completes finished upgrades and lists active slots
* `/test rarity:<rarity> town\_hall:<level>` — admin-only loot-table test
* `/reload\_loot` — re-reads the CSV files without restarting the bot
(handy after you edit weights or items)
* `/profile category:<category>` — shows every saved value in that category
* `/update\_saves` — moderator-only schema migration

## Player saves

The first command a player uses creates `saves/<discord_user_id>.sav`. Each
save is a compact binary record whose field order comes from `village.csv`
followed by `collection.csv`. Values use the declared signed `byte`, `short`,
`integer`, `long`, or `boolean` format. Reward additions stop at that format's
maximum value instead of overflowing. Each save also contains a schema
fingerprint so the bot refuses to interpret values using the wrong CSV order.
Active upgrades store a stable ID from `upgrade_ids.csv` and a UNIX finish
timestamp. The ID file is append-only, so reordering `village.csv` does not
change the meaning of an occupied Builder or Researcher slot.

Opening a chest updates the rolled resource or item, matching lifetime total
when one exists, Experience, Total Opened Chests, and collection unlocks. The
profile command reads the field names and categories from the progression CSVs,
so the binary save does not need to duplicate them.

## Upgrades

Every `village.csv` field whose category contains `Level` is available to
`/upgrade`. Level zero means locked. The command checks `counts.csv` for unlock
and instance-count limits, `levels.csv` for the current prerequisite-based cap,
then `upgrades.csv` for cost and duration. Missing level-one statistics produce
a free, instant unlock; missing statistics for later levels reject the upgrade.

Structure and Hero upgrades occupy Builder slots. Troop, Spell, Siege, and Pet
upgrades occupy Researcher slots. Equipment is instant: common equipment uses
Shiny and Glowy Ore through level 18, while epic equipment can also use Starry
Ore through level 27. All save writes clamp to the declared numeric data type.

Town Hall level one is a free initial unlock. Later Town Hall upgrades require
every other currently possible upgrade to be complete and no occupied upgrade
slots. Completing Town Hall 12, 13, 14, 15, or 17 also unlocks the corresponding
Town Hall weapon at level one.

## Updating the save schema

Before changing either progression file, retain copies of the previous versions
as `data/progression/village-old.csv` and
`data/progression/collection-old.csv`. Put the changed data in `village.csv`
and `collection.csv`, then run `/update_saves` in the Chester channel as a
moderator.

The command matches fields by their source file and exact name, preserves their
values when rows move, initializes added fields to zero, removes deleted fields,
preserves values across category changes, and clamps values when a numeric type
becomes smaller. A renamed field is treated as one removed field and one new
field because a rename cannot be inferred safely.

Every migration is built and validated in a staging folder before activation.
The previous complete `saves` folder is retained under `save-backups`, and
schema fingerprints make the command safe to run again without moving values a
second time. Other player commands pause while migration is running.

Keep `upgrade_ids.csv` when replacing progression CSVs. `/update_saves` extends
it for newly added level fields without renumbering existing or removed items,
which keeps active upgrade slots valid through schema changes.

## Notes

* Loot-table CSVs are read with no header row. Progression CSVs use their shown
headers.
* Every Town Hall from 1 through 18 must have all rarity files named by
`rarities.csv`.
* Image URLs are used directly in the embed via `set\_image`, so they must
be direct links to an image (ending in .png/.jpg/etc., or otherwise
served as an image).