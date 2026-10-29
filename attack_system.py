import csv
import json
import math
import os
import random
import re
import tempfile
import time
from pathlib import Path

from resource_system import COLLECTIBLE_RESOURCES, ORE_TYPES


ATTACK_DURATION = 180
ATTACK_CHESTS_PER_SECOND = 0.1
ATTACK_REQUIRED_CHESTS = math.floor(ATTACK_DURATION * ATTACK_CHESTS_PER_SECOND)
ATTACK_TYPES = ("Ground", "Air")
ATTACK_SIDES = ("Offense", "Defense")
ATTACK_POLL_SECONDS = 2
ATTACK_HISTORY_SIZE = 10
TOWN_HALL_CHANCE_EQUAL = 0.95
TOWN_HALL_CHANCE_MINIMUM = 0.45
EXPECTED_PERCENTAGE_EQUAL = 110
EXPECTED_PERCENTAGE_MINIMUM = 60
PERCENTAGE_STANDARD_DEVIATION = 20
MINIMUM_PERCENTAGE = 1
SURVIVING_TOWN_HALL_MAXIMUM = 99
DESTROYED_TOWN_HALL_MAXIMUM = 100
PERCENTAGE_STAR_THRESHOLD = 50
DIFFICULTY_REWARD_MINIMUM = 0.8
DIFFICULTY_REWARD_MAXIMUM = 1.0
RANDOM_REWARD_MINIMUM = 0.9
RANDOM_REWARD_MAXIMUM = 1.1
REWARD_MULTIPLIERS = {
    0: (1, 49, (0.0, 0.8), (0.0, 0.3)),
    1: (1, 99, (1.0, 2.2), (0.4, 0.8)),
    2: (50, 99, (2.5, 4.0), (1.0, 1.8)),
    3: (100, 100, (5.0, 5.0), (2.0, 2.0)),
}
TABLE_ALIASES = {"Electric Owl": "Electro Owl"}
ATTACK_END_FIELD = "Attack End Timestamp"
ATTACK_CHESTS_FIELD = "Chests Opened While Attacking"
BENCHMARK_COLUMNS = ["Town Hall"] + [
    f"{bound} {side} {kind}"
    for bound in ("Min", "Max")
    for side in ATTACK_SIDES
    for kind in ATTACK_TYPES
]


class AttackRejected(Exception):
    pass


