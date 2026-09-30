# Chester Discord Bot

Chester opens weighted random chests, stores player villages, runs timed upgrades,
and manages resources, equipment, magic items, obstacles, and collections.
Progression and rewards are configured through CSV files.

## Setup

1. Install the dependencies with `python -m pip install -r requirements.txt`.
2. Create a Discord application and bot in the Discord Developer Portal.
3. Copy `.env.example` to `.env` once, then set `DISCORD\\\_TOKEN` to your bot token.
4. Invite the bot with the `bot` and `applications.commands` scopes and permissions
to view channels, send messages, and embed links.
5. Run `python main.py`.

Keep `.env` private and untracked. Do not overwrite an existing host configuration
with the example file during deployment. Preserve `saves/`, `save-backups/`,
`notification\\\_state.json`, and `gembox\\\_log.csv` when updating the bot.

`DISCORD\\\_GUILD\\\_ID` optionally selects a server for command synchronization.
`TOWN\\\_HALL\\\_LOOT\\\_DIR` optionally overrides the Town Hall loot folder; relative
paths are resolved from the directory containing `main.py`.

## Channels and permissions

Most commands work in channels whose names contain `chester`. `/chest`, `/test`,
and `/fight` normally require a channel named exactly `chester`.

Additional channels can be configured in `whitelist.txt`, alongside `main.py`.
Use one comma separated server and channel pair per line, without a header:

```text
1397316370487578674,1528080502282518549
```

A matching server and channel pair allows all Chester commands regardless of the
channel name. The file is read when checking channel access, so edits do not
require a restart. Moderator and owner permissions still apply. Wrong-channel
messages do not disclose this configuration.

Moderator commands require Discord's **Moderate Members** permission.

## Commands

Square brackets below denote optional arguments.

|Command|Purpose|
|-|-|
|`/chest`|Open a chest from your Town Hall's loot table; create your village on first use.|
|`/profile category \\\[member]`|View saved values, grouped walls, and applicable resource capacities.|
|`/upgrade category item \\\[level] \\\[quantity]`|Choose a target level and batch size, then confirm a payment method.|
|`/upgradeinfo category item level`|Show the cost, duration, and image for the specified target level.|
|`/refresh\\\_upgrades`|Complete finished upgrades and display active Builder and Researcher slots.|
|`/cancel\\\_upgrade item`|Cancel an active upgrade and refund half its resource cost within storage limits.|
|`/remaining \\\[sorted]`|Page through upgrades required before the next Town Hall, with per-upgrade costs and times.|
|`/view\\\_collectors`|Show production rates, pending amounts, and capacities, including Blacksmith ores.|
|`/collect\\\_loot`|Collect resources and ores generated since the last resource check.|
|`/collect\\\_treasury`|Transfer treasury resources into main storage at a one-to-one ratio.|
|`/use item`|Use an owned magic item; Wall Rings open the wall upgrade flow.|
|`/sell \\\[item] \\\[quantity]`|Sell owned magic items, or view counts, sale values, and your Gems without arguments.|
|`/buy category item`|Buy an eligible missing collectible with Gems.|
|`/check\\\_obstacles`|Generate obstacles based on time since the last obstacle check.|
|`/remove\\\_obstacle obstacle amount`|Pay to remove owned obstacles and receive their Gem rewards.|
|`/loot\\\_table town\\\_hall \\\[rarity]`|View reward chances for Town Hall levels one through eighteen, optionally filtered by rarity.|
|`/leaderboard category`|Show the top ten players and your placement for a statistic or Progress.|
|`/option name enabled`|Toggle upgrade notifications, full collector notifications, or Hide tutorial.|
|`/help \\\[mod-only]`|Privately show command help; moderators can select Yes to include moderator commands.|
|`/about`|Show information about Chester.|
|`/support`|Show support information.|

Moderator commands:

|Command|Purpose|
|-|-|
|`/test rarity town\\\_hall`|Test a specified rarity and Town Hall loot table.|
|`/fight \\\[image]`|Practice the Gem Box encounter, optionally selecting image one through four, without rewards or failure logs.|
|`/reload\\\_loot`|Reload loot tables without restarting the bot.|
|`/update\\\_saves`|Migrate saves after village or collection schema changes.|

`/set user\\\_id category name value` is restricted to Discord user
`459126084428890113` and is omitted from `/help`.

## Player saves and profiles

Only `/chest` creates a new player save. First use creates
`saves/<discord\\\_user\\\_id>.sav`, initializes Town Hall level one, and sends:

> Welcome, Chief! Your village has been created, have fun opening chests!

Other commands do not create a village and direct new players to `/chest`.

