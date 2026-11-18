import csv
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Dict, Iterable, List, Mapping, Optional, Tuple


FORMAT_DETAILS = {
    "boolean": ("?", 0, 1),
    "byte": ("b", 0, 127),
    "short": ("h", 0, 32767),
    "integer": ("i", 0, 2147483647),
    "long": ("q", 0, 9223372036854775807),
}
SAVE_MAGIC = b"CHSV"
SAVE_VERSION = 2
SAVE_HEADER = struct.Struct("<4sB32s")
SERIAL_SAVE_HEADER = struct.Struct("<4sB32s32s")
EMPTY_SERIAL_DIGEST = bytes(32)
SAVE_FIELD_RENAMES = {
    "Hide tutorial": "Hide Tutorial",
    "Obstacles cleared": "Obstacles Cleared",
    "Power Pancakes Attacks Remaining": "Power Pancakes Expiration",
    "Mighty Morsel Attacks Remaining": "Mighty Morsel Expiration",
    "Clan Castle Cake End Timestamp": "Clan Castle Cake Expiration",
    "Power Potion End Timestamp": "Power Potion Expiration",
    "Hero Potion End Timestamp": "Hero Potion Expiration",
}


class SaveError(Exception):
    pass


class SaveSchemaMismatch(SaveError):
    pass


@dataclass(frozen=True)
class SaveField:
    source: str
    name: str
    category: str
    data_type: str
    index: int

    @property
    def identity(self) -> Tuple[str, str]:
        return self.source, self.name


@dataclass(frozen=True)
class MigrationReport:
    total_saves: int
    migrated_saves: int
    current_saves: int
    added_fields: int
    removed_fields: int
    changed_types: int
    moved_fields: int
    changed_categories: int
    backup_path: Optional[Path]
    cancelled_upgrades: int = 0


