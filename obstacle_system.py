import csv
import math
import random
import time
from dataclasses import dataclass

from player_saves import FORMAT_DETAILS


OBSTACLE_INTERVAL_SECONDS = 28800
INITIAL_OBSTACLES = 40
LAST_OBSTACLE_CHECK = "Last Obstacle Check"


class ObstacleRejected(ValueError):
    pass


@dataclass(frozen=True)
class Obstacle:
    name: str
    cost: int
    resource: str
    weight: float
    reward: str


class ObstacleSystem:
    def __init__(self, data_dir, store, rng=None):
        self.data_dir = data_dir
        self.store = store
        self.rng = rng if rng is not None else random.SystemRandom()
        self.items = {}

    def load(self):
        items = {}
        with (self.data_dir / "obstacles.csv").open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            required = {"Name", "Cost", "Resource", "Weight", "Reward"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("Missing obstacle columns")
            for row in reader:
                item = Obstacle(row["Name"].strip(), int(row["Cost"]), row["Resource"].strip(), float(row["Weight"]), row["Reward"].strip())
                if not item.name or item.name.casefold() in {name.casefold() for name in items}:
                    raise ValueError("Blank or duplicate obstacle name")
                if item.cost < 0 or not math.isfinite(item.weight) or item.weight < 0:
                    raise ValueError(f"Invalid obstacle cost or weight: {item.name}")
                if not item.reward or any(digit not in "0123456789" for digit in item.reward):
                    raise ValueError(f"Invalid obstacle reward: {item.name}")
                field = self.store.village_by_name.get(item.name)
                if field is None or field.category != "Obstacle" or field.data_type != "byte":
                    raise ValueError(f"Missing obstacle byte save field: {item.name}")
                if item.resource not in self.store.village_by_name:
                    raise ValueError(f"Unknown obstacle resource: {item.resource}")
                items[item.name] = item
        if not items or not any(item.weight > 0 for item in items.values()):
            raise ValueError("Obstacles need a positive total weight")
        field = self.store.village_by_name.get(LAST_OBSTACLE_CHECK)
        if field is None or field.data_type != "integer":
            raise ValueError("Missing Last Obstacle Check integer save field")
        self.items = items

    def _now(self, now):
        now = int(time.time()) if now is None else int(now)
        if not 0 < now <= FORMAT_DETAILS["integer"][2]:
            raise ObstacleRejected("The current timestamp cannot be stored")
        return now

    def _spawn(self, village, count):
        names = list(self.items)
        weights = [self.items[name].weight for name in names]
        added = {}
        for _ in range(count):
            name = self.rng.choices(names, weights=weights, k=1)[0]
            if village[name] < FORMAT_DETAILS["byte"][2]:
                village[name] += 1
                added[name] = added.get(name, 0) + 1
        return added

    def initialize(self, user_id, now=None):
        if not self.items:
            raise ObstacleRejected("Obstacle data is unavailable")
        now = self._now(now)
        with self.store.transaction(user_id) as (village, collection):
            if village[LAST_OBSTACLE_CHECK] != 0:
                return {}
            added = self._spawn(village, INITIAL_OBSTACLES)
            village[LAST_OBSTACLE_CHECK] = now
            return added

    def check(self, user_id, now=None):
        if not self.items:
            raise ObstacleRejected("Obstacle data is unavailable")
        now = self._now(now)
        with self.store.transaction(user_id) as (village, collection):
            previous = village[LAST_OBSTACLE_CHECK]
            if previous == 0:
                count = INITIAL_OBSTACLES
            else:
                elapsed = max(0, now - previous)
                count, remainder = divmod(elapsed, OBSTACLE_INTERVAL_SECONDS)
                count += int(self.rng.random() < remainder / OBSTACLE_INTERVAL_SECONDS)
            added = self._spawn(village, count)
            village[LAST_OBSTACLE_CHECK] = max(now, previous)
            total = sum(village[name] for name in self.items)
            return added, count - sum(added.values()), total

    def resolve(self, name):
        for item in self.items.values():
            if item.name.casefold() == name.strip().casefold():
                return item
        raise ObstacleRejected("Choose a valid obstacle type")

    def maximum(self, village, item):
        owned = village[item.name]
        return min(owned, village[item.resource] // item.cost) if item.cost else owned

    def remove(self, user_id, name, amount):
        item = self.resolve(name)
        with self.store.transaction(user_id) as (village, collection):
            maximum = self.maximum(village, item)
            if not isinstance(amount, int) or isinstance(amount, bool) or not 1 <= amount <= maximum:
                raise ObstacleRejected(f"You can remove between 1 and {maximum:,} {item.name} right now, based on your obstacles and {item.resource}.")
            reward = sum(int(self.rng.choice(item.reward)) for _ in range(amount))
            village[item.resource] -= item.cost * amount
            village[item.name] -= amount
            before = village["Gems"]
            field = self.store.village_by_name["Gems"]
            village["Gems"] = self.store._bounded_add(before, reward, field.data_type)
            credited = village["Gems"] - before
            if "Total Gems" in village:
                field = self.store.village_by_name["Total Gems"]
                village["Total Gems"] = self.store._bounded_add(village["Total Gems"], credited, field.data_type)
            return item, item.cost * amount, credited
