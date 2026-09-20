import csv
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from player_saves import FORMAT_DETAILS, SaveField, SaveStore


BUILDER_CATEGORIES = {"Structure Level", "Hero Level"}
RESEARCHER_CATEGORIES = {
    "Troop Level",
    "Spell Level",
    "Siege Level",
    "Pet Level",
}
EQUIPMENT_CATEGORY = "Equipment Level"
TABLE_NAME_ALIASES = {"Electric Owl": "Electro Owl"}
TOWN_HALL_WEAPONS = {
    12: "Town Hall 12 Weapon",
    13: "Town Hall 13 Weapon",
    14: "Town Hall 14 Weapon",
    15: "Town Hall 15 Weapon",
    17: "Town Hall 17 Weapon",
}
TOWN_HALL_COST_ROWS = {
    12: "TH12 Giga Tesla 1",
    13: "TH13 Giga Inferno 1",
    14: "TH14 Giga Inferno 1",
    15: "TH15 Giga Inferno 1",
    16: "TH16 Giga Inferno 1",
    17: "TH17 Inferno Artillery 1",
}
WEAPON_COST_ALIASES = {
    "TH12 Giga Tesla": "Town Hall 12 Weapon",
    "TH13 Giga Inferno": "Town Hall 13 Weapon",
    "TH14 Giga Inferno": "Town Hall 14 Weapon",
    "TH15 Giga Inferno": "Town Hall 15 Weapon",
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


class UpgradeSystem:
    def __init__(
        self,
        progression_dir: Path,
        save_store: SaveStore,
        common_equipment_max: int,
        epic_equipment_max: int,
    ):
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
            if "level" in field.category.casefold()
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
        if resource == "DE":
            return UpgradePrice(duration, {"Dark Elixir": amount})
        if resource == "Both":
            return UpgradePrice(duration, {}, ("Gold", "Elixir"), amount)
        if resource not in {"Gold", "Elixir"}:
            raise ValueError(f"Unknown upgrade resource: {resource}")
        return UpgradePrice(duration, {resource: amount})

    def _load_normal_prices(self) -> Dict[Tuple[str, int], UpgradePrice]:
        rows = self._read_csv_rows("upgrades.csv")
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
            prices[key] = self._normal_price(row)

        for target_level, row_name in TOWN_HALL_COST_ROWS.items():
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
            if serial <= 0 or serial > 32767:
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
            upgrade_field = self.save_store.village_by_name[upgrade_name]
            time_field = self.save_store.village_by_name[time_name]
            if FORMAT_DETAILS[upgrade_field.data_type][2] < maximum_serial:
                raise ValueError(
                    f"{upgrade_name} cannot store upgrade serial {maximum_serial}"
                )
            if FORMAT_DETAILS[time_field.data_type][2] < int(time.time()):
                raise ValueError(f"{time_name} cannot store a current UNIX timestamp")

    def resolve_item(self, item: str) -> SaveField:
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

    def _deductions(
        self,
        price: UpgradePrice,
        village: Mapping[str, int],
    ) -> Dict[str, int]:
        missing: List[str] = []
        deductions: Dict[str, int] = {}
        for resource, amount in price.fixed_costs.items():
            available = village.get(resource, 0)
            if available < amount:
                missing.append(f"{resource}: need {amount:,}, have {available:,}")
            elif amount:
                deductions[resource] = amount

        if price.choice_resources and price.choice_cost:
            selected = next(
                (
                    resource
                    for resource in price.choice_resources
                    if village.get(resource, 0) >= price.choice_cost
                ),
                None,
            )
            if selected is None:
                balances = ", ".join(
                    f"{resource} {village.get(resource, 0):,}"
                    for resource in price.choice_resources
                )
                missing.append(
                    f"need {price.choice_cost:,} of one of "
                    f"{', '.join(price.choice_resources)}; have {balances}"
                )
            else:
                deductions[selected] = price.choice_cost

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
        if field.category in BUILDER_CATEGORIES:
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
            serial = village[upgrade_name]
            finish_time = village[time_name]
            if serial == 0 and finish_time == 0:
                return upgrade_name, time_name
            if (serial == 0) != (finish_time == 0):
                raise UpgradeRejected(f"Upgrade slot {upgrade_name} contains inconsistent data")
        raise UpgradeRejected(f"No {slot_type} upgrade slot is available")

    def _pending_serials(self, village: Mapping[str, int]) -> Dict[int, str]:
        pending: Dict[int, str] = {}
        for upgrade_name, _, serial, _ in self._slot_values(village):
            if serial:
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
        blockers: List[str] = []
        if any(serial for _, _, serial, _ in self._slot_values(village)):
            blockers.append("an upgrade is still in progress")

        for field in self.level_fields:
            if field.name == "Town Hall":
                continue
            try:
                maximum = self._availability(
                    field,
                    village,
                    collection,
                    reject_at_maximum=False,
                )
            except UpgradeRejected as error:
                if "not unlocked" in str(error):
                    continue
                raise
            effective_maximum = self._supported_maximum(field, maximum)
            current_level = village[field.name]
            if current_level < effective_maximum:
                blockers.append(
                    f"{field.name} is level {current_level}/{effective_maximum}"
                )
        return blockers

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

    def refresh(self, user_id: int, now: Optional[int] = None) -> RefreshReport:
        now = int(time.time()) if now is None else int(now)
        village, _ = self.save_store.player_values(user_id)
        set_values: Dict[str, int] = {}
        completed: List[CompletedUpgrade] = []
        active: List[ActiveUpgrade] = []
        warnings: List[str] = []
        completed_serials: set[int] = set()

        for upgrade_name, time_name, serial, finish_time in self._slot_values(village):
            if serial == 0 and finish_time == 0:
                continue
            if serial == 0 or finish_time == 0:
                warnings.append(f"{upgrade_name} contains inconsistent data")
                continue
            item = self.name_by_serial.get(serial)
            if item is None:
                warnings.append(f"{upgrade_name} contains unknown serial {serial}")
                continue
            field = self.fields_by_name.get(item)
            if field is None:
                warnings.append(
                    f"{upgrade_name} referred to removed item {item} and was cleared"
                )
                set_values[upgrade_name] = 0
                set_values[time_name] = 0
                continue
            if finish_time > now:
                active.append(ActiveUpgrade(item, finish_time, upgrade_name))
                continue

            if serial not in completed_serials:
                current_level = set_values.get(item, village[item])
                new_level = current_level + 1
                set_values[item] = new_level
                set_values.update(
                    self._completion_side_effects(item, new_level, village)
                )
                completed.append(CompletedUpgrade(item, new_level, upgrade_name))
                completed_serials.add(serial)
            else:
                warnings.append(f"Duplicate completed upgrade for {item} was cleared")
            set_values[upgrade_name] = 0
            set_values[time_name] = 0

        if set_values:
            self.save_store.update_village_values(user_id, set_values=set_values)
        return RefreshReport(completed, active, warnings)

    def start_upgrade(
        self,
        user_id: int,
        item: str,
        now: Optional[int] = None,
    ) -> Tuple[UpgradeOutcome, RefreshReport]:
        now = int(time.time()) if now is None else int(now)
        refresh_report = self.refresh(user_id, now)
        village, collection = self.save_store.player_values(user_id)
        field = self.resolve_item(item)
        current_level = village[field.name]
        maximum = self._availability(field, village, collection)
        target_level = current_level + 1

        serial = self.serial_for(field)
        pending = self._pending_serials(village)
        if serial in pending:
            raise UpgradeRejected(
                f"{field.name} is already upgrading in {pending[serial]}"
            )

        if field.name == "Town Hall" and current_level > 0:
            blockers = self._town_hall_blockers(village, collection)
            if blockers:
                shown = "; ".join(blockers[:5])
                remaining = len(blockers) - min(len(blockers), 5)
                suffix = f"; and {remaining} more" if remaining else ""
                raise UpgradeRejected(
                    "Town Hall cannot be upgraded until every other possible "
                    f"upgrade is complete: {shown}{suffix}"
                )

        if target_level > maximum:
            raise UpgradeRejected(
                f"{field.name} cannot exceed its current maximum level of {maximum}"
            )
        price = self._price_for(field, target_level)
        deductions = self._deductions(price, village)
        additions = {resource: -amount for resource, amount in deductions.items()}
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

        self.save_store.update_village_values(
            user_id,
            set_values=set_values,
            additions=additions,
        )
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
