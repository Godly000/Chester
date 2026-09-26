import asyncio
import csv
import logging
import os
import random
import time
import tempfile

import discord

from image_proxy import proxy_image_url


GEM_BOX_TIMEOUT_SECONDS = 60
GEM_BOX_MIN_GEMS = 25
GEM_BOX_MAX_GEMS = 50
GEM_BOX_IMAGES = (
    ("Top Right", "https://i.imgur.com/2hV7gTP.png"),
    ("Top Left", "https://i.imgur.com/yqbcCrb.png"),
    ("Bottom Right", "https://i.imgur.com/qju8XK9.png"),
    ("Bottom Left", "https://i.imgur.com/vVCWo8l.png"),
)
GOBLIN_BUILDER_IMAGE = "https://static.wikia.nocookie.net/clashofclans/images/9/9a/Goblin_Builder_info.png"
PUNCHED_GOBLIN_BUILDER_IMAGE = "https://i.imgur.com/Ft9zPsM.png"
log = logging.getLogger("loot_bot")


class GemBoxSystem:
    def __init__(self, store, log_path, chance=0.01, migration_active=lambda: False, rng=None):
        self.store = store
        self.log_path = log_path
        self.chance = chance
        self.migration_active = migration_active
        self.rng = rng if rng is not None else random.SystemRandom()
        self.active = {}
        self.rolling = set()

    def record_failure(self, user_id, reason):
        if reason not in {"timeout", "incorrect_answer"}:
            raise ValueError("Unknown Gem Box failure reason")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        header = ["Discord User ID", "Timestamp", "Reason"]
        if self.log_path.exists() and self.log_path.stat().st_size:
            with self.log_path.open(newline="", encoding="utf-8") as file:
                reader = csv.reader(file)
                previous_header = next(reader, [])
                old_rows = list(reader) if previous_header == header[:2] else None
            if old_rows is not None:
                temporary_path = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8", dir=self.log_path.parent, delete=False) as file:
                        temporary_path = file.name
                        writer = csv.writer(file)
                        writer.writerow(header)
                        writer.writerows(row + ["unknown"] if len(row) == 2 else row for row in old_rows)
                        file.flush()
                        os.fsync(file.fileno())
                    os.replace(temporary_path, self.log_path)
                finally:
                    if temporary_path and os.path.exists(temporary_path):
                        os.unlink(temporary_path)
        with self.log_path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            if file.tell() == 0:
                writer.writerow(header)
            writer.writerow([user_id, int(time.time()), reason])
            file.flush()
            os.fsync(file.fileno())

    def award(self, user_id):
        rolled = self.rng.randint(GEM_BOX_MIN_GEMS, GEM_BOX_MAX_GEMS)
        with self.store.transaction(user_id) as (village, collection):
            before = village["Gems"]
            field = self.store.village_by_name["Gems"]
            village["Gems"] = self.store._bounded_add(before, rolled, field.data_type)
            credited = village["Gems"] - before
            if "Total Gems" in village:
                field = self.store.village_by_name["Total Gems"]
                village["Total Gems"] = self.store._bounded_add(village["Total Gems"], credited, field.data_type)
        return credited

    async def maybe_offer(self, interaction, rarity):
        if rarity.casefold() not in {"common", "rare"}:
            return None
        if self.rng.random() >= self.chance or interaction.user.id in self.active:
            return None
        return await self.offer(interaction)

    async def offer(self, interaction, answer=None, practice=False):
        if interaction.user.id in self.active:
            await interaction.followup.send("Finish your current Gem Box encounter first.", ephemeral=True)
            return None
        if answer is None:
            answer = self.rng.randrange(len(GEM_BOX_IMAGES))
        if answer not in range(len(GEM_BOX_IMAGES)):
            raise ValueError("Gem Box image must be from one through four")
        view = GemBoxView(self, interaction.user.id, answer, practice=practice)
        self.active[interaction.user.id] = view
        try:
            view.message = await interaction.followup.send(
                content=interaction.user.mention, embed=view.prompt(), view=view,
                ephemeral=False, wait=True,
            )
        except Exception:
            self.active.pop(interaction.user.id, None)
            view.stop()
            raise
        view.timer = asyncio.create_task(view.expire())
        return view


class GemBoxView(discord.ui.View):
    def __init__(self, system, user_id, answer, practice=False):
        super().__init__(timeout=None)
        self.system = system
        self.user_id = user_id
        self.answer = answer
        self.practice = practice
        self.deadline = time.monotonic() + GEM_BOX_TIMEOUT_SECONDS
        self.expires_at = int(time.time()) + GEM_BOX_TIMEOUT_SECONDS
        self.lock = asyncio.Lock()
        self.finished = False
        self.message = None
        self.timer = None
        for index, (label, _) in enumerate(GEM_BOX_IMAGES):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            async def callback(interaction, selected=index):
                await self.respond(interaction, selected)
            button.callback = callback
            self.add_item(button)

    def prompt(self):
        embed = discord.Embed(
            title="You found a Gem Box!",
            description=(
                "Where does the Gem Box's Rainbow come from?\n"
                + (f"Answer before <t:{self.expires_at}:R>. Practice encounter: no Gem rewards or failure logs."
                   if self.practice else f"Answer before <t:{self.expires_at}:R> to win **25–50 Gems**.")
            ),
            color=discord.Color.gold(),
        )
        embed.set_image(url=proxy_image_url(GEM_BOX_IMAGES[self.answer][1]))
        return embed

    def finish(self, success, reason="incorrect_answer"):
        if success:
            gems = 0 if self.practice else self.system.award(self.user_id)
            description = "Correct! Practice encounter complete. No Gems were awarded." if self.practice else f"Correct! You received **{gems:,} Gems** from the Gem Box."
            embed = discord.Embed(title="You caught the Goblin Builder!", description=description, color=discord.Color.green())
            embed.set_image(url=proxy_image_url(PUNCHED_GOBLIN_BUILDER_IMAGE))
        else:
            if not self.practice:
                self.system.record_failure(self.user_id, reason)
            embed = discord.Embed(title="The Goblin Builder stole the Gems!", description=("You ran out of time." if reason == "timeout" else "You answered incorrectly.") + " The Goblin Builder stole all the Gems from this Gem Box.", color=discord.Color.red())
            embed.set_image(url=proxy_image_url(GOBLIN_BUILDER_IMAGE))
        self.finished = True
        if self.system.active.get(self.user_id) is self:
            self.system.active.pop(self.user_id)
        for child in self.children:
            child.disabled = True
        self.stop()
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        return embed

    async def respond(self, interaction, selected):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This Gem Box belongs to another player.", ephemeral=True)
            return
        async with self.lock:
            if self.finished:
                await interaction.response.send_message("This Gem Box has already been resolved.", ephemeral=True)
                return
            timed_out = time.monotonic() >= self.deadline
            success = not timed_out and selected == self.answer
            if success and not self.practice and self.system.migration_active():
                await interaction.response.send_message("Saves are being updated. Please try your answer again shortly.", ephemeral=True)
                return
            await interaction.response.defer()
            embed = self.finish(success, reason="timeout" if timed_out else "incorrect_answer")
            await interaction.edit_original_response(embed=embed, view=self)

    async def expire(self):
        try:
            await asyncio.sleep(max(0, self.deadline - time.monotonic()))
            async with self.lock:
                if self.finished:
                    return
                embed = self.finish(False, reason="timeout")
                await self.message.edit(embed=embed, view=self)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Failed to expire Gem Box for %s", self.user_id)
