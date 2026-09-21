import csv
import re
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set, Tuple

from player_saves import FORMAT_DETAILS, SaveStore


RESOURCE_PRODUCTION_SECONDS = 3600
RESOURCE_TYPES = ("Gold", "Elixir", "Dark Elixir")
RESOURCE_CODES = {name: index for index, name in enumerate(RESOURCE_TYPES, 1)}
TREASURY_FIELDS = {name: f"Treasury {name}" for name in RESOURCE_TYPES}
LAST_RESOURCE_CHECK = "Last Resource Check"
INSTANCE_PATTERN = re.compile(r"^(.*) #\d+$")


class ResourceRejected(Exception):
    pass


@dataclass(frozen=True)
class ResourceStats:
    resource: str
    production: int
    capacity: int


@dataclass(frozen=True)
class CollectorStatus:
    item: str
    level: int
    resource: str
    hourly_rate: int
    stored: int
    capacity: int


@dataclass(frozen=True)
class ResourceReceipt:
    main: Dict[str, int]
    treasury: Dict[str, int]


class ResourceSystem:
    def __init__(self, progression_dir: Path, save_store: SaveStore, treasury_ratio=0.05):
        self.progression_dir = progression_dir
        self.save_store = save_store
        self.treasury_ratio = Fraction(str(treasury_ratio))
        if not 0 < self.treasury_ratio <= 1:
            raise ValueError("Clan Castle efficiency must be between zero and one")
        self.rows: Dict[Tuple[str, int], List[ResourceStats]] = {}
        self.producer_names: Set[str] = set()
        self.buildings: List[str] = []

    def load(self) -> None:
        self.save_store._require_schema()
        required_fields = [LAST_RESOURCE_CHECK, *RESOURCE_TYPES, *TREASURY_FIELDS.values()]
        for name in required_fields:
            if not self.save_store.has_village_field(name):
                raise ValueError(f"Missing resource save field: {name}")
        path = self.progression_dir / "resource.csv"
        rows = {}
        producers = set()
        with path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            required = {"Name", "Level", "Resource", "Production", "Capacity"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("resource.csv has invalid columns")
            for row in reader:
                name = row["Name"].strip()
                level = int(row["Level"])
                resource = row["Resource"].strip()
                if resource == "DE":
                    resource = "Dark Elixir"
                production = int(row["Production"].replace(",", ""))
                capacity = int(row["Capacity"].replace(",", ""))
                if not name or level <= 0 or min(production, capacity) < 0:
                    raise ValueError(f"Invalid resource data for {name} level {level}")
                if resource not in RESOURCE_TYPES:
                    raise ValueError(f"Unknown storage resource: {resource}")
                key = name, level
                entries = rows.setdefault(key, [])
                if any(entry.resource == resource for entry in entries):
                    raise ValueError(f"Duplicate resource data for {name} level {level}")
                entries.append(ResourceStats(resource, production, capacity))
                if production:
                    producers.add(name)
        self.rows = rows
        self.producer_names = producers
        known_names = {name for name, _ in rows}
        self.buildings = [
            field.name for field in self.save_store.fields
            if field.source == "village" and field.category == "Structure Level"
            and self.base_name(field.name) in known_names
        ]

    def base_name(self, name: str) -> str:
        match = INSTANCE_PATTERN.match(name)
        return match.group(1) if match else name

    def is_producer(self, item: str) -> bool:
        return self.base_name(item) in self.producer_names

    def _stats(self, item: str, level: int) -> List[ResourceStats]:
        if level <= 0:
            return []
        entries = self.rows.get((self.base_name(item), level))
        if entries is None:
            raise ResourceRejected(f"No resource data exists for {item} level {level}")
        return entries

    def _limit(self, name: str) -> int:
        field = self.save_store.village_by_name[name]
        return FORMAT_DETAILS[field.data_type][2]

    def capacities(self, village: Mapping[str, int]) -> Tuple[Dict[str, int], Dict[str, int]]:
        main = dict.fromkeys(RESOURCE_TYPES, 0)
        treasury = dict.fromkeys(RESOURCE_TYPES, 0)
        for item in self.buildings:
            base = self.base_name(item)
            if base == "Clan Castle":
                destination = treasury
            elif base == "Town Hall" or base.endswith(" Storage"):
                destination = main
            else:
                continue
            for stats in self._stats(item, village[item]):
                destination[stats.resource] += stats.capacity
        for resource in RESOURCE_TYPES:
            main[resource] = min(main[resource], self._limit(resource))
            treasury[resource] = min(treasury[resource], self._limit(TREASURY_FIELDS[resource]))
        return main, treasury

    def deposit(self, village: Dict[str, int], amounts: Mapping[str, int]) -> ResourceReceipt:
        main_caps, treasury_caps = self.capacities(village)
        main_gains = {}
        treasury_gains = {}
        for resource, amount in amounts.items():
            if resource not in RESOURCE_TYPES or amount < 0:
                raise ValueError(f"Invalid resource deposit: {resource}")
            amount = int(amount)
            main_gain = min(amount, max(0, main_caps[resource] - village[resource]))
            village[resource] += main_gain
            remaining = amount - main_gain
            converted = remaining * self.treasury_ratio.numerator // self.treasury_ratio.denominator
            treasury_field = TREASURY_FIELDS[resource]
            treasury_gain = min(converted, max(0, treasury_caps[resource] - village[treasury_field]))
            village[treasury_field] += treasury_gain
            main_gains[resource] = main_gain
            treasury_gains[resource] = treasury_gain
        return ResourceReceipt(main_gains, treasury_gains)

    def collector_status(
        self, village: Mapping[str, int], now: Optional[int] = None
    ) -> List[CollectorStatus]:
        now = int(time.time()) if now is None else int(now)
        last_check = village[LAST_RESOURCE_CHECK]
        elapsed = max(0, now - last_check) if last_check > 0 else 0
        result = []
        for item in self.buildings:
            if not self.is_producer(item):
                continue
            for stats in self._stats(item, village[item]):
                amount = min(stats.capacity, stats.production * elapsed // RESOURCE_PRODUCTION_SECONDS)
                result.append(CollectorStatus(item, village[item], stats.resource, stats.production, amount, stats.capacity))
        return result

    def collect(
        self, village: Dict[str, int], now: Optional[int] = None, *, automatic: bool = False
    ) -> ResourceReceipt:
        now = int(time.time()) if now is None else int(now)
        now = max(now, village[LAST_RESOURCE_CHECK])
        if now > self._limit(LAST_RESOURCE_CHECK):
            raise ResourceRejected("The current time exceeds the Last Resource Check data limit")
        statuses = self.collector_status(village, now)
        amounts = {}
        for status in statuses:
            amounts[status.resource] = amounts.get(status.resource, 0) + status.stored
        if not automatic:
            main_caps, treasury_caps = self.capacities(village)
            full = [
                resource for resource in RESOURCE_TYPES
                if (main_caps[resource] or treasury_caps[resource] or resource in amounts)
                and village[resource] >= main_caps[resource]
                and village[TREASURY_FIELDS[resource]] >= treasury_caps[resource]
            ]
            if full:
                raise ResourceRejected(
                    "Collection blocked because these storages and treasuries are full: "
                    + ", ".join(full)
                )
        receipt = self.deposit(village, amounts)
        village[LAST_RESOURCE_CHECK] = now
        for resource in amounts:
            total_name = f"Total {resource}"
            if total_name in village:
                village[total_name] += receipt.main[resource] + receipt.treasury[resource]
        return receipt

    def collect_treasury(self, village: Dict[str, int]) -> Dict[str, int]:
        main_caps, _ = self.capacities(village)
        collected = {}
        for resource in RESOURCE_TYPES:
            treasury_field = TREASURY_FIELDS[resource]
            amount = min(village[treasury_field], max(0, main_caps[resource] - village[resource]))
            village[resource] += amount
            village[treasury_field] -= amount
            collected[resource] = amount
        return collected