class AttackSystem:
    def __init__(self, progression_dir, store, resources):
        self.progression_dir = Path(progression_dir)
        self.store = store
        self.resources = resources
        self.units = []
        self.bounds = {}
        self.benchmarks = {}
        self.rewards = {}
        self.ready = False

    def _read_rows(self, filename):
        with (self.progression_dir / filename).open(newline="", encoding="utf-8-sig") as source:
            return [{key: value.strip() for key, value in row.items()} for row in csv.DictReader(source)]

    def load(self, *, generate_benchmarks=False):
        self.ready = False
        self.store._require_schema()
        required = [ATTACK_END_FIELD, ATTACK_CHESTS_FIELD] + [
            f"Attack #{index} {part}"
            for index in range(1, ATTACK_HISTORY_SIZE + 1)
            for part in ("Type", "Stars", "Percentage", "Timestamp")
        ]
        if any(name not in self.store.village_by_name for name in required):
            raise ValueError("Attack fields are missing from village.csv")
        counts = {row["Name"]: row for row in self._read_rows("counts.csv")}
        levels = {row["Name"]: row for row in self._read_rows("levels.csv")}
        self.units = []
        seen = set()
        for row in self._read_rows("attacking.csv"):
            name, side, kind = row["Name"], row["Side"], row["Category"]
            weight = float(row["Weight"])
            if side not in ATTACK_SIDES or kind not in (*ATTACK_TYPES, "Both") or not math.isfinite(weight) or weight < 0:
                raise ValueError(f"Invalid attack rule for {name}")
            names = [field.name for field in self.store.village_by_name.values() if (
                re.sub(r" #\d+$", "", field.name) == name
                or re.fullmatch(re.escape(name) + "[ABC]", field.name)
            ) and ("Level" in field.category or field.category == "Pet")]
            if not names:
                raise ValueError(f"No village units match attacking.csv row {name}")
            if any(re.fullmatch(re.escape(name) + r" #\d+", field_name) for field_name in names):
                count_row = counts.get(name) or counts.get(TABLE_ALIASES.get(name, name))
                if count_row is None:
                    raise ValueError(f"Missing attack count data for {name}")
                maximum_count = max(int(value or 0) for key, value in count_row.items() if key.isdigit())
                names = sorted(set(names) | {f"{name} #{index}" for index in range(1, maximum_count + 1)})
            for field_name in names:
                if (side, field_name) in seen:
                    raise ValueError(f"Duplicate attack rule for {side} {field_name}")
                seen.add((side, field_name))
                self.units.append((field_name, side, kind, weight))

        maximums = {0: {name: 0 for name, _, _, _ in self.units}}
        for town_hall in range(1, 19):
            cache = {"Town Hall": town_hall}

            def maximum(name, visiting=()):
                if name in cache:
                    return cache[name]
                if name in visiting:
                    raise ValueError(f"Circular progression requirement for {name}")
                base = re.sub(r" #\d+$", "", name)
                count_row = counts.get(base) or counts.get(TABLE_ALIASES.get(base, base))
                level_row = levels.get(base) or levels.get(TABLE_ALIASES.get(base, base))
                if count_row is None or level_row is None:
                    raise ValueError(f"Missing attack progression data for {name}")
                count_requirement = maximum(count_row["Requirement"], (*visiting, name))
                level_requirement = maximum(level_row["Requirement"], (*visiting, name))
                count = int(count_row.get(str(count_requirement), "0") or 0)
                instance = re.search(r" #(\d+)$", name)
                value = int(level_row.get(str(level_requirement), "0") or 0)
                cache[name] = value if count >= (int(instance[1]) if instance else 1) else 0
                return cache[name]

            maximums[town_hall] = {name: maximum(name) for name, _, _, _ in self.units}
        self.bounds = {
            th: {name: (maximums[th - 1][name], maximums[th][name]) for name, _, _, _ in self.units}
            for th in range(1, 19)
        }
        for th, units in self.bounds.items():
            if any(low > high for low, high in units.values()):
                raise ValueError(f"Attack levels decrease at Town Hall {th}")
        generated = self.benchmark_rows()
        self.benchmarks = {}
        for row in (generated if generate_benchmarks else self._read_rows("attacking_benchmarks.csv")):
            th = int(row["Town Hall"])
            if th in self.benchmarks:
                raise ValueError(f"Duplicate Town Hall {th} attack benchmark")
            self.benchmarks[th] = {key: float(row[key]) for key in BENCHMARK_COLUMNS[1:]}
        for row in generated:
            th = row["Town Hall"]
            if th not in self.benchmarks or any(not math.isclose(self.benchmarks[th][key], row[key], abs_tol=1e-8) for key in BENCHMARK_COLUMNS[1:]):
                raise ValueError(f"Regenerate attacking_benchmarks.csv for Town Hall {th}")
        self.rewards = {}
        for row in self._read_rows("attacking_rewards.csv"):
            th = int(row["Town Hall"])
            ranges = {}
            for resource in COLLECTIBLE_RESOURCES:
                value = row.get(resource, "")
                if not value:
                    continue
                parts = value.split("-")
                low, high = float(parts[0]), float(parts[-1])
                if len(parts) > 2 or not all(math.isfinite(n) for n in (low, high)) or not 0 <= low <= high:
                    raise ValueError(f"Invalid attack reward for {resource} at Town Hall {th}")
                ranges[resource] = (low, high)
            if not ranges or th in self.rewards:
                raise ValueError(f"Invalid attack reward row for Town Hall {th}")
            self.rewards[th] = ranges
        if set(self.rewards) != set(self.bounds):
            raise ValueError("Attack rewards must cover Town Halls one through eighteen")
        self.ready = True

    def score(self, values, side, kind):
        return math.fsum(values.get(name, 0) * weight for name, unit_side, unit_kind, weight in self.units if unit_side == side and unit_kind in (kind, "Both"))

    def benchmark_rows(self):
        rows = []
        for th, bounds in self.bounds.items():
            row = {"Town Hall": th}
            for index, bound in enumerate(("Min", "Max")):
                values = {name: pair[index] for name, pair in bounds.items()}
                for side in ATTACK_SIDES:
                    for kind in ATTACK_TYPES:
                        row[f"{bound} {side} {kind}"] = self.score(values, side, kind)
            rows.append(row)
        return rows

    def scaled_score(self, th, score, side, kind):
        row = self.benchmarks[th]
        low, high = row[f"Min {side} {kind}"], row[f"Max {side} {kind}"]
        return min(1.0, max(0.0, (score - low) / (high - low))) if high > low else 0.0

    def _path(self, user_id):
        return self.store.player_path(user_id).with_suffix(".attack.json")

    def _write(self, user_id, state):
        path = self._path(user_id)
        descriptor, name = tempfile.mkstemp(prefix=".attack-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                json.dump(state, destination, separators=(",", ":"), allow_nan=False)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _read(self, user_id):
        try:
            return json.loads(self._path(user_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def pending_users(self):
        return [int(path.name.split(".")[0]) for path in self.store.saves_dir.glob("*.attack.json") if path.name.split(".")[0].isdigit()]

    def _require_player(self, user_id):
        if not self.ready:
            raise AttackRejected("Attack data is unavailable. Please ask a moderator to check the bot setup.")
        if not self.store.player_path(user_id).is_file():
            raise AttackRejected("New to Chester? Open a Chest using the /chest command to get started!")

    def start(self, user_id, now=None):
        now = int(time.time()) if now is None else int(now)
        with self.store.lock:
            self._require_player(user_id)
            existing = self.status(user_id, now)
            if existing is not None:
                return existing
            with self.store.transaction(user_id) as (village, _):
                if village[ATTACK_END_FIELD]:
                    raise AttackRejected("Your previous attack data is unavailable. Please contact the bot owner.")
                th = village["Town Hall"]
                if th not in self.bounds:
                    raise AttackRejected("Open a Chest to create your village before attacking.")
                kind = random.choice(ATTACK_TYPES)
                defense = {name: random.randint(*self.bounds[th][name]) for name, side, _, _ in self.units if side == "Defense"}
                difficulty = self.scaled_score(th, self.score(defense, "Defense", kind), "Defense", kind)
                offense = self.scaled_score(th, self.score(village, "Offense", kind), "Offense", kind)
                state = {"user_id": int(user_id), "end": now + ATTACK_DURATION, "town_hall": th,
                         "type": kind, "difficulty": difficulty, "offense": offense,
                         "seed": random.getrandbits(128), "status": "active", "notified": False,
                         "reward_ranges": self.rewards[th], "chests": 0}
                self._write(user_id, state)
                village[ATTACK_CHESTS_FIELD] = 0
                village[ATTACK_END_FIELD] = state["end"]
            return state

    def count_chest(self, village, now=None):
        now = int(time.time()) if now is None else int(now)
        end = village.get(ATTACK_END_FIELD, 0)
        if end - ATTACK_DURATION <= now < end:
            field = self.store.village_by_name[ATTACK_CHESTS_FIELD]
            village[field.name] = self.store._bounded_add(village[field.name], 1, field.data_type)

    def simulate(self, town_hall, offense=None, defense=None):
        if not self.ready:
            raise AttackRejected("Attack data is unavailable. Please ask a moderator to check the bot setup.")
        if isinstance(town_hall, bool) or not isinstance(town_hall, int) or town_hall not in self.bounds:
            raise AttackRejected("Choose a Town Hall level from 1 through 18.")
        overrides = {"Offense": offense, "Defense": defense}
        for side, value in overrides.items():
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1):
                raise AttackRejected(f"{side} score must be between 0 and 1.")
        kind = random.choice(ATTACK_TYPES)
        scores = {}
        for side in ATTACK_SIDES:
            if overrides[side] is not None:
                scores[side] = float(overrides[side])
                continue
            values = {name: random.randint(*self.bounds[town_hall][name]) for name, unit_side, _, _ in self.units if unit_side == side}
            scores[side] = self.scaled_score(town_hall, self.score(values, side, kind), side, kind)
        state = {"town_hall": town_hall, "type": kind, "offense": scores["Offense"],
                 "difficulty": scores["Defense"], "seed": random.getrandbits(128), "reward_ranges": self.rewards[town_hall]}
        return self._result(state)

    @staticmethod
    def expectations(offset):
        chance = TOWN_HALL_CHANCE_EQUAL + offset * (TOWN_HALL_CHANCE_EQUAL - TOWN_HALL_CHANCE_MINIMUM)
        percentage = EXPECTED_PERCENTAGE_EQUAL + offset * (EXPECTED_PERCENTAGE_EQUAL - EXPECTED_PERCENTAGE_MINIMUM)
        return min(1.0, max(0.0, chance)), percentage

    @staticmethod
    def reward_multiplier(stars, percentage, ore):
        low, high, loot, ores = REWARD_MULTIPLIERS[stars]
        start, end = ores if ore else loot
        fraction = min(1.0, max(0.0, (percentage - low) / (high - low))) if high > low else 0.0
        return start + (end - start) * fraction

    def _result(self, state):
        rng = random.Random(state["seed"])
        chance, expected = self.expectations(state["offense"] - state["difficulty"])
        destroyed = rng.random() < chance
        ceiling = DESTROYED_TOWN_HALL_MAXIMUM if destroyed else SURVIVING_TOWN_HALL_MAXIMUM
        percentage = max(MINIMUM_PERCENTAGE, min(ceiling, math.floor(rng.gauss(expected, PERCENTAGE_STANDARD_DEVIATION))))
        stars = int(destroyed) + int(percentage >= PERCENTAGE_STAR_THRESHOLD) + int(destroyed and percentage >= DESTROYED_TOWN_HALL_MAXIMUM)
        difficulty_factor = DIFFICULTY_REWARD_MINIMUM + (DIFFICULTY_REWARD_MAXIMUM - DIFFICULTY_REWARD_MINIMUM) * state["difficulty"]
        rewards = {}
        for resource, (low, high) in state["reward_ranges"].items():
            amount = rng.uniform(low, high) * self.reward_multiplier(stars, percentage, resource in ORE_TYPES)
            resource_multiplier = rng.uniform(RANDOM_REWARD_MINIMUM, RANDOM_REWARD_MAXIMUM)
            amount *= difficulty_factor * resource_multiplier
            rewards[resource] = math.floor(amount + 0.5)
        return {**state, "status": "complete", "destroyed": destroyed, "percentage": percentage, "stars": stars,
                "rewards": rewards, "chance": chance, "expected_percentage": expected}

    def _history(self, village, state):
        if village["Attack #1 Timestamp"] == state["end"]:
            return
        for index in range(ATTACK_HISTORY_SIZE, 1, -1):
            for part in ("Type", "Stars", "Percentage", "Timestamp"):
                village[f"Attack #{index} {part}"] = village[f"Attack #{index - 1} {part}"]
        village["Attack #1 Type"] = int(state["type"] == "Air")
        village["Attack #1 Stars"] = state.get("stars", 0)
        village["Attack #1 Percentage"] = state.get("percentage", 0)
        village["Attack #1 Timestamp"] = state["end"]

    def status(self, user_id, now=None):
        now = int(time.time()) if now is None else int(now)
        with self.store.lock:
            state = self._read(user_id)
            if state is None:
                return None
            if not self.store.player_path(user_id).is_file():
                self._path(user_id).unlink(missing_ok=True)
                return None
            self._require_player(user_id)
            completed_now = False
            with self.store.transaction(user_id) as (village, _):
                if village[ATTACK_END_FIELD] != state["end"]:
                    self._path(user_id).unlink(missing_ok=True)
                    return None
                state["chests"] = village[ATTACK_CHESTS_FIELD]
                if state["status"] == "active" and now >= state["end"]:
                    if state["chests"] < ATTACK_REQUIRED_CHESTS:
                        state["status"] = "cancelled"
                        village[ATTACK_END_FIELD] = 0
                        village[ATTACK_CHESTS_FIELD] = 0
                    else:
                        state = self._result(state)
                        completed_now = True
                    self._history(village, state)
            if state["status"] == "cancelled":
                self._path(user_id).unlink(missing_ok=True)
            elif completed_now:
                self._write(user_id, state)
            return state

    def attach_message(self, user_id, end, channel_id, message_id):
        with self.store.lock:
            state = self._read(user_id)
            if state is not None and state["end"] == end:
                state.update(channel_id=int(channel_id), message_id=int(message_id), notified=state["status"] == "complete")
                self._write(user_id, state)

    def mark_notified(self, user_id, end):
        with self.store.lock:
            state = self._read(user_id)
            if state is not None and state["end"] == end:
                state["notified"] = True
                self._write(user_id, state)

    def claim(self, user_id, end, resource):
        with self.store.lock:
            self._require_player(user_id)
            state = self.status(user_id)
            if state is None or state["end"] != end:
                raise AttackRejected("This attack reward has already been claimed or is no longer available.")
            if state["status"] != "complete" or resource not in state["rewards"]:
                raise AttackRejected("Choose one of the rewards from your completed attack.")
            with self.store.transaction(user_id) as (village, _):
                receipt = self.resources.deposit(village, {resource: state["rewards"][resource]})
                total = f"Total {resource}"
                if total in village:
                    field = self.store.village_by_name[total]
                    village[total] = self.store._bounded_add(village[total], receipt.main[resource] + receipt.treasury[resource], field.data_type)
                village[ATTACK_END_FIELD] = 0
                village[ATTACK_CHESTS_FIELD] = 0
                caps, treasury_caps = self.resources.capacities(village)
            self._path(user_id).unlink(missing_ok=True)
            return state, receipt, village, caps, treasury_caps
