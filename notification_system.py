import asyncio
import json
import logging
import os
import time
from pathlib import Path

from player_saves import SaveSchemaMismatch

NOTIFICATION_INTERVAL = 60
NOTIFICATION_RETRY_SECONDS = 900
UPGRADE_OPTION = "Notification for finished upgrades"
COLLECTOR_OPTION = "Notification for full collectors"

log = logging.getLogger("loot_bot")


class NotificationSystem:
    def __init__(self, bot, store, upgrades, resources, state_path, paused):
        self.bot = bot
        self.store = store
        self.upgrades = upgrades
        self.resources = resources
        self.path = Path(state_path)
        self.paused = paused
        self.task = None
        self.state = {}
        try:
            content = self.path.read_text(encoding="utf-8-sig").strip()
            self.state = json.loads(content) if content else {}
            if not isinstance(self.state, dict):
                raise ValueError("Invalid notification state")
            if not content:
                self.save_state()
        except FileNotFoundError:
            pass
        except (ValueError, UnicodeError) as error:
            self.state = {}
            backup = self.path.with_name(f"{self.path.name}.invalid.{time.time_ns()}.bak")
            try:
                self.path.rename(backup)
                self.save_state()
                log.warning("Invalid notification state backed up to %s and reset: %s", backup, error)
            except OSError:
                log.exception("Could not back up and reset invalid notification state")
        except OSError:
            log.exception("Could not read notification state")
            self.state = {}

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    def save_state(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state), encoding="utf-8")
        os.replace(temporary, self.path)

    def check_player(self, user_id, now):
        village, _ = self.store.player_values(user_id)
        previous = self.state.setdefault(str(user_id), {})
        jobs = {}
        messages = []
        if village.get(UPGRADE_OPTION):
            for slot, finish in self.upgrades.builder_slots + self.upgrades.researcher_slots:
                name = self.upgrades.name_by_serial.get(village[slot])
                if name and village[finish] > 0:
                    key = f"{slot}:{village[slot]}:{village[finish]}"
                    jobs[key] = {"name": name, "level": village[name] + 1, "finish": village[finish]}
            for key, job in previous.get("jobs", {}).items():
                if key not in jobs and village.get(job["name"], 0) >= job["level"]:
                    jobs[key] = job
            sent = set(previous.get("sent", []))
            for key, job in jobs.items():
                if job["finish"] <= now and key not in sent:
                    messages.append(("upgrade", key, f"{job['name']} reached level {job['level']} <t:{job['finish']}:R>. Use /refresh_upgrades to refresh your village."))
            previous["jobs"] = jobs
            previous["sent"] = list(sent.intersection(jobs))
        else:
            previous["jobs"] = {}
            previous["sent"] = []
        full = {}
        if village.get(COLLECTOR_OPTION):
            for collector in self.resources.collector_status(village, now):
                if collector.capacity > 0 and collector.hourly_rate > 0 and collector.stored >= collector.capacity:
                    key = f"{collector.item}:{collector.level}:{village['Last Resource Check']}"
                    full[key] = collector
            sent = set(previous.get("collectors", []))
            for key, collector in full.items():
                if key not in sent:
                    messages.append(("collector", key, f"{collector.item} is full with {collector.capacity:,} {collector.resource}. Use /collect_loot to collect it."))
            previous["collectors"] = list(sent.intersection(full))
        else:
            previous["collectors"] = []
        return messages

    async def run_once(self):
        if self.paused():
            return
        now = int(time.time())
        for path in list(self.store.saves_dir.glob("*.sav")):
            if self.paused():
                break
            if not path.stem.isdigit():
                continue
            user_id = int(path.stem)
            state = self.state.setdefault(str(user_id), {})
            try:
                marker = f"{self.store.schema_digest.hex()}:{path.stat().st_mtime_ns}:{path.stat().st_size}"
                if state.get("schema_mismatch") == marker or state.get("retry", 0) > now:
                    continue
                messages = self.check_player(user_id, now)
                state.pop("schema_mismatch", None)
                state = self.state[str(user_id)]
                if not messages or state.get("retry", 0) > now:
                    continue
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                batch = []
                for message in messages:
                    if batch and len("\n".join(entry[2] for entry in batch + [message])) > 1900:
                        await self.deliver(user, state, batch)
                        batch = []
                    batch.append(message)
                if batch:
                    await self.deliver(user, state, batch)
                state.pop("retry", None)
            except SaveSchemaMismatch:
                state["schema_mismatch"] = marker
                log.warning("Notifications skipped for player %s because the save schema differs from village.csv and collection.csv. Restore matching schema files or run /update_saves with the correct previous files.", user_id)
            except Exception:
                log.exception("Could not process notifications for player %s", user_id)
                self.state.setdefault(str(user_id), {})["retry"] = now + NOTIFICATION_RETRY_SECONDS
        self.save_state()

    async def deliver(self, user, state, messages):
        await user.send("\n".join(message[2] for message in messages))
        for kind, key, _ in messages:
            state.setdefault("sent" if kind == "upgrade" else "collectors", []).append(key)
        self.save_state()

    async def run(self):
        while not self.bot.is_closed():
            try:
                if self.bot.is_ready():
                    await self.run_once()
            except Exception:
                log.exception("Notification check failed")
            await asyncio.sleep(NOTIFICATION_INTERVAL)
