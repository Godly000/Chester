# Town Hall 1-18 Treasure Chest Loot Tables

## Source & methodology

The requested source, https://support.supercell.com/clash-of-clans/en/articles/chest-drop-chances.html,
renders its Town-Hall-specific tables entirely client-side (a Town Hall
selector re-fetches/re-renders content via JavaScript). No fetch tool
available to me — including community mirrors — can execute that
interaction or return anything other than the page's default view
(Town Hall 18), so the underlying per-Town-Hall numbers could not be
scraped directly from that URL for Town Halls 1-17.

Instead, this data was built from the Clash of Clans Fandom wiki's
"Treasure Chests" article (https://clashofclans.fandom.com/wiki/Treasure_Chests),
which explicitly states it publishes Supercell's own official weight
system and cites the same support-page URL as its source. That article
provides:
- a constant base weight + minimum-Town-Hall-eligibility for every
  possible reward, per chest rarity
- Town-Hall-indexed quantity tables for Gold, Elixir, Dark Elixir, and
  the three Ore types

**Validation:** applying `weight = round(base_weight / total_eligible_base_weight * 10000)`
against this wiki data, using Town Hall 18's full eligible item pool,
reproduces the values in the 4 CSVs already present in the main bot's
`data/` folder **exactly** (every individual weight integer and quantity
range matches). The `Town Hall 18` folder here contains that same data.
This gives high confidence in applying the same formula and eligibility
table to Town Halls 1-17.

## Known assumption

**Builder Gold / Builder Elixir quantity ranges** are indexed by *Builder
Hall* level on the wiki, not Town Hall level, and Builder Hall progress
isn't strictly tied to Town Hall level. Since the given Town Hall 18 data
resolves this to a single fixed range (375,000-625,000 for Common,
750,000-1,250,000 for Rare), the same fixed values are reused for every
Town Hall from 4 (when the Builder Base unlocks) through 18, rather than
guessing an unverified Town-Hall-to-Builder-Hall mapping. Every other
quantity range and all weights are directly Town-Hall-indexed and not
subject to this assumption.

## Format

Matches the main bot's `data/` folder exactly:

- Each `Town Hall N/` folder has `common.csv`, `rare.csv`, `epic.csv`,
  `legendary.csv` in `item_name,weight[,extra]` format — **no image
  column**. The `extra` 3rd column follows the same rules as the main
  bot: a numeric range for item quantities, or
  `cchouse`/`decoration`/`heroskin`/`equipment` for the four category
  placeholders that reroll into their respective sub-table.
- `images.csv`, `cchouse.csv`, `decoration.csv`, `equipment.csv`, and
  `heroskin.csv` sit once at the top level (shared across every Town
  Hall, since the same items/images apply at every level) rather than
  being duplicated inside each `Town Hall N/` folder.
- Every item's image comes from a **case-sensitive** lookup of its name
  in `images.csv`, exactly like the live bot. There is no placeholder
  image: if a name isn't found (and isn't overridden by a sub-table
  roll either), the live bot shows a "❌ Image Error" instead of an
  image — these static CSVs don't have any code attached, but they're
  formatted to work as drop-in replacements for the bot's own `data/`
  folder if you swap in a specific Town Hall's tables.
