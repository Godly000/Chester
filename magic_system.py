import csv
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from player_saves import FORMAT_DETAILS
from resource_system import LAST_RESOURCE_CHECK, RESOURCE_CODES, RESOURCE_TYPES
from upgrade_system import UpgradeRejected


WORKER_TARGETS = {"Builder", "Researcher", "All Builders", "All Researchers"}


class MagicRejected(Exception):
    pass


@dataclass(frozen=True)
class MagicItem:
    name: str
    target: str
    cost: int
    strength: int
    sell: int
    capacity: int


@dataclass(frozen=True)
class MagicOption:
    value: str
    label: str
    description: str
    token: Tuple


@dataclass(frozen=True)
class MagicResult:
    item: str
    consumed: int
    lines: List[str]


class MagicSystem:
    def __init__(self, progression_dir: Path, save_store, upgrades, resources):
        self.progression_dir = progression_dir
        self.store = save_store
        self.upgrades = upgrades
        self.resources = resources
        self.items: Dict[str, MagicItem] = {}
        self.by_key: Dict[str, MagicItem] = {}

    def load(self):
        items = {}
        with (self.progression_dir / "magicitems.csv").open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            required = {"Name", "Target", "Cost", "Strength", "Sell", "Capacity"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("magicitems.csv has invalid columns")
            for row in reader:
                name = row["Name"].strip()
                numbers = [int((row[key] or "0").replace(",", "")) for key in ["Cost", "Strength", "Sell", "Capacity"]]
                if not name or min(numbers) < 0 or name.casefold() in {key.casefold() for key in items}:
                    raise ValueError(f"Invalid or duplicate magic item: {name}")
                field = self.store.village_by_name.get(name)
                if field is None or field.category != "Magic Item":
                    raise ValueError(f"Magic item is missing from the save schema: {name}")
                numbers[-1] = min(numbers[-1], FORMAT_DETAILS[field.data_type][2])
                items[name] = MagicItem(name, row["Target"].strip(), *numbers)
        self.items = items
        self.by_key = {name.casefold(): item for name, item in items.items()}

    def resolve(self, name: str) -> MagicItem:
        if not self.items:
            raise MagicRejected("Magic item data is not loaded")
        item = self.by_key.get(name.strip().casefold())
        if item is None:
            raise MagicRejected(f"Unknown magic item: {name}")
        return item

    def _credit_gems(self, village, amount):
        field = self.store.village_by_name["Gems"]
        before = village["Gems"]
        village["Gems"] = self.store._bounded_add(before, amount, field.data_type)
        credited = village["Gems"] - before
        village["Total Gems"] += credited
        return credited

    def award(self, village, name: str, amount: int):
        item = self.resolve(name)
        if amount < 0:
            raise ValueError("A magic item reward cannot be negative")
        stored = min(amount, max(0, item.capacity - village[item.name]))
        sold = amount - stored
        village[item.name] += stored
        gems = self._credit_gems(village, sold * item.sell)
        return stored, sold, gems

    def sell(self, user_id: int, name: str):
        item = self.resolve(name)
        with self.store.transaction(user_id) as (village, collection):
            self._owned(village, item)
            gems = self._credit_gems(village, item.sell)
            if gems <= 0:
                raise MagicRejected("No Gems can be received from this sale")
            village[item.name] -= 1
            return MagicResult(item.name, 1, [f"Sold for {gems:,} Gems."])

    def _owned(self, village, item, count=1):
        if village[item.name] < count:
            raise MagicRejected(f"You need {count:,} {item.name}; you have {village[item.name]:,}")

    def _worker_options(self, item, village, now):
        builder = item.target in {"Builder", "All Builders"}
        slots = self.upgrades.builder_slots if builder else self.upgrades.researcher_slots
        options = []
        status = []
        seen = set()
        for slot, clock in slots:
            prefix = slot.removesuffix(" Upgrade")
            serial, finish = village[slot], village[clock]
            name = self.upgrades.name_by_serial.get(serial) if finish > now else None
            field = self.upgrades.fields_by_name.get(name)
            number = int(re.search(r"#(\d+)", slot).group(1))
            unlocked = (number == 1 or number <= 5 and village.get(f"Builder's Hut #{number}", 0) > 0) if builder else (
                number == 1 and village.get("Laboratory", 0) >= 1 or number == 2 and village.get("Pet House", 0) >= 2
            )
            if field is None:
                status.append(f"{prefix}: {'Idle' if unlocked else 'Locked'}")
                continue
            status.append(f"{prefix}: {name}, level {village[name]} to {village[name] + 1}, finishes <t:{finish}:R>")
            if item.name == "Pet Potion" and field.category != "Pet Level":
                continue
            if serial in seen:
                raise MagicRejected("Duplicate active upgrades need to be repaired before using magic items")
            seen.add(serial)
            currency, cost = self.upgrades._cost_fields(slot)
            token = (serial, finish, village[name], village[currency], village[cost])
            options.append(MagicOption(slot, prefix, f"{name}: level {village[name]} to {village[name] + 1}", token))
        return options, status

    def _wall_option(self, item, village, collection, level):
        matches = []
        for field in self.upgrades.level_fields:
            match = re.fullmatch(r"Wall #(\d+)", field.name)
            if match and village[field.name] == level:
                matches.append((int(match.group(1)), field))
        if not matches:
            raise MagicRejected(f"No Wall is at level {level}")
        field = min(matches, key=lambda pair: pair[0])[1]
        if self.upgrades.serial_for(field) in self.upgrades._pending_serials(village):
            raise MagicRejected(f"{field.name} is already upgrading")
        self.upgrades._check_instance_order(field, village)
        self.upgrades._availability(field, village, collection)
        price = self.upgrades._price_for(field, level + 1)
        cost = sum(price.fixed_costs.values()) + price.choice_cost
        if cost <= 0:
            raise MagicRejected(f"{field.name} can be upgraded for free with /upgrade")
        if item.cost <= 0:
            raise MagicRejected("Wall Rings need a positive Cost in magicitems.csv")
        rings = (cost + item.cost - 1) // item.cost
        return MagicOption(str(level), f"Level {level} to {level + 1}", f"{field.name}: {rings:,} Wall Rings", (field.name, level, rings, price))

    def prepare(self, user_id, name, now=None):
        now = int(time.time()) if now is None else int(now)
        item = self.resolve(name)
        self.upgrades.refresh(user_id, now)
        village, collection = self.store.player_values(user_id)
        self._owned(village, item)
        if item.target in {"Builder", "Researcher"}:
            options, status = self._worker_options(item, village, now)
        elif item.target == "Wall":
            levels = sorted({village[field.name] for field in self.upgrades.level_fields if re.fullmatch(r"Wall #\d+", field.name)})
            options = []
            for level in levels:
                try:
                    options.append(self._wall_option(item, village, collection, level))
                except (MagicRejected, UpgradeRejected):
                    continue
            status = [f"You have {village[item.name]:,} Wall Rings. Choose the current Wall level."]
        else:
            return item, [], []
        if not options:
            raise MagicRejected(f"There are no eligible targets for {item.name}")
        return item, options, status

    def _reduce_cost(self, item, village, slot, name):
        if item.cost <= 0:
            return 0, []
        currency_field, cost_field = self.upgrades._cost_fields(slot)
        code, paid = village[currency_field], village[cost_field]
        warnings = []
        if code == 4 and paid == 0:
            return 0, []
        if code == 0 and paid == 0:
            price = self.upgrades._price_for(self.upgrades.fields_by_name[name], village[name] + 1)
            if price.choice_resources or len(price.fixed_costs) > 1:
                raise MagicRejected("This older upgrade has no recorded payment currency")
            resource, paid = next(iter(price.fixed_costs.items()), (None, 0))
            code = RESOURCE_CODES[resource] if resource else 4
            warnings.append("Older upgrade: the cost reduction uses the current upgrade price.")
        elif code not in RESOURCE_CODES.values():
            raise MagicRejected("The saved upgrade payment is invalid")
        reduction = min(item.cost, paid)
        village[currency_field] = code
        village[cost_field] = paid - reduction
        if not reduction:
            return 0, warnings
        resource = next(name for name, value in RESOURCE_CODES.items() if value == code)
        receipt = self.resources.deposit(village, {resource: reduction})
        credited = receipt.main[resource] + receipt.treasury[resource]
        warnings.append(f"Cost reduced by {reduction:,} {resource}: {receipt.main[resource]:,} to storage, {receipt.treasury[resource]:,} to treasury.")
        return credited, warnings

    def use(self, user_id, name, *, option: Optional[MagicOption] = None, expected_item=None, now=None):
        now = int(time.time()) if now is None else int(now)
        item = self.resolve(name)
        if expected_item is not None and item != expected_item:
            raise MagicRejected("The item data changed. Run /use again")
        self.upgrades.refresh(user_id, now)
        with self.store.transaction(user_id) as (village, collection):
            self._owned(village, item)
            lines = []
            consumed = 1
            if item.name == "Research Potion" or item.target == "Resources":
                if item.strength <= 0:
                    raise MagicRejected("This item has no production time skip configured")
                previous = village[LAST_RESOURCE_CHECK] or now
                village[LAST_RESOURCE_CHECK] = max(1, previous - item.strength)
                lines.append(f"Production advanced by {previous - village[LAST_RESOURCE_CHECK]:,} seconds. Use /collect_loot to collect it.")
            elif item.target in RESOURCE_TYPES:
                capacities, _ = self.resources.capacities(village)
                amount = min(item.strength, max(0, capacities[item.target] - village[item.target]))
                if amount <= 0:
                    raise MagicRejected(f"{item.target} storage is full or this item has no resource strength")
                village[item.target] += amount
                total = f"Total {item.target}"
                if total in village:
                    village[total] += amount
                lines.append(f"Added {amount:,} {item.target} to main storage.")
            elif item.target == "Wall":
                if option is None:
                    raise MagicRejected("Choose a Wall level first")
                current = self._wall_option(item, village, collection, int(option.value))
                if current.token != option.token:
                    raise MagicRejected("The selected Wall or its cost changed. Run /use again")
                wall, level, consumed, _ = current.token
                self._owned(village, item, consumed)
                village[wall] = level + 1
                lines.append(f"{wall} upgraded from level {level} to {level + 1} using {consumed:,} Wall Rings.")
            elif item.target in WORKER_TARGETS:
                options, _ = self._worker_options(item, village, now)
                if item.target in {"Builder", "Researcher"}:
                    if option is None:
                        raise MagicRejected("Choose a worker first")
                    options = [entry for entry in options if entry.value == option.value and entry.token == option.token]
                    if not options:
                        raise MagicRejected("That worker's upgrade changed or finished. Run /use again")
                if not options:
                    raise MagicRejected("No eligible upgrades are in progress")
                changed = False
                for entry in options:
                    slot = entry.value
                    clock = slot.removesuffix(" Upgrade") + " Time"
                    name = self.upgrades.name_by_serial[village[slot]]
                    credited, refund_lines = self._reduce_cost(item, village, slot, name)
                    seconds = min(item.strength, max(0, village[clock] - now))
                    village[clock] -= seconds
                    changed = changed or seconds > 0 or credited > 0
                    lines.append(f"{entry.label}: {name}, reduced by {seconds:,} seconds.")
                    lines.extend(refund_lines)
                if not changed:
                    raise MagicRejected("This item would have no effect on those upgrades")
                completed = self.upgrades._refresh_values(village, now).completed
                lines.extend(f"Completed {entry.item}: level {entry.new_level}." for entry in completed)
            else:
                raise MagicRejected(f"{item.name} has no use configured yet. You can sell it for {item.sell:,} Gems")
            village[item.name] -= consumed
            return MagicResult(item.name, consumed, lines)