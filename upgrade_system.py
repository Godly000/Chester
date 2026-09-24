import csv
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from player_saves import FORMAT_DETAILS, SaveField, SaveStore
from resource_system import RESOURCE_CODES, ResourceReceipt, ResourceSystem


STRUCTURE_CATEGORIES = {
    "Structure Level", "Defense Level", "Wall Level", "Trap Level",
    "Resource Level", "Army Level",
}
BUILDER_CATEGORIES = STRUCTURE_CATEGORIES | {"Hero Level"}
SECOND_BUILDER_COST = 250
THIRD_BUILDER_COST = 500
FOURTH_BUILDER_COST = 1000
FIFTH_BUILDER_COST = 2000
BUILDER_UNLOCK_COSTS = {
    2: SECOND_BUILDER_COST,
    3: THIRD_BUILDER_COST,
    4: FOURTH_BUILDER_COST,
    5: FIFTH_BUILDER_COST,
}
BUILDER_BASE_MESSAGE = "WIP: Builder Base update coming soon!"
GOBLIN_RESEARCHER_MESSAGE = "Goblin Researcher isn't here to steal your gems!"
RESEARCHER_CATEGORIES = {
    "Troop Level",
    "Spell Level",
    "Siege Level",
    "Pet Level",
}
EQUIPMENT_CATEGORY = "Equipment Level"
TABLE_NAME_ALIASES = {"Electric Owl": "Electro Owl"}
TOWN_HALL_WEAPONS = {
    17: "Town Hall 17 Weapon",
}
TOWN_HALL_COST_ROWS = {
    17: "TH17 Inferno Artillery 1",
}
WEAPON_COST_ALIASES = {
    "TH17 Inferno Artillery": "Town Hall 17 Weapon",
}
CRAFTED_DEFENSES = [
    "Crafted Defense AA",
    "Crafted Defense AB",
    "Crafted Defense AC",
    "Crafted Defense BA",
    "Crafted Defense BB",
    "Crafted Defense BC",
    "Crafted Defense CA",
    "Crafted Defense CB",
    "Crafted Defense CC",
]
INSTANCE_PATTERN = re.compile(r"^(.*) #(\d+)$")
UPGRADE_ROW_PATTERN = re.compile(r"^(.*) (\d+)(?:\.(\d+))?$")
CRAFTED_ROW_PATTERN = re.compile(r"^Crafted Defense ([1-9])([1-9])$")


class UpgradeRejected(Exception):
    pass


class WorkerUnavailable(UpgradeRejected):
    pass


class TownHallUpgradeBlocked(UpgradeRejected):
    def __init__(self, count: int):
        label = "upgrade" if count == 1 else "upgrades"
        super().__init__(
            f"You have {count:,} {label} remaining before you can upgrade to the next "
            "Town Hall, use the `remaining` command to see a full list."
        )


@dataclass(frozen=True)
class RemainingUpgrade:
    item: str
    current_level: int
    target_level: int
    slot: Optional[str] = None
    finish_time: Optional[int] = None

    @property
    def count(self) -> int:
        return max(int(self.slot is not None), self.target_level - self.current_level)


@dataclass(frozen=True)
class UpgradePrice:
    duration: int
    fixed_costs: Dict[str, int]
    choice_resources: Tuple[str, ...] = ()
    choice_cost: int = 0


@dataclass(frozen=True)
class UpgradeOutcome:
    item: str
    previous_level: int
    target_level: int
    instant: bool
    slot: Optional[str]
    finish_time: Optional[int]
    costs: Dict[str, int]


@dataclass(frozen=True)
class CancelledUpgrade:
    item: str
    slot: str
    refunds: Dict[str, int]
    received: ResourceReceipt
    warnings: List[str]


@dataclass(frozen=True)
class CompletedUpgrade:
    item: str
    new_level: int
    slot: str


@dataclass(frozen=True)
class ActiveUpgrade:
    item: str
    finish_time: int
    slot: str


@dataclass(frozen=True)
class RefreshReport:
    completed: List[CompletedUpgrade]
    active: List[ActiveUpgrade]
    warnings: List[str]


class UpgradeCurrencyChoiceRequired(UpgradeRejected):
    def __init__(
        self,
        item: str,
        current_level: int,
        price: UpgradePrice,
        resources: Tuple[str, ...],
        refreshed: RefreshReport,
    ):
        super().__init__("Choose a currency for this upgrade")
        self.item = item
        self.current_level = current_level
        self.price = UpgradePrice(
            price.duration,
            dict(price.fixed_costs),
            price.choice_resources,
            price.choice_cost,
        )
        self.resources = resources
        self.refreshed = refreshed