class SaveStore:
    def __init__(self, progression_dir: Path, saves_dir: Path):
        self.progression_dir = progression_dir
        self.saves_dir = saves_dir
        self.fields: List[SaveField] = []
        self.village_by_name: Dict[str, SaveField] = {}
        self.collection_by_name: Dict[str, SaveField] = {}
        self.categories: List[str] = []
        self.categories_by_key: Dict[str, str] = {}
        self.fields_by_category: Dict[str, List[SaveField]] = {}
        self.binary_format = "<"
        self.record_size = 0
        self.schema_digest = b""
        self.serial_digest = EMPTY_SERIAL_DIGEST
        self.serial_by_name = {}
        self.lock = RLock()

    def load_schema(
        self,
        village_path: Optional[Path] = None,
        collection_path: Optional[Path] = None,
    ) -> None:
        village_path = village_path or self.progression_dir / "village.csv"
        collection_path = collection_path or self.progression_dir / "collection.csv"
        fields: List[SaveField] = []
        self._load_fields(
            village_path,
            "village",
            "Name",
            "Category",
            "Data Format",
            fields,
        )
        self._load_fields(
            collection_path,
            "collection",
            "Name",
            "Level",
            "Data Type",
            fields,
        )

        village_by_name: Dict[str, SaveField] = {}
        collection_by_name: Dict[str, SaveField] = {}
        categories: List[str] = []
        categories_by_key: Dict[str, str] = {}
        fields_by_category: Dict[str, List[SaveField]] = {}
        format_codes: List[str] = []

        for field in fields:
            lookup = village_by_name if field.source == "village" else collection_by_name
            if field.name in lookup:
                raise ValueError(f"Duplicate {field.source} save field: {field.name}")
            lookup[field.name] = field
            format_codes.append(FORMAT_DETAILS[field.data_type][0])

            key = field.category.casefold()
            if key not in categories_by_key:
                categories_by_key[key] = field.category
                categories.append(field.category)
                fields_by_category[field.category] = []
            fields_by_category[categories_by_key[key]].append(field)

        schema_text = "\n".join(
            "\0".join((field.source, field.name, field.category, field.data_type))
            for field in fields
        )
        self.fields = fields
        self.village_by_name = village_by_name
        self.collection_by_name = collection_by_name
        self.categories = categories
        self.categories_by_key = categories_by_key
        self.fields_by_category = fields_by_category
        self.binary_format = "<" + "".join(format_codes)
        self.record_size = struct.calcsize(self.binary_format)
        self.schema_digest = hashlib.sha256(schema_text.encode("utf-8")).digest()
        self.load_serial_mapping()
        self.saves_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def read_serial_mapping(path: Path):
        mapping = {}
        serials = set()
        with path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            if not reader.fieldnames or not {"Serial", "Name", "Category"}.issubset(reader.fieldnames):
                raise SaveError(f"Invalid upgrade serial columns in {path.name}")
            for row in reader:
                name = row["Name"].strip()
                serial = int(row["Serial"])
                if not name or name in mapping or serial in serials or not 0 <= serial <= 32767:
                    raise SaveError(f"Invalid or duplicate upgrade serial in {path.name}: {name}")
                mapping[name] = serial
                serials.add(serial)
        if not mapping:
            raise SaveError(f"Empty upgrade serial mapping in {path.name}")
        canonical = json.dumps(sorted(mapping.items()), ensure_ascii=False, separators=(",", ":"))
        return mapping, hashlib.sha256(canonical.encode("utf-8")).digest()

    def load_serial_mapping(self):
        path = self.progression_dir.parent / "upgrade_ids.csv"
        if path.is_file():
            self.serial_by_name, self.serial_digest = self.read_serial_mapping(path)
        else:
            self.serial_by_name, self.serial_digest = {}, EMPTY_SERIAL_DIGEST

    @staticmethod
    def _save_header(raw: bytes):
        if not raw.startswith(SAVE_MAGIC):
            return None, None, 0
        if len(raw) < SAVE_HEADER.size:
            raise SaveError("Save header is incomplete")
        magic, version, schema = SAVE_HEADER.unpack(raw[:SAVE_HEADER.size])
        if version == 1:
            return schema, None, SAVE_HEADER.size
        if version != SAVE_VERSION:
            raise SaveError(f"Unsupported save format version: {version}")
        if len(raw) < SERIAL_SAVE_HEADER.size:
            raise SaveError("Save serial header is incomplete")
        return schema, SERIAL_SAVE_HEADER.unpack(raw[:SERIAL_SAVE_HEADER.size])[3], SERIAL_SAVE_HEADER.size

    def _load_fields(
        self,
        path: Path,
        source: str,
        name_column: str,
        category_column: str,
        type_column: str,
        fields: List[SaveField],
    ) -> None:
        with path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            required = {name_column, category_column, type_column}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(f"Missing required columns in {path.name}")
            for row_number, row in enumerate(reader, start=2):
                name = row[name_column].strip()
                category = row[category_column].strip()
                data_type = row[type_column].strip().lower()
                if not name or not category:
                    raise ValueError(f"Blank save field at {path.name}:{row_number}")
                if data_type not in FORMAT_DETAILS:
                    raise ValueError(
                        f"Unsupported data format '{data_type}' at {path.name}:{row_number}"
                    )
                fields.append(
                    SaveField(
                        source=source,
                        name=name,
                        category=category,
                        data_type=data_type,
                        index=len(fields),
                    )
                )

    def _require_schema(self) -> None:
        if not self.fields:
            self.load_schema()

    def _old_schema_files_exist(self) -> bool:
        return (
            (self.progression_dir / "village-old.csv").is_file()
            or (self.progression_dir / "collection-old.csv").is_file()
        )

    def player_path(self, user_id: int) -> Path:
        user_id = int(user_id)
        if user_id < 0:
            raise ValueError("Discord user ID cannot be negative")
        return self.saves_dir / f"{user_id}.sav"

    def ensure_player(self, user_id: int) -> Path:
        self._require_schema()
        path = self.player_path(user_id)
        try:
            with path.open("xb") as file:
                file.write(self._encode_values(self._default_values()))
                file.flush()
                os.fsync(file.fileno())
        except FileExistsError:
            self._read_values(path)
        return path

    def _default_values(self) -> List[int]:
        values = [0] * len(self.fields)
        field = self.village_by_name.get("Last Resource Check")
        if field is not None:
            values[field.index] = self._bounded_value(int(time.time()), field.data_type)
        return values

    @contextmanager
    def transaction(self, user_id: int):
        with self.lock:
            path = self.ensure_player(user_id)
            values = self._read_values(path)
            village = {name: values[field.index] for name, field in self.village_by_name.items()}
            collection = {name: values[field.index] for name, field in self.collection_by_name.items()}
            yield village, collection
            updated = []
            for field in self.fields:
                source = village if field.source == "village" else collection
                updated.append(self._bounded_value(source[field.name], field.data_type))
            if updated != values:
                self._write_values(path, updated)

    def _encode_values(self, values: List[int]) -> bytes:
        if len(values) != len(self.fields):
            raise ValueError(
                f"Expected {len(self.fields)} save values but received {len(values)}"
            )
        payload = struct.pack(self.binary_format, *values)
        return SERIAL_SAVE_HEADER.pack(SAVE_MAGIC, SAVE_VERSION, self.schema_digest, self.serial_digest) + payload

    def _decode_raw(self, raw: bytes, allow_legacy: bool = False, allow_serial_mismatch: bool = False) -> Tuple[List[int], bool]:
        schema_digest, serial_digest, header_size = self._save_header(raw)
        if not allow_serial_mismatch and (serial_digest or EMPTY_SERIAL_DIGEST) != self.serial_digest:
            raise SaveSchemaMismatch("Upgrade serial mapping requires /update_saves before this save can be used")
        if raw.startswith(SAVE_MAGIC):
            if schema_digest != self.schema_digest:
                raise SaveSchemaMismatch(
                    "Save schema does not match the loaded progression files"
                )
            payload = raw[header_size:]
            legacy = False
        else:
            if not allow_legacy:
                raise SaveSchemaMismatch(
                    "Legacy save requires migration before it can be loaded"
                )
            payload = raw
            legacy = True

        if len(payload) != self.record_size:
            raise SaveError(
                f"Save payload is {len(payload)} bytes but this schema requires "
                f"{self.record_size} bytes"
            )
        values = [int(value) for value in struct.unpack(self.binary_format, payload)]
        return values, legacy

    def _read_values(self, path: Path) -> List[int]:
        allow_legacy = not self._old_schema_files_exist()
        values, legacy = self._decode_raw(path.read_bytes(), allow_legacy=allow_legacy)
        if legacy:
            self._write_values(path, values)
        return values

    def _write_values(self, path: Path, values: List[int]) -> None:
        raw = self._encode_values(values)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as file:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _bounded_value(self, value: int, data_type: str) -> int:
        minimum, maximum = FORMAT_DETAILS[data_type][1:]
        return min(maximum, max(minimum, int(value)))

    def _bounded_add(self, current: int, amount: int, data_type: str) -> int:
        return self._bounded_value(current + int(amount), data_type)

    def has_village_field(self, name: str) -> bool:
        self._require_schema()
        return name in self.village_by_name

    def has_collection_field(self, name: str) -> bool:
        self._require_schema()
        return name in self.collection_by_name

    def get_village_value(self, user_id: int, name: str) -> int:
        self._require_schema()
        field = self.village_by_name[name]
        path = self.ensure_player(user_id)
        return self._read_values(path)[field.index]

    def player_values(self, user_id: int) -> Tuple[Dict[str, int], Dict[str, int]]:
        self._require_schema()
        path = self.ensure_player(user_id)
        values = self._read_values(path)
        village = {
            name: values[field.index]
            for name, field in self.village_by_name.items()
        }
        collection = {
            name: values[field.index]
            for name, field in self.collection_by_name.items()
        }
        return village, collection

    def update_village_values(
        self,
        user_id: int,
        set_values: Optional[Mapping[str, int]] = None,
        additions: Optional[Mapping[str, int]] = None,
    ) -> Dict[str, int]:
        self._require_schema()
        path = self.ensure_player(user_id)
        values = self._read_values(path)
        changed: Dict[str, int] = {}

        for name, value in (set_values or {}).items():
            field = self.village_by_name.get(name)
            if field is None:
                raise KeyError(f"Unknown village save field: {name}")
            values[field.index] = self._bounded_value(value, field.data_type)
            changed[name] = values[field.index]

        for name, amount in (additions or {}).items():
            field = self.village_by_name.get(name)
            if field is None:
                raise KeyError(f"Unknown village save field: {name}")
            values[field.index] = self._bounded_add(
                values[field.index], amount, field.data_type
            )
            changed[name] = values[field.index]

        self._write_values(path, values)
        return changed

    def update_rewards(
        self,
        user_id: int,
        village_additions: Mapping[str, int],
        collection_unlocks: Iterable[str] = (),
    ) -> Dict[str, int]:
        self._require_schema()
        path = self.ensure_player(user_id)
        values = self._read_values(path)
        changed: Dict[str, int] = {}

        for name, amount in village_additions.items():
            field = self.village_by_name.get(name)
            if field is None:
                raise KeyError(f"Unknown village save field: {name}")
            values[field.index] = self._bounded_add(
                values[field.index], amount, field.data_type
            )
            changed[name] = values[field.index]

        for name in collection_unlocks:
            field = self.collection_by_name.get(name)
            if field is None:
                raise KeyError(f"Unknown collection save field: {name}")
            values[field.index] = 1
            changed[name] = 1

        self._write_values(path, values)
        return changed

    def resolve_category(self, category: str) -> str:
        self._require_schema()
        resolved = self.categories_by_key.get(category.strip().casefold())
        if resolved is None:
            raise KeyError(category)
        return resolved

    def category_values(self, user_id: int, category: str) -> List[Tuple[str, int]]:
        resolved = self.resolve_category(category)
        path = self.ensure_player(user_id)
        values = self._read_values(path)
        return [
            (field.name, values[field.index])
            for field in self.fields_by_category[resolved]
        ]

    def _decode_migration_layout(self, raw: bytes) -> Optional[Tuple[List[int], int]]:
        if raw.startswith(SAVE_MAGIC) and "Squad Barracks" in self.village_by_name:
            digest, _, size = self._save_header(raw)
            prior = [f for f in self.fields if f.identity != ("village", "Squad Barracks")]
            schema = "\n".join("\0".join((f.source, f.name, f.category, f.data_type)) for f in prior)
            if hashlib.sha256(schema.encode()).digest() == digest:
                fmt = "<" + "".join(FORMAT_DETAILS[f.data_type][0] for f in prior)
                if len(raw) - size != struct.calcsize(fmt):
                    raise SaveError("Save payload does not match the pre Squad Barracks schema")
                values = self._default_values()
                for field, value in zip(prior, struct.unpack_from(fmt, raw, size)):
                    values[field.index] = value
                return values, 0
        path = self.progression_dir / "save_migrations.json"
        if not path.is_file() or not raw.startswith(SAVE_MAGIC) or len(raw) < SAVE_HEADER.size:
            return None
        digest, _, header_size = self._save_header(raw)
        with path.open(encoding="utf-8") as file:
            layouts = json.load(file)
        layout = layouts.get(digest.hex())
        if layout is None:
            return None
        payload = raw[header_size:]
        if len(payload) != struct.calcsize(layout["format"]):
            raise SaveError("Save payload does not match its migration layout")
        old_values = struct.unpack(layout["format"], payload)
        values = self._default_values()
        fields = {field.identity: field for field in self.fields}
        for source, name, index in layout["fields"]:
            if source == "village":
                name = SAVE_FIELD_RENAMES.get(name, name)
            field = fields.get((source, name))
            if field is not None:
                values[field.index] = self._bounded_value(old_values[index], field.data_type)
        retired = set(layout.get("retired_serials", []))
        for field in self.fields:
            if field.source != "village" or not field.name.endswith(" Upgrade"):
                continue
            if values[field.index] not in retired:
                continue
            prefix = field.name.removesuffix(" Upgrade")
            for suffix in (" Upgrade", " Time", " Currency", " Cost"):
                slot_field = self.village_by_name.get(prefix + suffix)
                if slot_field is not None:
                    values[slot_field.index] = 0
        return values, len(old_values) - len(layout["fields"])

    def _remap_serials(self, raw: bytes, values: List[int], recover_unmappable: bool = True) -> int:
        _, source_digest, _ = self._save_header(raw)
        if source_digest == self.serial_digest:
            return 0
        references = []
        for field in self.fields:
            if field.source != "village":
                continue
            if re.fullmatch(r"(?:Builder|Researcher) #\d+ Upgrade", field.name):
                time_name = field.name.removesuffix(" Upgrade") + " Time"
            elif re.fullmatch(r"Upgrade #\d+ Name", field.name):
                time_name = field.name.removesuffix(" Name") + " Time"
            else:
                continue
            time_field = self.village_by_name.get(time_name)
            if time_field is None:
                raise SaveError(f"Missing timestamp for {field.name}")
            if values[field.index] or values[time_field.index]:
                references.append((field, time_field))
        if not references:
            return 0
        if self.serial_digest == EMPTY_SERIAL_DIGEST:
            if source_digest not in (None, EMPTY_SERIAL_DIGEST):
                raise SaveError("Cannot migrate serials without upgrade_ids.csv")
            return 0
        previous_path = self.progression_dir.parent / "upgrade_ids-old.csv"
        if not previous_path.is_file():
            raise SaveError("Provide the matching upgrade_ids-old.csv before migrating serials")
        previous, previous_digest = self.read_serial_mapping(previous_path)
        if source_digest is not None and source_digest != previous_digest:
            names = (
                field.name for field in self.fields
                if "level" in field.category.casefold() or field.category == "Pet" or field.name == "Town Hall"
            )
            generated = {name: serial for serial, name in enumerate(names, 1)}
            canonical = json.dumps(sorted(generated.items()), ensure_ascii=False, separators=(",", ":"))
            if hashlib.sha256(canonical.encode("utf-8")).digest() != source_digest:
                archive = previous_path.with_name(f"upgrade_ids-{source_digest.hex()}.csv")
                if not archive.is_file():
                    raise SaveSchemaMismatch(f"Missing matching historical serial mapping: {archive.name}")
                generated, digest = self.read_serial_mapping(archive)
                if digest != source_digest:
                    raise SaveSchemaMismatch(f"Historical serial mapping fingerprint mismatch: {archive.name}")
            previous = generated
        old_names = {serial: name for name, serial in previous.items()}
        cancelled = 0
        for field, time_field in references:
            worker = field.name.endswith(" Upgrade")
            serial = values[field.index]
            name = old_names.get(serial)
            if name is None or name not in self.serial_by_name:
                if worker and recover_unmappable and values[time_field.index] > 0:
                    from resource_system import RESOURCE_CODES
                    prefix = field.name.removesuffix(" Upgrade")
                    cost_field = self.village_by_name[prefix + " Cost"]
                    currency_field = self.village_by_name[prefix + " Currency"]
                    amount = values[cost_field.index]
                    code = values[currency_field.index]
                    resource = next((key for key, value in RESOURCE_CODES.items() if value == code), None)
                    if amount < 0 or (amount and resource is None):
                        raise SaveError(f"Cannot determine the recorded refund for {prefix}")
                    if amount:
                        balance = self.village_by_name[resource]
                        total = values[balance.index] + amount
                        if total > FORMAT_DETAILS[balance.data_type][2]:
                            raise SaveError(f"Cannot store the full {resource} refund for {prefix}")
                        values[balance.index] = total
                    for slot_field in (field, time_field, cost_field, currency_field):
                        values[slot_field.index] = 0
                    cancelled += 1
                    continue
                raise SaveError(f"Cannot migrate {field.name}: old serial {serial} ({name or 'unknown'}) has no new mapping")
            target = self.serial_by_name[name]
            if target > FORMAT_DETAILS[field.data_type][2]:
                raise SaveError(f"New serial {target} exceeds the storage limit for {field.name}")
            values[field.index] = target

        return cancelled

    def migrate_from(self, old_store: "SaveStore", backups_dir: Path, *, recover_unmappable: bool = True) -> MigrationReport:
        self._require_schema()
        old_store._require_schema()
        if self.saves_dir.resolve() != old_store.saves_dir.resolve():
            raise ValueError("Old and new schemas must target the same saves folder")

        old_fields = {field.identity: field for field in old_store.fields}
        new_fields = {field.identity: field for field in self.fields}
        for previous, current in SAVE_FIELD_RENAMES.items():
            old_key, new_key = ("village", previous), ("village", current)
            if old_key in old_fields and new_key in new_fields and new_key not in old_fields:
                old_fields[new_key] = old_fields.pop(old_key)
        common = old_fields.keys() & new_fields.keys()
        added_fields = len(new_fields.keys() - old_fields.keys())
        removed_fields = len(old_fields.keys() - new_fields.keys())
        changed_types = sum(
            old_fields[key].data_type != new_fields[key].data_type for key in common
        )
        moved_fields = sum(
            old_fields[key].index != new_fields[key].index for key in common
        )
        changed_categories = sum(
            old_fields[key].category != new_fields[key].category for key in common
        )

        self.saves_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=".saves-migration-", dir=self.saves_dir.parent)
        )
        migrated_saves = 0
        cancelled_upgrades = 0
        current_saves = 0
        save_paths = sorted(self.saves_dir.glob("*.sav"))

        try:
            for source_path in save_paths:
                raw = source_path.read_bytes()
                try:
                    self._decode_raw(raw)
                    current_saves += 1
                    continue
                except SaveSchemaMismatch:
                    pass
                try:
                    try:
                        migrated_values, _ = self._decode_raw(raw, allow_serial_mismatch=True)
                    except SaveSchemaMismatch:
                        layout_values = self._decode_migration_layout(raw)
                        if layout_values is not None:
                            migrated_values, retired_count = layout_values
                            removed_fields = max(removed_fields, retired_count)
                        else:
                            old_values, _ = old_store._decode_raw(raw, allow_legacy=True, allow_serial_mismatch=True)
                            migrated_values = self._default_values()
                            for identity in common:
                                old_field = old_fields[identity]
                                new_field = new_fields[identity]
                                migrated_values[new_field.index] = self._bounded_value(
                                    old_values[old_field.index], new_field.data_type
                                )
                    cancelled_upgrades += self._remap_serials(raw, migrated_values, recover_unmappable)
                except (SaveError, ValueError) as error:
                    raise SaveError(f"Cannot migrate {source_path.name}: {error}. No player saves were changed.") from error
                self._write_values(staging / source_path.name, migrated_values)
                migrated_saves += 1

            for staged_path in staging.glob("*.sav"):
                self._decode_raw(staged_path.read_bytes())

            backup_path = None
            if migrated_saves:
                backups_dir.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
                backup_path = backups_dir / f"saves-{timestamp}"
                shutil.copytree(self.saves_dir, backup_path)
                for staged_path in sorted(staging.glob("*.sav")):
                    os.replace(staged_path, self.saves_dir / staged_path.name)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

        return MigrationReport(
            total_saves=len(save_paths),
            migrated_saves=migrated_saves,
            current_saves=current_saves,
            added_fields=added_fields,
            removed_fields=removed_fields,
            changed_types=changed_types,
            moved_fields=moved_fields,
            changed_categories=changed_categories,
            backup_path=backup_path,
            cancelled_upgrades=cancelled_upgrades,
        )