Saves are compact binary records ordered by `village.csv`, then `collection.csv`.
Fields use their declared `boolean`, signed `byte`, `short`, `integer`, or `long`
format. A schema fingerprint prevents loading a save against the wrong layout.
Upgrade slots store stable numeric item IDs and UNIX finish timestamps.

Profiles hide zero entries except those in the Important category, which appear
at the top of every section. Walls of the same level are grouped. Currency views
include defined storage capacities and treasury balances. Profile thumbnails use
the player's Town Hall image.

## Upgrades

Level zero means an item is locked. `counts.csv` controls prerequisite building
levels and allowed instance counts; `levels.csv` controls attainable target
levels. `upgrades.csv` supplies ordinary upgrade costs, durations in seconds,
and images. Missing level-one upgrade statistics allow a free instant unlock
once the requirements are met. Missing later-level statistics reject an upgrade.

The command groups numbered instances of the same type. Its optional `level`
argument selects the resulting level, and `quantity` selects how many instances
at the preceding level to upgrade. Defaults are the lowest available target and
one instance. The confirmation shows duration, costs, and the target image, then
lets the player choose eligible resources or magic items. `Both` costs allow
Gold or Elixir; `DE` means Dark Elixir.

Suggestions ignore worker availability while still checking other requirements.
Starting an upgrade requires an available worker unless the selected payment or
upgrade makes it instantaneous. An item already upgrading cannot start again.
Upgrades sharing an image are combined in the result, with the assigned workers
listed.

Structure and Hero upgrades use Builders. Troop, Spell, Siege, and Pet upgrades
use Researchers. Builders two through five cost 250, 500, 1000, and 2000 Gems.
The first Researcher unlocks at Laboratory level one and the second at Pet House
level two. The sixth Builder and third Researcher are unavailable.

Equipment upgrades are instantaneous and use ore costs from `equipmentcost.csv`.
Common equipment has a maximum level of eighteen and uses Shiny and Glowy Ore;
epic equipment has a maximum level of twenty-seven and can also require Starry
Ore. Epic equipment must be owned before upgrading.

Later Town Hall upgrades require completion of the other required upgrades.
Unlocking every Builder is not required. `/remaining` lists each target level
separately, combines matching instances with an `xN` multiplier, and shows costs
and times per upgrade. Sorting options are Cost Ascending, Cost Descending,
Time Ascending, and Time Descending. Progress leaderboards sort by Town Hall
level, then total upgrades completed.

## Resources and ore production

`data/progression/resource.csv` defines each building level's resource,
production per 3600 seconds, and capacity. Decimal production rates are supported.
Gold Mines, Elixir Collectors, Dark Elixir Drills, and the Blacksmith are producers.
The Blacksmith generates Shiny, Glowy, and Starry Ore and supplies their capacities.

`/collect\\\_loot` calculates elapsed time from `Last Resource Check`, limits each
producer's pending yield to its capacity, deposits the results, and updates the
check timestamp. Gold, Elixir, and Dark Elixir fill Town Hall and Storage capacity
at one-to-one efficiency. Excess can enter the Clan Castle treasury at five
percent efficiency. Ores never enter the treasury.

For a fractional calculated amount, collection keeps the whole part and rounds
up when a random value is greater than or equal to the fractional remainder;
otherwise it rounds down. Exact integers stay unchanged. This is intentionally
the reverse of unbiased stochastic rounding: a remainder of 0.1 rounds up with
ninety percent probability. The same rule applies to fractional treasury
conversion during collection. Status displays do not perform random rounding.

Manual collection is blocked when any applicable resource has both its main
storage and treasury full. For ores, a full main storage is sufficient because
there is no ore treasury. `/collect\\\_treasury` transfers at one-to-one efficiency
and leaves amounts that do not fit in the treasury.

Starting, completing, or cancelling any producer upgrade automatically collects
all producers and updates `Last Resource Check`. This includes the Blacksmith.
Automatic collection proceeds even when storage is full, retaining only what
fits. Completed producer upgrades collect at the old level before applying the
new production rate. Lifetime collection totals respect save data limits.

## Chests, magic items, and collections

Rarity and reward weights are relative and do not need to sum to one. The supplied
rarity weights are 58 percent common, 32 percent rare, 8 percent epic, and 2 percent
legendary. Resource quantity pointers use `common\\\_resources.csv`,
`rare\\\_resources.csv`, or `ore\\\_rewards.csv`. Ore chest ranges depend on Town Hall,
ore type, and rarity.

Cosmetics and epic equipment are selected from eligible unowned rewards. Epic
equipment must be unlockable from level zero to one under the Hero Hall count
requirements and Blacksmith level requirements. If all eligible collectibles in
a category are owned, selection rerolls within the same rarity.