class UpgradeSystem:
    def __init__(
        self,
        progression_dir: Path,
        save_store: SaveStore,
        common_equipment_max: int,
        epic_equipment_max: int,
        resource_system: Optional[ResourceSystem] = None,
    ):
        self.resource_system = resource_system
        self.progression_dir = progression_dir
        self.save_store = save_store
        self.common_equipment_max = common_equipment_max
        self.epic_equipment_max = epic_equipment_max
        self.level_fields: List[SaveField] = []
        self.fields_by_name: Dict[str, SaveField] = {}
        self.fields_by_key: Dict[str, SaveField] = {}
        self.count_rows: Dict[str, Dict[str, str]] = {}
        self.level_rows: Dict[str, Dict[str, str]] = {}
        self.normal_prices: Dict[Tuple[str, int], UpgradePrice] = {}
        self.equipment_prices: Dict[int, Dict[str, int]] = {}
        self.serial_by_name: Dict[str, int] = {}
        self.name_by_serial: Dict[int, str] = {}
        self.builder_slots: List[Tuple[str, str]] = []
        self.researcher_slots: List[Tuple[str, str]] = []

    def load(self) -> None:
        self.save_store._require_schema()
        self.level_fields = [
            field
            for field in self.save_store.fields
            if "level" in field.category.casefold() or field.name == "Town Hall"
        ]
        self.fields_by_name = {field.name: field for field in self.level_fields}
        self.fields_by_key = {field.name.casefold(): field for field in self.level_fields}
        self.count_rows = self._read_progression_table("counts.csv")
        self.level_rows = self._read_progression_table("levels.csv")
        self.normal_prices = self._load_normal_prices()
        self.equipment_prices = self._load_equipment_prices()
        self.builder_slots = self._load_slots("Builder")
        self.researcher_slots = self._load_slots("Researcher")
        self._load_serials()
        self._validate_slots()

    def _read_progression_table(self, filename: str) -> Dict[str, Dict[str, str]]:
        path = self.progression_dir / filename
        with path.open(newline="", encoding="utf-8-sig") as file:
            rows = list(csv.DictReader(file))
        result: Dict[str, Dict[str, str]] = {}
        for row in rows:
            name = row["Name"].strip()
            if name in result:
                raise ValueError(f"Duplicate row in {filename}: {name}")
            result[name] = {key: value.strip() for key, value in row.items()}
        return result

    def _read_csv_rows(self, filename: str) -> List[Dict[str, str]]:
        path = self.progression_dir / filename
        with path.open(newline="", encoding="utf-8-sig") as file:
            return [
                {key: value.strip() for key, value in row.items()}
                for row in csv.DictReader(file)
            ]

    def _parse_number(self, value: str) -> int:
        return int(value.replace(",", "").strip())

    def _base_name(self, name: str) -> str:
        match = INSTANCE_PATTERN.match(name)
        return match.group(1) if match else name

    def _table_row(
        self,
        rows: Mapping[str, Dict[str, str]],
        base_name: str,
    ) -> Optional[Dict[str, str]]:
        return rows.get(base_name) or rows.get(
            TABLE_NAME_ALIASES.get(base_name, base_name)
        )

    def _normal_price(self, row: Mapping[str, str]) -> UpgradePrice:
        duration = self._parse_number(row["Time"])
        amount = self._parse_number(row["Cost"])
        if duration < 0 or amount < 0:
            raise ValueError(f"Negative upgrade data for {row['Name']}")
        resource = row["Resource"]
        if resource in {"DE", "Dark Elixir"}:
            return UpgradePrice(duration, {"Dark Elixir": amount})
        if resource == "Both":
            return UpgradePrice(duration, {}, ("Gold", "Elixir"), amount)
        if resource not in {"Gold", "Elixir"}:
            raise ValueError(f"Unknown upgrade resource: {resource}")
        return UpgradePrice(duration, {resource: amount})

    def _load_normal_prices(self) -> Dict[Tuple[str, int], UpgradePrice]:
        rows = self._read_csv_rows("upgrades.csv")
        self.upgrade_images = {}
        rows_by_name = {row["Name"]: row for row in rows}
        if len(rows_by_name) != len(rows):
            raise ValueError("upgrades.csv contains duplicate names")
        known_bases = {
            self._base_name(field.name)
            for field in self.level_fields
            if field.category != EQUIPMENT_CATEGORY
        }
        aliases = {base: base for base in known_bases}
        aliases.update(WEAPON_COST_ALIASES)
        prices: Dict[Tuple[str, int], UpgradePrice] = {}

        for row in rows:
            crafted = CRAFTED_ROW_PATTERN.match(row["Name"])
            if crafted:
                group = int(crafted.group(1))
                target_level = int(crafted.group(2)) + 1
                key = CRAFTED_DEFENSES[group - 1], target_level
                if key in prices:
                    raise ValueError(
                        f"Duplicate upgrade price for {key[0]} level {target_level}"
                    )
                self.upgrade_images[key] = (row.get("Image") or "").strip()
                prices[key] = self._normal_price(row)
                continue

            match = UPGRADE_ROW_PATTERN.match(row["Name"])
            if not match:
                continue
            alias, whole_level, stage = match.groups()
            base_name = aliases.get(alias)
            if base_name is None:
                continue
            target_level = int(whole_level) + (int(stage) if stage else 0)
            key = base_name, target_level
            if key in prices:
                raise ValueError(f"Duplicate upgrade price for {base_name} level {target_level}")
            self.upgrade_images[key] = (row.get("Image") or "").strip()
            prices[key] = self._normal_price(row)

        for target_level, row_name in TOWN_HALL_COST_ROWS.items():
            self.upgrade_images[("Town Hall", target_level)] = (rows_by_name[row_name].get("Image") or "").strip()
            prices[("Town Hall", target_level)] = self._normal_price(rows_by_name[row_name])
        for weapon_name in TOWN_HALL_WEAPONS.values():
            prices.pop((weapon_name, 1), None)
        return prices

    def _load_equipment_prices(self) -> Dict[int, Dict[str, int]]:
        rows = self._read_csv_rows("equipmentcost.csv")
        prices: Dict[int, Dict[str, int]] = {}
        for row in rows:
            level = self._parse_number(row["Level"])
            if level in prices:
                raise ValueError(f"Duplicate equipment cost for level {level}")
            costs = {
                "Shiny Ore": self._parse_number(row["Shiny"]),
                "Glowy Ore": self._parse_number(row["Glowy"]),
                "Starry Ore": self._parse_number(row["Starry"]),
            }
            if level < 1 or any(amount < 0 for amount in costs.values()):
                raise ValueError(f"Invalid equipment cost data for level {level}")
            prices[level] = costs
        return prices

    def _load_slots(self, prefix: str) -> List[Tuple[str, str]]:
        slots: List[Tuple[int, str, str]] = []
        pattern = re.compile(rf"^{re.escape(prefix)} #(\d+) Upgrade$")
        for name in self.save_store.village_by_name:
            match = pattern.match(name)
            if not match:
                continue
            number = int(match.group(1))
            time_name = f"{prefix} #{number} Time"
            if time_name not in self.save_store.village_by_name:
                raise ValueError(f"Missing upgrade time field: {time_name}")
            slots.append((number, name, time_name))
        return [(upgrade, finish) for _, upgrade, finish in sorted(slots)]

    def _load_serials(self) -> None:
        path = self.progression_dir / "upgrade_ids.csv"
        rows: List[Tuple[int, str, str]] = []
        if path.exists():
            with path.open(newline="", encoding="utf-8-sig") as file:
                reader = csv.DictReader(file)
                required = {"Serial", "Name", "Category"}
                if not reader.fieldnames or not required.issubset(reader.fieldnames):
                    raise ValueError("upgrade_ids.csv has invalid columns")
                for row in reader:
                    rows.append((int(row["Serial"]), row["Name"], row["Category"]))

        serial_by_name: Dict[str, int] = {}
        name_by_serial: Dict[int, str] = {}
        for serial, name, category in rows:
            if serial < 0 or serial > 32767:
                raise ValueError(f"Upgrade serial is out of range: {serial}")
            if name in serial_by_name or serial in name_by_serial:
                raise ValueError("upgrade_ids.csv contains duplicate mappings")
            serial_by_name[name] = serial
            name_by_serial[serial] = name

        next_serial = max(name_by_serial, default=0) + 1
        changed = not path.exists()
        for field in self.level_fields:
            if field.name in serial_by_name:
                continue
            if next_serial > 32767:
                raise ValueError("No upgrade serials remain")
            serial_by_name[field.name] = next_serial
            name_by_serial[next_serial] = field.name
            rows.append((next_serial, field.name, field.category))
            next_serial += 1
            changed = True

        if changed:
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            try:
                with temporary.open("w", newline="", encoding="utf-8") as file:
                    writer = csv.writer(file)
                    writer.writerow(["Serial", "Name", "Category"])
                    for serial, name, category in sorted(rows):
                        writer.writerow([serial, name, category])
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()

        self.serial_by_name = serial_by_name
        self.name_by_serial = name_by_serial

    def _validate_slots(self) -> None:
        if not self.builder_slots or not self.researcher_slots:
            raise ValueError("Builder and Researcher upgrade slots are required")
        maximum_serial = max(self.name_by_serial, default=0)
        for upgrade_name, time_name in self.builder_slots + self.researcher_slots:
            for cost_name in self._cost_fields(upgrade_name):
                if cost_name not in self.save_store.village_by_name:
                    raise ValueError(f"Missing upgrade cost save field: {cost_name}")
            upgrade_field = self.save_store.village_by_name[upgrade_name]
            time_field = self.save_store.village_by_name[time_name]
            if FORMAT_DETAILS[upgrade_field.data_type][2] < maximum_serial:
                raise ValueError(
                    f"{upgrade_name} cannot store upgrade serial {maximum_serial}"
                )
            if FORMAT_DETAILS[time_field.data_type][2] < int(time.time()):
                raise ValueError(f"{time_name} cannot store a current UNIX timestamp")

    def resolve_item(self, item: str) -> SaveField:
        worker = re.fullmatch(r"(?:Builder|Builder's Hut) #(\d+)", item.strip(), re.IGNORECASE)
        if worker:
            number = int(worker.group(1))
            if number >= 6:
                raise WorkerUnavailable(BUILDER_BASE_MESSAGE)
            item = f"Builder's Hut #{number}"
        researcher = re.fullmatch(r"Researcher #(\d+)", item.strip(), re.IGNORECASE)
        if researcher:
            number = int(researcher.group(1))
            if number >= 3:
                raise WorkerUnavailable(GOBLIN_RESEARCHER_MESSAGE)
            requirement = "Laboratory level 1" if number == 1 else "Pet House level 2"
            raise UpgradeRejected(f"Researcher #{number} unlocks automatically at {requirement}")
        field = self.fields_by_key.get(item.strip().casefold())
        if field is None:
            raise UpgradeRejected(f"Unknown upgradeable item: {item}")
        return field

    def serial_for(self, field: SaveField) -> int:
        return self.serial_by_name[field.name]

    def _column_value(self, row: Mapping[str, str], level: int) -> int:
        if level <= 0:
            return 0
        numeric_columns = [int(key) for key in row if key.isdigit()]
        column = str(min(level, max(numeric_columns)))
        return self._parse_number(row[column])

    def _prerequisite_level(
        self,
        requirement: str,
        village: Mapping[str, int],
    ) -> int:
        if requirement not in village:
            raise UpgradeRejected(f"Missing prerequisite save field: {requirement}")
        return village[requirement]

    def _equipment_template_row(self, epic: bool) -> Dict[str, str]:
        target_max = self.epic_equipment_max if epic else self.common_equipment_max
        for row in self.level_rows.values():
            if row["Requirement"] == "Blacksmith" and self._parse_number(row["19"]) == target_max:
                return row
        raise UpgradeRejected("Equipment maximum-level data is incomplete")

    def _maximum_level(
        self,
        field: SaveField,
        village: Mapping[str, int],
        collection: Mapping[str, int],
    ) -> int:
        base_name = self._base_name(field.name)
        row = self._table_row(self.level_rows, base_name)
        if row is None and field.category == EQUIPMENT_CATEGORY:
            epic = bool(collection.get(field.name, 0))
            row = self._equipment_template_row(epic)
        if row is None:
            raise UpgradeRejected(f"No maximum-level data exists for {field.name}")
        prerequisite = self._prerequisite_level(row["Requirement"], village)
        return self._column_value(row, prerequisite)

    def _allowed_count(
        self,
        field: SaveField,
        village: Mapping[str, int],
        collection: Mapping[str, int],
    ) -> Tuple[int, str, int]:
        base_name = self._base_name(field.name)
        row = self._table_row(self.count_rows, base_name)
        if row is None:
            raise UpgradeRejected(f"No unlock data exists for {field.name}")
        requirement = row["Requirement"]
        if requirement == "Collection":
            owned = int(bool(collection.get(field.name, 0)))
            return owned, requirement, owned
        prerequisite = self._prerequisite_level(requirement, village)
        return self._column_value(row, prerequisite), requirement, prerequisite

    def _availability(
        self,
        field: SaveField,
        village: Mapping[str, int],
        collection: Mapping[str, int],
        reject_at_maximum: bool = True,
    ) -> int:
        current_level = village[field.name]
        if field.name == "Town Hall":
            if current_level >= 18 and reject_at_maximum:
                raise UpgradeRejected("Town Hall is already at its maximum level")
            return 18

        allowed_count, requirement, prerequisite = self._allowed_count(
            field, village, collection
        )
        match = INSTANCE_PATTERN.match(field.name)
        instance = int(match.group(2)) if match else 1
        if instance > allowed_count:
            if requirement == "Collection":
                raise UpgradeRejected(
                    f"{field.name} is not unlocked in the player's collection"
                )
            raise UpgradeRejected(
                f"{field.name} is not unlocked: {requirement} level {prerequisite} "
                f"allows {allowed_count}"
            )

        maximum = self._maximum_level(field, village, collection)
        if maximum <= current_level and reject_at_maximum:
            raise UpgradeRejected(
                f"{field.name} cannot be upgraded: its current level is {current_level} "
                f"and the maximum allowed level is {maximum}"
            )
        return maximum

    def _equipment_cap(self, field: SaveField) -> int:
        row = self._table_row(self.level_rows, self._base_name(field.name))
        if row is None:
            if field.name in self.save_store.collection_by_name:
                return self.epic_equipment_max
            return self.common_equipment_max
        return self._parse_number(row["19"])

    def _price_for(self, field: SaveField, target_level: int) -> UpgradePrice:
        if target_level == 1:
            builder = re.fullmatch(r"Builder's Hut #(\d+)", field.name)
            if builder:
                number = int(builder.group(1))
                if number >= 6:
                    raise WorkerUnavailable(BUILDER_BASE_MESSAGE)
                cost = BUILDER_UNLOCK_COSTS.get(number, 0)
                return UpgradePrice(0, {"Gems": cost} if cost else {})
        if field.category == EQUIPMENT_CATEGORY:
            costs = self.equipment_prices.get(target_level)
            if costs is None:
                raise UpgradeRejected(
                    f"No equipment cost data exists for level {target_level}"
                )
            fixed_costs = dict(costs)
            if self._equipment_cap(field) == self.common_equipment_max:
                fixed_costs.pop("Starry Ore", None)
            return UpgradePrice(0, fixed_costs)

        base_name = self._base_name(field.name)
        price = self.normal_prices.get((base_name, target_level))
        if price is not None:
            return price
        if target_level == 1:
            return UpgradePrice(0, {})
        raise UpgradeRejected(
            f"No upgrade statistics exist for {field.name} level {target_level}"
        )

    def _affordable_currencies(
        self,
        price: UpgradePrice,
        village: Mapping[str, int],
    ) -> Tuple[str, ...]:
        if not price.choice_cost:
            return ()
        return tuple(
            resource
            for resource in price.choice_resources
            if village.get(resource, 0) - price.fixed_costs.get(resource, 0)
            >= price.choice_cost
        )

    def _deductions(
        self,
        price: UpgradePrice,
        village: Mapping[str, int],
        currency: Optional[str] = None,
    ) -> Dict[str, int]:
        if currency is not None and currency not in price.choice_resources:
            raise UpgradeRejected(f"{currency} cannot be used for this upgrade")
        missing: List[str] = []
        deductions: Dict[str, int] = {}
        for resource, amount in price.fixed_costs.items():
            available = village.get(resource, 0)
            if available < amount:
                missing.append(f"{resource}: need {amount:,}, have {available:,}")
            elif amount:
                deductions[resource] = amount

        if price.choice_resources and price.choice_cost:
            affordable = self._affordable_currencies(price, village)
            if currency is not None:
                selected = currency
                if selected not in affordable:
                    required = price.choice_cost + price.fixed_costs.get(selected, 0)
                    missing.append(
                        f"{selected}: need {required:,}, have {village.get(selected, 0):,}"
                    )
                else:
                    deductions[selected] = deductions.get(selected, 0) + price.choice_cost
            elif not affordable:
                balances = ", ".join(
                    f"{resource} {village.get(resource, 0):,}"
                    for resource in price.choice_resources
                )
                missing.append(
                    f"need {price.choice_cost:,} of one of "
                    f"{', '.join(price.choice_resources)}; have {balances}"
                )
            elif len(affordable) == 1:
                selected = affordable[0]
                deductions[selected] = deductions.get(selected, 0) + price.choice_cost
            elif not missing:
                raise UpgradeRejected("Choose a currency before starting this upgrade")

        if missing:
            raise UpgradeRejected("Inadequate resources: " + "; ".join(missing))
        return deductions

    def _slot_values(
        self,
        village: Mapping[str, int],
    ) -> List[Tuple[str, str, int, int]]:
        result: List[Tuple[str, str, int, int]] = []
        for upgrade_name, time_name in self.builder_slots + self.researcher_slots:
            result.append(
                (upgrade_name, time_name, village[upgrade_name], village[time_name])
            )
        return result

    def _find_free_slot(
        self,
        field: SaveField,
        village: Mapping[str, int],
    ) -> Tuple[str, str]:
        if field.category in BUILDER_CATEGORIES or field.name == "Town Hall":
            slots = self.builder_slots
            slot_type = "Builder"
        elif field.category in RESEARCHER_CATEGORIES:
            slots = self.researcher_slots
            slot_type = "Researcher"
        else:
            raise UpgradeRejected(
                f"No upgrade slot type is configured for category {field.category}"
            )
        for upgrade_name, time_name in slots:
            number = int(re.search(r"#(\d+)", upgrade_name).group(1))
            if slot_type == "Builder":
                unlocked = number == 1 or (
                    number in BUILDER_UNLOCK_COSTS and village.get(f"Builder's Hut #{number}", 0) >= 1
                )
            else:
                unlocked = (
                    number == 1 and village.get("Laboratory", 0) >= 1
                ) or (
                    number == 2 and village.get("Pet House", 0) >= 2
                )
            if not unlocked:
                continue
            serial = village[upgrade_name]
            finish_time = village[time_name]
            if serial == 0 and finish_time == 0:
                return upgrade_name, time_name
            if finish_time <= 0 or serial not in self.name_by_serial:
                raise UpgradeRejected(f"Upgrade slot {upgrade_name} contains inconsistent data")
        if slot_type == "Researcher":
            if village.get("Laboratory", 0) < 1:
                raise UpgradeRejected("Unlock the first Researcher by completing Laboratory level 1")
            if village.get("Pet House", 0) < 2:
                raise UpgradeRejected("The first Researcher is busy. Complete Pet House level 2 to unlock the second Researcher")
            raise WorkerUnavailable(GOBLIN_RESEARCHER_MESSAGE)
        raise UpgradeRejected("No unlocked Builder is free. Wait for an upgrade or unlock another Builder with /upgrade")

    def _pending_serials(self, village: Mapping[str, int]) -> Dict[int, str]:
        pending: Dict[int, str] = {}
        for upgrade_name, _, serial, finish_time in self._slot_values(village):
            if serial or finish_time:
                pending[serial] = upgrade_name
        return pending

    def _supported_maximum(self, field: SaveField, allowed_maximum: int) -> int:
        if field.category == EQUIPMENT_CATEGORY:
            return allowed_maximum
        base_name = self._base_name(field.name)
        supported = 0
        for target_level in range(1, allowed_maximum + 1):
            if target_level == 1 or (base_name, target_level) in self.normal_prices:
                supported = target_level
            else:
                break
        return supported

    def _town_hall_blockers(
        self,
        village: Mapping[str, int],
        collection: Mapping[str, int],
    ) -> List[str]:
        return [
            f"{entry.item} is level {entry.current_level}/{entry.target_level}"
            for entry in self.remaining_upgrades(village, collection)
        ]

    def remaining_upgrades(
        self, village: Mapping[str, int], collection: Mapping[str, int]
    ) -> List[RemainingUpgrade]:
        projected = dict(village)
        candidates = [
            field for field in self.level_fields
            if field.name != "Town Hall" and not (
                self._base_name(field.name) == "Builder's Hut" and village[field.name] == 0
            )
        ]
        for _ in range(len(candidates) + 1):
            changed = False
            for field in candidates:
                try:
                    maximum = self._availability(field, projected, collection, reject_at_maximum=False)
                except UpgradeRejected as error:
                    if "not unlocked" in str(error):
                        continue
                    raise
                target = self._supported_maximum(field, maximum)
                if target > projected[field.name]:
                    projected[field.name] = target
                    changed = True
            if not changed:
                break
        pending = {}
        for slot, _, serial, finish_time in self._slot_values(village):
            if serial == 0 and finish_time == 0:
                continue
            item = self.name_by_serial.get(serial, f"Unknown upgrade in {slot}")
            pending.setdefault(item, (slot, finish_time))
        remaining = []
        for field in self.level_fields:
            current = village[field.name]
            target = projected[field.name]
            active = pending.pop(field.name, None)
            if target > current or active is not None:
                slot, finish = active if active else (None, None)
                remaining.append(RemainingUpgrade(field.name, current, max(target, current + int(active is not None)), slot, finish))
        for name, (slot, finish) in pending.items():
            remaining.append(RemainingUpgrade(name, 0, 1, slot, finish))
        return remaining

    def _completion_side_effects(
        self,
        item: str,
        new_level: int,
        village: Mapping[str, int],
    ) -> Dict[str, int]:
        changes: Dict[str, int] = {}
        if item == "Town Hall":
            weapon = TOWN_HALL_WEAPONS.get(new_level)
            if weapon and village.get(weapon, 0) == 0:
                changes[weapon] = 1
        return changes

    def _cost_fields(self, slot: str) -> Tuple[str, str]:
        prefix = slot.removesuffix(" Upgrade")
        return f"{prefix} Currency", f"{prefix} Cost"

    def _clear_slot(self, village: Dict[str, int], slot: str, time_name: str) -> None:
        village[slot] = 0
        village[time_name] = 0
        for name in self._cost_fields(slot):
            if name in village:
                village[name] = 0

    def _check_instance_order(self, field: SaveField, village: Mapping[str, int], batch_started=()) -> None:
        match = INSTANCE_PATTERN.match(field.name)
        if field.category not in STRUCTURE_CATEGORIES or match is None:
            return
        base, number = match.group(1), int(match.group(2))
        alternatives = []
        for candidate in self.level_fields:
            if candidate.name in batch_started:
                continue
            other = INSTANCE_PATTERN.match(candidate.name)
            if candidate.category != field.category or other is None:
                continue
            if other.group(1) == base and int(other.group(2)) < number:
                if village[candidate.name] == village[field.name]:
                    alternatives.append((int(other.group(2)), candidate.name))
        if alternatives:
            lowest = min(alternatives)[1]
            raise UpgradeRejected(
                f"Upgrade {lowest} first because it is the lowest numbered {base} "
                f"at level {village[field.name]}. If it is upgrading, wait for it to finish."
            )

    def refresh(self, user_id: int, now: Optional[int] = None) -> RefreshReport:
        now = int(time.time()) if now is None else int(now)
        with self.save_store.transaction(user_id) as (village, collection):
            return self._refresh_values(village, now)

    def _refresh_values(self, village: Dict[str, int], now: int) -> RefreshReport:
        if village.get("Town Hall", 0) >= 1 and village.get("Builder's Hut #1", 0) == 0:
            village["Builder's Hut #1"] = 1
        completed = []
        active = []
        warnings = []
        completed_serials = set()
        slots = sorted(self._slot_values(village), key=lambda slot: slot[3])
        for upgrade_name, time_name, serial, finish_time in slots:
            if serial == 0 and finish_time == 0:
                continue
            if finish_time <= 0:
                warnings.append(f"{upgrade_name} contains inconsistent data")
                continue
            item = self.name_by_serial.get(serial)
            if item is None:
                warnings.append(f"{upgrade_name} contains unknown serial {serial}")
                continue
            field = self.fields_by_name.get(item)
            if field is None:
                warnings.append(f"{upgrade_name} referred to removed item {item} and was cleared")
                self._clear_slot(village, upgrade_name, time_name)
                continue
            if finish_time > now:
                if serial in completed_serials:
                    warnings.append(f"Duplicate upgrade for {item} was cleared")
                    self._clear_slot(village, upgrade_name, time_name)
                else:
                    active.append(ActiveUpgrade(item, finish_time, upgrade_name))
                continue
            if serial not in completed_serials:
                if self.resource_system and self.resource_system.is_producer(item):
                    self.resource_system.collect(village, finish_time, automatic=True)
                new_level = village[item] + 1
                village[item] = new_level
                village.update(self._completion_side_effects(item, new_level, village))
                completed.append(CompletedUpgrade(item, new_level, upgrade_name))
                completed_serials.add(serial)
            else:
                warnings.append(f"Duplicate completed upgrade for {item} was cleared")
            self._clear_slot(village, upgrade_name, time_name)
        return RefreshReport(completed, active, warnings)

    def start_upgrade(
        self,
        user_id: int,
        item: str,
        now: Optional[int] = None,
        *,
        currency: Optional[str] = None,
        expected_level: Optional[int] = None,
        expected_price: Optional[UpgradePrice] = None,
    ) -> Tuple[UpgradeOutcome, RefreshReport]:
        now = int(time.time()) if now is None else int(now)
        refresh_report = self.refresh(user_id, now)
        with self.save_store.transaction(user_id) as (village, collection):
            return self._start_values(
                village, collection, item, now, currency, expected_level,
                expected_price, refresh_report,
            )

    def _start_values(
        self, village, collection, item, now, currency, expected_level,
        expected_price, refresh_report, batch_started=(),
    ):
        field = self.resolve_item(item)
        current_level = village[field.name]
        if expected_level is not None and current_level != expected_level:
            raise UpgradeRejected(
                f"{field.name} changed level while you were choosing. Use /upgrade again."
            )
        serial = self.serial_for(field)
        pending = self._pending_serials(village)
        if serial in pending:
            raise UpgradeRejected(
                f"{field.name} is already upgrading in {pending[serial]}"
            )

        self._check_instance_order(field, village, batch_started)
        maximum = self._availability(field, village, collection)
        target_level = current_level + 1

        if field.name == "Town Hall" and current_level > 0:
            remaining = self.remaining_upgrades(village, collection)
            if remaining:
                raise TownHallUpgradeBlocked(sum(entry.count for entry in remaining))

        if target_level > maximum:
            raise UpgradeRejected(
                f"{field.name} cannot exceed its current maximum level of {maximum}"
            )
        price = self._price_for(field, target_level)
        if expected_price is not None and price != expected_price:
            raise UpgradeRejected(
                "The upgrade cost or duration changed while you were choosing. Use /upgrade again."
            )
        set_values: Dict[str, int] = {}
        slot_name: Optional[str] = None
        finish_time: Optional[int] = None

        if price.duration == 0:
            set_values[field.name] = target_level
            set_values.update(
                self._completion_side_effects(field.name, target_level, village)
            )
        else:
            if field.category == EQUIPMENT_CATEGORY:
                raise UpgradeRejected("Equipment upgrades must be instantaneous")
            upgrade_name, time_name = self._find_free_slot(field, village)
            finish_time = now + price.duration
            time_field = self.save_store.village_by_name[time_name]
            time_limit = FORMAT_DETAILS[time_field.data_type][2]
            if finish_time < 1 or finish_time > time_limit:
                raise UpgradeRejected("Upgrade finish time exceeds the save format limit")
            set_values[upgrade_name] = serial
            set_values[time_name] = finish_time
            slot_name = upgrade_name

        affordable = self._affordable_currencies(price, village)
        if currency is None and len(affordable) > 1:
            self._deductions(price, village, affordable[0])
            raise UpgradeCurrencyChoiceRequired(
                field.name, current_level, price, affordable, refresh_report
            )
        deductions = self._deductions(price, village, currency)
        if self.resource_system and self.resource_system.is_producer(field.name):
            self.resource_system.collect(village, now, automatic=True)
        if slot_name:
            currency_field, cost_field = self._cost_fields(slot_name)
            if len(deductions) > 1:
                raise UpgradeRejected("Timed upgrades must use a single resource")
            resource, amount = next(iter(deductions.items()), (None, 0))
            set_values[currency_field] = RESOURCE_CODES[resource] if resource else 4
            set_values[cost_field] = amount
        for resource, amount in deductions.items():
            village[resource] -= amount
        village.update(set_values)
        return (
            UpgradeOutcome(
                item=field.name,
                previous_level=current_level,
                target_level=target_level,
                instant=price.duration == 0,
                slot=slot_name,
                finish_time=finish_time,
                costs=deductions,
            ),
            refresh_report,
        )

    def cancel_upgrade(self, user_id: int, item: str, now: Optional[int] = None):
        now = int(time.time()) if now is None else int(now)
        self.refresh(user_id, now)
        if self.resource_system is None:
            raise UpgradeRejected("Resource storage data is not loaded")
        with self.save_store.transaction(user_id) as (village, collection):
            selected = None
            key = item.strip().casefold()
            for slot, time_name, serial, finish_time in self._slot_values(village):
                if finish_time <= now:
                    continue
                name = self.name_by_serial.get(serial)
                if name is None:
                    continue
                choices = {name.casefold(), slot.casefold(), slot.removesuffix(" Upgrade").casefold()}
                if key in choices:
                    selected = slot, time_name, serial, name
                    break
            if selected is None:
                raise UpgradeRejected(f"No active upgrade matches {item}. Completed upgrades cannot be cancelled.")
            slot, time_name, serial, name = selected
            currency_field, cost_field = self._cost_fields(slot)
            resource_code = village[currency_field]
            paid_cost = village[cost_field]
            warnings = []
            if resource_code == 4:
                if paid_cost:
                    raise UpgradeRejected("The saved upgrade cost is inconsistent")
                paid = {}
            elif resource_code in RESOURCE_CODES.values():
                resource = next(name for name, code in RESOURCE_CODES.items() if code == resource_code)
                paid = {resource: paid_cost}
            elif resource_code == 0 and paid_cost == 0:
                price = self._price_for(self.fields_by_name[name], village[name] + 1)
                if price.choice_resources:
                    raise UpgradeRejected("This older upgrade has no saved payment currency, so its refund cannot be determined")
                paid = price.fixed_costs
                warnings.append("This upgrade predates payment tracking. Its refund uses the current upgrade cost.")
            else:
                raise UpgradeRejected("The saved upgrade currency is invalid")
            if self.resource_system.is_producer(name):
                self.resource_system.collect(village, now, automatic=True)
            refunds = {resource: amount // 2 for resource, amount in paid.items()}
            received = self.resource_system.deposit(village, refunds)
            for duplicate_slot, duplicate_time, duplicate_serial, finish_time in self._slot_values(village):
                if duplicate_serial == serial and finish_time:
                    self._clear_slot(village, duplicate_slot, duplicate_time)
            return CancelledUpgrade(name, slot, refunds, received, warnings)
