import csv


ATTACK_BOOST_FIELDS = {
    "Power Pancakes": "Power Pancakes Expiration",
    "Mighty Morsel": "Mighty Morsel Expiration",
}
TIMED_BOOST_FIELDS = {
    "Clan Castle Cake": "Clan Castle Cake Expiration",
    "Power Potion": "Power Potion Expiration",
    "Hero Potion": "Hero Potion Expiration",
}
BOOST_FIELDS = {**ATTACK_BOOST_FIELDS, **TIMED_BOOST_FIELDS}
NO_CLAN_CASTLE_PENALTY = 0.1


def load_boost_targets(path):
    targets = {}
    with path.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            name = row["Name"].strip()
            if name in BOOST_FIELDS:
                groups = {value.strip().removesuffix(" Level") for value in row["Target"].split(",") if value.strip()}
                if not groups or not groups <= {"Troop", "Siege", "Hero", "Pet", "Equipment", "Clan Castle"}:
                    raise ValueError(f"Invalid attack boost targets for {name}")
                targets[name] = groups
    if targets.keys() != BOOST_FIELDS.keys():
        raise ValueError("Attack boost definitions are missing from magicitems.csv")
    return targets


def boosted_offense(village, fields, bounds, targets, now):
    values = dict(village)
    groups = set()
    for item, field in BOOST_FIELDS.items():
        active = village.get(field, 0) > (0 if item in ATTACK_BOOST_FIELDS else now)
        if active:
            groups.update(targets[item])
    for name, (_, maximum) in bounds.items():
        field = fields.get(name)
        if field is not None and field.category.removesuffix(" Level") in groups and village.get(name, 0) > 0:
            values[name] = max(values[name], maximum)
    return values


def consume_attack_boosts(village):
    for field in ATTACK_BOOST_FIELDS.values():
        if village.get(field, 0) > 0:
            village[field] -= 1