`magicitems.csv` defines item targets, effects, sale values, and inventory caps.
Excess magic items from chests are automatically sold for Gems and reported in
the chest embed. Books and Hammers respect their target categories; Troop Level
also includes Siege Level. Pet Potions affect Pet research only. Wall Rings can
require multiple items per wall and use the wall upgrade confirmation flow.

Decorations and Clan Capital Houses cost 500 Gems in `/buy`. Hero Skins and Epic
Equipment cost 1500 Gems. Epic equipment purchases require a level-one Blacksmith
and the corresponding unlock prerequisites.

Common and rare chest rolls have a one percent chance of a Gem Box encounter.
Players have sixty seconds to identify where its rainbow comes from. Success
awards twenty-five to fifty Gems and increments Goblin Builders Defeated.
Incorrect answers and timeouts are recorded separately in `gembox\\\_log.csv`.
Practice encounters through `/fight` award nothing and do not record failures.

## Options and notifications

The three Option fields default to disabled:

* Notification for finished upgrades sends a direct message when upgrades finish.
* Notification for full collectors sends a direct message when producers fill.
* Hide tutorial replaces chest tutorial tips with the standard author footer.

The notification task checks approximately once per minute. Notifications do not
apply completed upgrades themselves; use `/refresh\\\_upgrades`. Delivered payloads
are removed from `notification\\\_state.json`, while minimal state prevents duplicate
notifications for unchanged slots or full collectors.

## Data files

|Path|Purpose|
|-|-|
|`data/Town Hall Loot Tables/rarities.csv`|Rarity weights.|
|`data/Town Hall Loot Tables/Town Hall <level>/<rarity>.csv`|Town Hall reward tables.|
|`data/Town Hall Loot Tables/` shared CSVs|Collection subtables and reward image lookup.|
|`data/common\\\_resources.csv` and `data/rare\\\_resources.csv`|Resource ranges by Town Hall.|
|`data/ore\\\_rewards.csv`|Ore ranges by Town Hall and rarity.|
|`data/progression/village.csv` and `collection.csv`|Ordered save fields and categories.|
|`data/progression/counts.csv` and `levels.csv`|Unlock counts and level limits.|
|`data/progression/upgrades.csv`|Upgrade costs, times, and Image column.|
|`data/progression/equipmentcost.csv`|Equipment ore costs.|
|`data/progression/resource.csv`|Production rates and capacities.|
|`data/progression/magicitems.csv`|Magic item effects, limits, and sale values.|
|`data/obstacles.csv`|Obstacle weights, removal costs, and rewards.|
|`data/upgrade\\\_ids.csv`|Stable upgrade serial mapping.|
|`data/equipment.csv` and `data/equipment\\\_images.csv`|Equipment images.|
|`data/tutorial.txt`|Chest footer tips.|
|`data/xp.csv`|Experience thresholds.|

Loot tables use headerless reward rows; progression and resource tables use their
named headers. Every Town Hall from one through eighteen needs each configured
rarity file. The loader also supports a legacy root-level `Town Hall Loot Tables`
folder. Restart after changing progression or production data; `/reload\\\_loot`
reloads loot data, not every gameplay system.

## Images

Embed images pass through `image\\\_proxy.py` and
`https://image-proxy.godly-proxy-26.workers.dev/`. Fandom URLs receive
`/revision/latest/scale-to-width-down/100` at runtime; keep source CSV links
unmodified. Upgrade images use the resulting level's Image value, while equipment
images are looked up by equipment name without a level suffix. Blank image
fields are allowed. Attempted image fetches and failures are logged to the console.

## Updating the save schema

Before changing the save layout, keep the previous schemas as
`data/progression/village-old.csv` and `collection-old.csv`. Put the new schemas
in `village.csv` and `collection.csv`, then run `/update\\\_saves` as a moderator.

Migration matches fields by source and exact name, preserves values when rows
move or categories change, initializes new fields to zero, removes deleted
fields, and clamps values when a numeric type becomes smaller. Renaming a field
is normally treated as removal and addition, so preserve spelling and case.

Migrations are staged and validated before activation. Previous saves are backed
up in `save-backups/`, and gameplay commands pause during migration. Keep
`data/progression/save\\\_migrations.json` for supported historical layouts. An
unknown fingerprint requires its matching prior schema; do not remove the schema
check or guess a binary layout.

Preserve `data/upgrade\\\_ids.csv`. New fields may receive new IDs, but existing IDs
must not be reassigned or reused: active worker slots rely on them independently
of CSV row order. Replacing costs, level requirements, resource rates, or loot
ranges alone does not change the binary save schema.
